#!/usr/bin/env bash

# Promote a configured/degraded binding to verified after one minimal call.
# Does not select models, retry, or fallback. Observed model is recorded
# separately from the requested model.

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
SKILL_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
PLUGIN_ROOT=$(cd -- "$SKILL_DIR/../.." && pwd -P)
# shellcheck source=lib/data-root.sh
source "$SCRIPT_DIR/lib/data-root.sh"
agents_apply_data_root
DISPATCH="$SCRIPT_DIR/dispatch.sh"
BINDINGS_FILE="$LOCAL_ROOT/bindings.tsv"
EVIDENCE_FILE="$LOCAL_ROOT/binding-evidence.tsv"
FAILURES_FILE="$LOCAL_ROOT/binding-failures.tsv"

API=
HARNESS=
MODEL=
EFFORT=
WORK_DIR=$PWD
TIMEOUT_SECONDS=120

usage() {
  cat <<'EOF'
Usage: verify.sh --api ID --harness ID --model ID [options]

Runs one minimal read-only prompt through dispatch.sh for a configured or
degraded binding. Success appends binding-evidence.tsv and flips the registry
row to verified. Failure appends binding-failures.tsv and leaves state unchanged.

verified rows are rejected. This is the only supported way to pass
dispatch.sh --allow-state.

Options:
  --effort LEVEL
  --dir DIR
  --timeout-seconds N   default: 120
EOF
}

die() {
  printf 'verify.sh: %s\n' "$*" >&2
  exit 2
}

need_arg() {
  (($# >= 2)) || die "missing value for $1"
}

is_uint() {
  [[ ${1:-} =~ ^[0-9]+$ ]]
}

tsv_lookup() {
  awk -F '\t' \
    -v api="$API" -v harness="$HARNESS" -v model="$MODEL" \
    '/^#/ || NF == 0 { next }
     $1 == api && $2 == harness && $3 == model { print; found = 1 }
     END { exit found ? 0 : 1 }' "$BINDINGS_FILE"
}

flip_verified() {
  local tmp
  tmp=$(mktemp)
  awk -F '\t' -v OFS='\t' \
    -v api="$API" -v harness="$HARNESS" -v model="$MODEL" \
    'BEGIN { changed = 0 }
     /^#/ || NF == 0 { print; next }
     $1 == api && $2 == harness && $3 == model {
       $4 = "verified"
       changed = 1
     }
     { print }
     END { if (!changed) exit 1 }' "$BINDINGS_FILE" >"$tmp"
  mv "$tmp" "$BINDINGS_FILE"
}

append_row() {
  local file=$1
  shift
  printf '%s\n' "$*" >>"$file"
}

observed_from_output() {
  local out=$1 harness=$2
  command -v jq >/dev/null 2>&1 || { printf '%s' '-'; return; }
  case $harness in
    grok-build)
      jq -r '
        if type == "object" and (.modelUsage | type == "object") then
          (.modelUsage | keys[0] // empty)
        elif type == "object" and .model != null then
          .model | tostring
        else empty end
      ' "$out" 2>/dev/null || true
      ;;
    claude-old)
      jq -r '
        if type == "object" then
          (.model // .result.model // empty | tostring)
        else empty end
      ' "$out" 2>/dev/null || true
      ;;
    codex-cli)
      jq -r '
        select(type == "object")
        | (.model // .payload.model // .item.model // empty | tostring)
      ' "$out" 2>/dev/null | awk 'NF { print; exit }' || true
      ;;
    *)
      printf '%s' ''
      ;;
  esac
}

session_from_output() {
  local out=$1 harness=$2
  command -v jq >/dev/null 2>&1 || { printf '%s' '-'; return; }
  case $harness in
    grok-build)
      jq -r '.sessionId // empty' "$out" 2>/dev/null || true
      ;;
    *)
      printf '%s' ''
      ;;
  esac
}

# Mechanical probe: the dispatch stdout, after harness-specific unwrap, must be exactly PING.
# grok-build --output-format json stores the assistant text in .text; other harnesses write plain text.
extract_probe_text() {
  local out=$1 harness=$2
  [[ -s $out ]] || return 0
  case $harness in
    grok-build)
      command -v jq >/dev/null 2>&1 || return 0
      jq -r 'if type == "object" and (.text | type == "string") then .text else empty end' \
        "$out" 2>/dev/null || true
      ;;
    *)
      tr -d '\r' <"$out"
      ;;
  esac
}

normalize_probe_text() {
  sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}

while (($#)); do
  case $1 in
    --api) need_arg "$@"; API=$2; shift 2 ;;
    --harness) need_arg "$@"; HARNESS=$2; shift 2 ;;
    --model) need_arg "$@"; MODEL=$2; shift 2 ;;
    --effort) need_arg "$@"; EFFORT=$2; shift 2 ;;
    --dir|--cwd) need_arg "$@"; WORK_DIR=$2; shift 2 ;;
    --timeout-seconds) need_arg "$@"; TIMEOUT_SECONDS=$2; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ -n $API && -n $HARNESS && -n $MODEL ]] || \
  die 'provide all of --api --harness --model'
[[ -x $DISPATCH ]] || die "dispatcher is not executable: $DISPATCH"
[[ -r $BINDINGS_FILE ]] || die "binding registry is not readable: $BINDINGS_FILE"
[[ -d $WORK_DIR ]] || die "working directory does not exist: $WORK_DIR"
is_uint "$TIMEOUT_SECONDS" || die '--timeout-seconds must be a non-negative integer'

