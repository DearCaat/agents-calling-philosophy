#!/usr/bin/env bash

# Optional bounded-parallel wrapper around dispatch.sh.
# The complete manifest is validated before the first model process starts.

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
DISPATCH="$SCRIPT_DIR/dispatch.sh"

MANIFEST=
MAX_PARALLEL=1
DRY_RUN=false

usage() {
  cat <<'EOF'
Usage: parallel.sh --manifest jobs.jsonl [--max-parallel N] [--dry-run]

Every non-empty JSONL row requires:
  job_id, api, harness, model, prompt_file, out, stderr

Optional row fields:
  effort, dir, timeout_seconds

The whole manifest is preflighted before execution. Each dispatch is invoked
once; this wrapper adds no retry, fallback, rate-limit queue, cooldown, or
shared session. Native harness behavior still applies. Any failed job makes
the batch exit non-zero.
EOF
}

die() {
  printf 'parallel.sh: %s\n' "$*" >&2
  exit 2
}

need_arg() {
  (($# >= 2)) || die "missing value for $1"
}

is_uint() {
  [[ ${1:-} =~ ^[0-9]+$ ]]
}

json_value() {
  local row=$1 key=$2
  jq -r --arg key "$key" '.[$key] // empty | tostring' <<<"$row"
}

while (($#)); do
  case $1 in
    --manifest) need_arg "$@"; MANIFEST=$2; shift 2 ;;
    --max-parallel) need_arg "$@"; MAX_PARALLEL=$2; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --help|-h) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ -n $MANIFEST && -r $MANIFEST ]] || \
  die '--manifest must be readable'
is_uint "$MAX_PARALLEL" && ((MAX_PARALLEL > 0)) || \
  die '--max-parallel must be a positive integer'
[[ -x $DISPATCH ]] || die "dispatcher is not executable: $DISPATCH"
command -v jq >/dev/null 2>&1 || die 'jq is required'
command -v realpath >/dev/null 2>&1 || die 'realpath is required'

ROWS=()
declare -A JOB_IDS=()
declare -A OUTPUT_PATHS=()
NEEDS_TIMEOUT=false

build_dispatch_args() {
  local row=$1 job_id api harness model effort work_dir prompt_file out err timeout
  job_id=$(json_value "$row" job_id)
  api=$(json_value "$row" api)
  harness=$(json_value "$row" harness)
  model=$(json_value "$row" model)
  effort=$(json_value "$row" effort)
  work_dir=$(json_value "$row" dir)
  prompt_file=$(json_value "$row" prompt_file)
  out=$(json_value "$row" out)
  err=$(json_value "$row" stderr)
  timeout=$(json_value "$row" timeout_seconds)

  [[ -n $job_id && -n $api && -n $harness && -n $model && \
     -n $prompt_file && -n $out && -n $err ]] || \
    die 'every row needs job_id, api, harness, model, prompt_file, out, and stderr'

  DISPATCH_ARGS=(--api "$api" --harness "$harness" --model "$model" \
    --prompt-file "$prompt_file" --out "$out" --stderr "$err")
  [[ -z $effort ]] || DISPATCH_ARGS+=(--effort "$effort")
  [[ -z $work_dir ]] || DISPATCH_ARGS+=(--dir "$work_dir")
  [[ -z $timeout ]] || DISPATCH_ARGS+=(--timeout-seconds "$timeout")
}

line_no=0
while IFS= read -r row || [[ -n $row ]]; do
  line_no=$((line_no + 1))
  [[ -z ${row//[[:space:]]/} ]] && continue
  jq -e 'type == "object"' >/dev/null <<<"$row" || \
    die "manifest line $line_no is not a JSON object"

  job_id=$(json_value "$row" job_id)
  [[ -n $job_id ]] || die "manifest line $line_no has no job_id"
  [[ -z ${JOB_IDS[$job_id]+x} ]] || die "duplicate job_id: $job_id"
  JOB_IDS[$job_id]=1

  out=$(json_value "$row" out)
  err=$(json_value "$row" stderr)
  [[ -n $out && -n $err ]] || die "job $job_id needs out and stderr"
  out_key=$(realpath -m -- "$out")
  err_key=$(realpath -m -- "$err")
  [[ -z ${OUTPUT_PATHS[$out_key]+x} ]] || \
    die "output path reused by jobs: $out_key"
  OUTPUT_PATHS[$out_key]=$job_id
  [[ -z ${OUTPUT_PATHS[$err_key]+x} ]] || \
    die "output path reused by jobs: $err_key"
  OUTPUT_PATHS[$err_key]=$job_id

  job_timeout=$(json_value "$row" timeout_seconds)
  build_dispatch_args "$row"
  "$DISPATCH" "${DISPATCH_ARGS[@]}" --dry-run >/dev/null || \
    die "preflight failed for job $job_id"
  if [[ -n $job_timeout ]] && ((10#$job_timeout > 0)); then
    NEEDS_TIMEOUT=true
  fi
  ROWS+=("$row")
done <"$MANIFEST"

((${#ROWS[@]} > 0)) || die 'manifest has no jobs'

if [[ $DRY_RUN == false ]]; then
  command -v setsid >/dev/null 2>&1 || die 'setsid is required for execution'
  [[ $NEEDS_TIMEOUT == false ]] || \
    command -v timeout >/dev/null 2>&1 || die 'timeout is required by a timed job'
fi

if [[ $DRY_RUN == true ]]; then
  for row in "${ROWS[@]}"; do
    job_id=$(json_value "$row" job_id)
    build_dispatch_args "$row"
    printf 'job_id=%s\n' "$job_id"
    "$DISPATCH" "${DISPATCH_ARGS[@]}" --dry-run
  done
  exit 0
fi

declare -A PID_TO_JOB=()
ACTIVE_PIDS=()
FAILED=0
CANCELLED=false

remove_pid() {
  local target=$1 pid kept=()
  for pid in "${ACTIVE_PIDS[@]}"; do
    [[ $pid == "$target" ]] || kept+=("$pid")
  done
  ACTIVE_PIDS=("${kept[@]}")
}

terminate_active() {
  local pid
  CANCELLED=true
  for pid in "${ACTIVE_PIDS[@]:-}"; do
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  done
}

trap terminate_active HUP INT TERM

wait_one() {
  local done_pid rc
  if wait -n -p done_pid "${ACTIVE_PIDS[@]}"; then
    rc=0
  else
    rc=$?
  fi
  if [[ -z ${done_pid:-} ]]; then
    FAILED=1
    return
  fi
  printf 'job_id=%s exit_code=%s\n' "${PID_TO_JOB[$done_pid]:-unknown}" "$rc" >&2
  ((rc == 0)) || FAILED=1
  remove_pid "$done_pid"
}

for row in "${ROWS[@]}"; do
  while ((${#ACTIVE_PIDS[@]} >= MAX_PARALLEL)); do
    wait_one
  done
  [[ $CANCELLED == false ]] || break

  job_id=$(json_value "$row" job_id)
  build_dispatch_args "$row"
  setsid --wait "$DISPATCH" "${DISPATCH_ARGS[@]}" &
  pid=$!
  ACTIVE_PIDS+=("$pid")
  PID_TO_JOB[$pid]=$job_id
done

while ((${#ACTIVE_PIDS[@]})); do
  wait_one
done

[[ $CANCELLED == false ]] || exit 130
((FAILED == 0)) || exit 1