ROW=$(tsv_lookup) || die 'unregistered API+harness+model binding'
IFS=$'\t' read -r RESOLVED_API RESOLVED_HARNESS RESOLVED_MODEL BINDING_STATE \
  SELECTOR CATALOG_FILE WRAPPER_SUPPORTED <<<"$ROW"
[[ $WRAPPER_SUPPORTED == yes ]] || die 'binding is host-only; cannot verify via wrapper'
[[ $BINDING_STATE != verified ]] || \
  die 'binding is already verified; verify.sh refuses to re-run a verified row'
[[ $BINDING_STATE == configured || $BINDING_STATE == degraded ]] || \
  die "binding state is $BINDING_STATE; verify.sh accepts configured or degraded only"

TODAY=$(date +%F)
WORKDIR=$(mktemp -d "${TMPDIR:-/tmp}/agents-verify.XXXXXX")
PROMPT="$WORKDIR/prompt.txt"
OUT="$WORKDIR/out.txt"
ERR="$WORKDIR/err.txt"
cat >"$PROMPT" <<'EOF'
Reply with exactly the text PING and nothing else. Do not use tools. Do not inspect files. Do not modify anything.
EOF

DISPATCH_ARGS=(--api "$RESOLVED_API" --harness "$RESOLVED_HARNESS" --model "$RESOLVED_MODEL" \
  --prompt-file "$PROMPT" --dir "$WORK_DIR" --out "$OUT" --stderr "$ERR" \
  --timeout-seconds "$TIMEOUT_SECONDS" --allow-state "$BINDING_STATE")
# Minimal probe: do not inherit production effort defaults (luna max / grok high).
[[ -n $EFFORT ]] || EFFORT=low
DISPATCH_ARGS+=(--effort "$EFFORT")

set +e
"$DISPATCH" "${DISPATCH_ARGS[@]}"
RC=$?
set -e

OBSERVED=$(observed_from_output "$OUT" "$RESOLVED_HARNESS")
[[ -n $OBSERVED ]] || OBSERVED=-
SESSION=$(session_from_output "$OUT" "$RESOLVED_HARNESS")
[[ -n $SESSION ]] || SESSION=-
PROFILE=-
case $SELECTOR in
  profile:*) PROFILE=${SELECTOR#profile:} ;;
  profile-model:*) PROFILE=${SELECTOR#profile-model:}; PROFILE=${PROFILE%%:*} ;;
esac
ENTRYPOINT="dispatch.sh"
case $RESOLVED_HARNESS in
  grok-build) ENTRYPOINT='grok --output-format json --prompt-file' ;;
  codex-cli) ENTRYPOINT='codex exec' ;;
  claude-old) ENTRYPOINT='claude_old -p' ;;
  dsh) ENTRYPOINT='dsh --profile headless' ;;
esac

PROBE_TEXT=$(extract_probe_text "$OUT" "$RESOLVED_HARNESS" | normalize_probe_text)
if ((RC == 0)) && [[ $PROBE_TEXT == PING ]]; then
  [[ -s $EVIDENCE_FILE ]] || die "evidence file missing: $EVIDENCE_FILE"
  append_row "$EVIDENCE_FILE" \
    "${RESOLVED_API}"$'\t'"${RESOLVED_HARNESS}"$'\t'"${RESOLVED_MODEL}"$'\t'"${TODAY}"$'\t'"${RESOLVED_MODEL}"$'\t'"${OBSERVED}"$'\t'"-"$'\t'"-"$'\t'"-"$'\t'"-"$'\t'"-"$'\t'"text; tools unused"$'\t'"${PROFILE}"$'\t'"${EFFORT:-default}"$'\t'"${ENTRYPOINT}"$'\t'"-"$'\t'"0"$'\t'"${SESSION}"$'\t'"verify.sh minimal PING; request_model=${RESOLVED_MODEL} observed=${OBSERVED}"
  flip_verified || die 'failed to flip registry state to verified'
  printf 'verify.sh: promoted %s + %s + %s to verified (observed=%s session=%s)\n' \
    "$RESOLVED_API" "$RESOLVED_HARNESS" "$RESOLVED_MODEL" "$OBSERVED" "$SESSION"
  exit 0
fi

[[ -s $FAILURES_FILE ]] || die "failures file missing: $FAILURES_FILE"
NOTE='verify.sh minimal PING failed'
if ((RC == 0)); then
  NOTE+='; exit 0 but probe text was not PING'
fi
if [[ -s $ERR ]]; then
  NOTE+='; stderr captured'
fi
append_row "$FAILURES_FILE" \
  "${RESOLVED_API}"$'\t'"${RESOLVED_HARNESS}"$'\t'"${RESOLVED_MODEL}"$'\t'"${TODAY}"$'\t'"${RESOLVED_MODEL}"$'\t'"${OBSERVED}"$'\t'"-"$'\t'"${ENTRYPOINT}"$'\t'"${PROFILE}"$'\t'"${EFFORT:-default}"$'\t'"-"$'\t'"${RC}"$'\t'"${SESSION}"$'\t'"${NOTE}"
printf 'verify.sh: %s + %s + %s failed (exit=%s); state left as %s\n' \
  "$RESOLVED_API" "$RESOLVED_HARNESS" "$RESOLVED_MODEL" "$RC" "$BINDING_STATE" >&2
exit 1
