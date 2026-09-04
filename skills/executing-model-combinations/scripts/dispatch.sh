#!/usr/bin/env bash

# Optional single-job wrapper for the canonical binding registry.
# Native harness commands remain the authoritative interface.

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
SKILL_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
PLUGIN_ROOT=$(cd -- "$SKILL_DIR/../.." && pwd -P)
# shellcheck source=lib/data-root.sh
source "$SCRIPT_DIR/lib/data-root.sh"
agents_apply_data_root
BINDINGS_FILE="$LOCAL_ROOT/bindings.tsv"
ADAPTERS_FILE="$LOCAL_ROOT/adapters.tsv"
DEFAULTS_FILE="$SKILL_DIR/references/runtime-defaults.tsv"

LOGIN_HOME=$(getent passwd "${USER:-$(id -un)}" | cut -d: -f6)
[[ -n ${LOGIN_HOME:-} && -d $LOGIN_HOME ]] || LOGIN_HOME=${HOME:-}
export PATH="${LOGIN_HOME:+$LOGIN_HOME/.local/bin:}$PATH"

API=
HARNESS=
MODEL=
EFFORT=
WORK_DIR=$PWD
PROMPT_FILE=
OUT=
STDERR_FILE=
TIMEOUT_SECONDS=0
DRY_RUN=false
LIST_BINDINGS=false
ALLOW_STATE=

usage() {
  cat <<'EOF'
Usage:
  dispatch.sh --api ID --harness ID --model ID --prompt-file FILE [options]
  dispatch.sh --list-bindings

Binding:
  --api ID               Concrete API channel ID.
  --harness ID           Harness ID.
  --model ID             Exact requested model ID.

Runtime options:
  --effort LEVEL         Optional per-call reasoning effort; not binding identity.
  --dir DIR              Working directory (default: current directory).
  --prompt-file FILE     Prompt input file.
  --out FILE             Capture native stdout; otherwise pass it through.
  --stderr FILE          Capture native stderr; otherwise pass it through.
  --timeout-seconds N    One-shot timeout; 0 disables it (default: 0).
  --dry-run              Validate and print the redacted native command only.
  --list-bindings        List registered API+harness+model bindings.
  --allow-state STATE    Invoke a configured or degraded row once (verify.sh).
                         Not inherited from the environment.

This wrapper does not select models, retry, fallback, rate-limit, resume a
session, change permissions, or create a runlog. Use the native harness for
features not exposed here.
EOF
}

die() {
  printf 'dispatch.sh: %s\n' "$*" >&2
  exit 2
}

need_arg() {
  (($# >= 2)) || die "missing value for $1"
}

is_uint() {
  [[ ${1:-} =~ ^[0-9]+$ ]]
}

validate_binding_registry() {
  [[ -d $LOCAL_ROOT ]] || \
    die "local inventory missing: $LOCAL_ROOT (mount \$AGENTS_DATA_ROOT/local, or copy references/local.example → local, or set AGENTS_LOCAL_ROOT)"
  [[ -r $BINDINGS_FILE ]] || die "binding registry is not readable: $BINDINGS_FILE"
  awk -F '\t' '
    /^#/ || NF == 0 { next }
    NF != 7 { printf "registry line %d must have 7 tab-separated fields\\n", NR > "/dev/stderr"; bad = 1; next }
    $1 == "" || $2 == "" || $3 == "" || $5 == "" || $6 == "" { printf "registry line %d has an empty required field\\n", NR > "/dev/stderr"; bad = 1 }
    $4 !~ /^(configured|verified|degraded)$/ { printf "registry line %d has invalid state: %s\\n", NR, $4 > "/dev/stderr"; bad = 1 }
    $7 !~ /^(yes|no)$/ { printf "registry line %d has invalid wrapper flag: %s\\n", NR, $7 > "/dev/stderr"; bad = 1 }
    { key = $1 SUBSEP $2 SUBSEP $3; if (++seen[key] > 1) { printf "registry line %d duplicates an API+harness+model tuple\\n", NR > "/dev/stderr"; bad = 1 } }
    END { exit bad }
  ' "$BINDINGS_FILE" || die 'binding registry is invalid'
}

binding_data() {
  awk -F '\t' '/^#/ || NF == 0 { next } { print }' "$BINDINGS_FILE"
}

list_bindings() {
  printf 'api\tharness\tmodel\tstate\twrapper\n'
  binding_data | awk -F '\t' '{print $1 "\t" $2 "\t" $3 "\t" $4 "\t" $7}'
}

shell_quote() {
  local arg rendered= quoted=
  for arg in "$@"; do
    printf -v quoted '%q' "$arg"
    rendered+="${rendered:+ }$quoted"
  done
  printf '%s' "$rendered"
}

binding_state_allowed() {
  local state=$1
  [[ $state == verified ]] && return 0
  [[ -n ${ALLOW_STATE:-} && $ALLOW_STATE == "$state" ]] && return 0
  return 1
}

emit_state_override() {
  local dest=$1
  [[ -n $ALLOW_STATE ]] || return 0
  printf 'state_override=%s\n' "$ALLOW_STATE" >&"$dest"
}

maybe_source_nvm() {
  local nvm_script="$LOGIN_HOME/.nvm/nvm.sh"
  if [[ -s $nvm_script ]]; then
    # shellcheck source=/dev/null
    source "$nvm_script"
  fi
}

# Pin every Claude Code alias to the requested non-Grok model so a cc worker
# cannot silently fall onto a Grok or Anthropic model.
set_claude_default_models() {
  CLAUDE_DEFAULT_OPUS=$RESOLVED_MODEL
  CLAUDE_DEFAULT_SONNET=$RESOLVED_MODEL
  CLAUDE_DEFAULT_HAIKU=$RESOLVED_MODEL
  CLAUDE_DEFAULT_FABLE=$RESOLVED_MODEL
}

# Runtime defaults from portable references/runtime-defaults.tsv (first glob match).
# Explicit --effort wins. Not binding identity. Context is calling philosophy.
lookup_runtime_defaults_row() {
  local model=$1
  [[ -r $DEFAULTS_FILE ]] || return 0
  awk -F '\t' -v model="$model" '
    /^#/ || NF == 0 { next }
    NF < 2 { next }
    {
      pat = $1
      gsub(/\*/, ".*", pat)
      if (model ~ ("^" pat "$")) { print $0; exit }
    }
  ' "$DEFAULTS_FILE"
}

default_effort_for_model() {
  local row effort
  row=$(lookup_runtime_defaults_row "$1") || true
  [[ -n $row ]] || { printf ''; return 0; }
  effort=$(printf '%s\n' "$row" | cut -f2)
  [[ $effort != - ]] || effort=
  printf '%s' "$effort"
}

# Returns "TOKENS|AUTOCOMPACT_LABEL". Empty = leave harness default.
default_context_for_model() {
  local row tokens compact
  row=$(lookup_runtime_defaults_row "$1") || true
  [[ -n $row ]] || { printf ''; return 0; }
  tokens=$(printf '%s\n' "$row" | cut -f3)
  compact=$(printf '%s\n' "$row" | cut -f4)
  [[ -n $tokens && $tokens != - && -n $compact && $compact != - ]] || { printf ''; return 0; }
  printf '%s|%s' "$tokens" "$compact"
}

# Local adapter for (api, harness): base_url, credential_ref, token_env
lookup_adapter() {
  local api=$1 harness=$2
  [[ -r $ADAPTERS_FILE ]] || return 1
  awk -F '\t' -v api="$api" -v harness="$harness" '
    /^#/ || NF == 0 { next }
    $1 == api && $2 == harness { print; found = 1; exit }
    END { exit found ? 0 : 1 }
  ' "$ADAPTERS_FILE"
}

while (($#)); do
  case $1 in
    --api) need_arg "$@"; API=$2; shift 2 ;;
    --harness) need_arg "$@"; HARNESS=$2; shift 2 ;;
    --model) need_arg "$@"; MODEL=$2; shift 2 ;;
    --effort) need_arg "$@"; EFFORT=$2; shift 2 ;;
    --dir|--cwd) need_arg "$@"; WORK_DIR=$2; shift 2 ;;
    --prompt-file) need_arg "$@"; PROMPT_FILE=$2; shift 2 ;;
    --out|--stdout) need_arg "$@"; OUT=$2; shift 2 ;;
    --stderr) need_arg "$@"; STDERR_FILE=$2; shift 2 ;;
    --timeout-seconds) need_arg "$@"; TIMEOUT_SECONDS=$2; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --list-bindings) LIST_BINDINGS=true; shift ;;
    --allow-state)
      need_arg "$@"
      ALLOW_STATE=$2
      [[ $ALLOW_STATE == configured || $ALLOW_STATE == degraded ]] || \
        die '--allow-state must be configured or degraded'
      shift 2
      ;;
    --help|-h) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

validate_binding_registry

if [[ $LIST_BINDINGS == true ]]; then
  [[ -z $API && -z $HARNESS && -z $MODEL && -z $PROMPT_FILE ]] || \
    die '--list-bindings cannot be combined with execution options'
  list_bindings
  exit 0
fi

[[ -n $API && -n $HARNESS && -n $MODEL ]] || \
  die 'provide all of --api --harness --model'
[[ $HARNESS != claude-old || $MODEL != grok-* ]] || \
  die 'Grok must use harness=grok-build; claude-old Grok bindings are forbidden'
mapfile -t MATCHES < <(binding_data | awk -F '\t' \
  -v api="$API" -v harness="$HARNESS" -v model="$MODEL" \
  '$1 == api && $2 == harness && $3 == model')
if ((${#MATCHES[@]} != 1)); then
    die "unregistered API+harness+model binding: api=$API harness=$HARNESS model=$MODEL; no fallback"
  fi

IFS=$'\t' read -r RESOLVED_API RESOLVED_HARNESS RESOLVED_MODEL BINDING_STATE \
  SELECTOR CATALOG_FILE WRAPPER_SUPPORTED <<<"${MATCHES[0]}"
[[ $WRAPPER_SUPPORTED == yes ]] || \
  die 'binding is host-only; use the native Codex sub-agent tool'
if ! binding_state_allowed "$BINDING_STATE"; then
  die "binding state is $BINDING_STATE; normal dispatch and dry-run accept verified bindings only"
fi

if [[ -z $EFFORT ]]; then
  EFFORT=$(default_effort_for_model "$RESOLVED_MODEL")
fi
if [[ -n $EFFORT ]]; then
  [[ $EFFORT =~ ^(none|low|medium|high|xhigh|max|ultra)$ ]] || \
    die 'unsupported effort value'
fi
[[ $RESOLVED_HARNESS != dsh || -z $EFFORT ]] || \
  die 'dsh headless does not expose per-call effort'
[[ $RESOLVED_MODEL != claude-haiku-4-5 || -z $EFFORT ]] || \
  die 'claude-haiku-4-5 does not support effort'

[[ -n $PROMPT_FILE ]] || die '--prompt-file is required'
[[ -f $PROMPT_FILE && -r $PROMPT_FILE ]] || \
  die "prompt file must be a readable regular file: $PROMPT_FILE"
[[ -d $WORK_DIR ]] || die "working directory does not exist: $WORK_DIR"
WORK_DIR=$(cd -- "$WORK_DIR" && pwd -P)
is_uint "$TIMEOUT_SECONDS" || die '--timeout-seconds must be a non-negative integer'
[[ -z $OUT || $OUT != "$STDERR_FILE" ]] || die '--out and --stderr must be different files'

COMMAND=()
DISPLAY_COMMAND=()
CATALOG_PATH=
PROFILE=
SELECTED_MODEL=
CLAUDE_MODEL=
CLAUDE_DEFAULT_OPUS=
CLAUDE_DEFAULT_SONNET=
CLAUDE_DEFAULT_HAIKU=
CLAUDE_DEFAULT_FABLE=
GROK_OVERRIDE_HOME=

case $RESOLVED_HARNESS in
  codex-cli)
    case $SELECTOR in
      model:*)
        [[ $CATALOG_FILE != - ]] || die 'Codex binding has no catalog entry'
        CATALOG_PATH="$CODEX_CATALOG_DIR/model-catalogs/$CATALOG_FILE"
        DISPLAY_COMMAND=(codex exec -m "$RESOLVED_MODEL")
        ;;
      profile:*|profile-model:*)
        if [[ $SELECTOR == profile-model:* ]]; then
          PROFILE_AND_MODEL=${SELECTOR#profile-model:}
          PROFILE=${PROFILE_AND_MODEL%%:*}
          SELECTED_MODEL=${PROFILE_AND_MODEL#*:}
        else
          PROFILE=${SELECTOR#profile:}
        fi
        [[ $CATALOG_FILE != - ]] || die 'Codex binding has no catalog entry'
        CATALOG_PATH="$CODEX_CATALOG_DIR/model-catalogs/$CATALOG_FILE"
        DISPLAY_COMMAND=(codex exec -p "$PROFILE")
        [[ -z $SELECTED_MODEL ]] || DISPLAY_COMMAND+=(--model "$SELECTED_MODEL")
        ;;
      *) die 'invalid Codex selector' ;;
    esac
    [[ -z $EFFORT ]] || DISPLAY_COMMAND+=(-c "model_reasoning_effort=$EFFORT")
    DISPLAY_COMMAND+=(-c "model_catalog_json=$CATALOG_PATH" \
      -C "$WORK_DIR" --skip-git-repo-check - '<' "$PROMPT_FILE")
    ;;
  dsh)
    # Kept so a future dsh install can reuse this branch; this machine has no dsh
    # as of 2026-09-03 and the registry no longer lists a dsh binding.
    DISPLAY_COMMAND=(env "DSH_HOME=$DSH_RUNTIME_HOME" "DSH_AGENTS_HOME=$PLUGIN_ROOT" \
      dsh --profile headless '<prompt-file>')
    ;;
  claude-old)
    CLAUDE_MODEL=${SELECTOR#claude:}
    set_claude_default_models
    CLAUDE_OLD_BIN=${CLAUDE_OLD_BIN:-claude_old}
    CLAUDE_ADAPTER_ROW=$(lookup_adapter "$RESOLVED_API" claude-old) || \
      die "no local adapter for api=$RESOLVED_API harness=claude-old in $ADAPTERS_FILE"
    IFS=$'\t' read -r _ADAPTER_API _ADAPTER_HARNESS CLAUDE_BASE_URL CLAUDE_CRED_REF CLAUDE_TOKEN_ENV \
      <<<"$CLAUDE_ADAPTER_ROW"
    [[ -n $CLAUDE_BASE_URL && -n $CLAUDE_CRED_REF ]] || die 'adapter row missing invoke_base_url or credential_ref'
    [[ -n $CLAUDE_TOKEN_ENV ]] || CLAUDE_TOKEN_ENV=ANTHROPIC_AUTH_TOKEN
    CLAUDE_CONTEXT_SPEC=$(default_context_for_model "$RESOLVED_MODEL")
    CLAUDE_CONTEXT_TOKENS=
    CLAUDE_AUTOCOMPACT=auto
    if [[ -n $CLAUDE_CONTEXT_SPEC ]]; then
      CLAUDE_CONTEXT_TOKENS=${CLAUDE_CONTEXT_SPEC%%|*}
      CLAUDE_AUTOCOMPACT=${CLAUDE_CONTEXT_SPEC#*|}
    fi
    DISPLAY_COMMAND=(env DISABLE_AUTOUPDATER=1 \
      ANTHROPIC_BASE_URL="$CLAUDE_BASE_URL" \
      ANTHROPIC_DEFAULT_OPUS_MODEL="$CLAUDE_DEFAULT_OPUS" \
      ANTHROPIC_DEFAULT_SONNET_MODEL="$CLAUDE_DEFAULT_SONNET" \
      ANTHROPIC_DEFAULT_HAIKU_MODEL="$CLAUDE_DEFAULT_HAIKU" \
      ANTHROPIC_DEFAULT_FABLE_MODEL="$CLAUDE_DEFAULT_FABLE" \
      ANTHROPIC_DEFAULT_OPUS_MODEL_NAME="$CLAUDE_DEFAULT_OPUS" \
      ANTHROPIC_DEFAULT_SONNET_MODEL_NAME="$CLAUDE_DEFAULT_SONNET" \
      ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME="$CLAUDE_DEFAULT_HAIKU" \
      ANTHROPIC_DEFAULT_FABLE_MODEL_NAME="$CLAUDE_DEFAULT_FABLE" \
      CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1 \
      CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1)
    [[ -z $CLAUDE_CONTEXT_TOKENS ]] || \
      DISPLAY_COMMAND+=(CLAUDE_CODE_MAX_CONTEXT_TOKENS="$CLAUDE_CONTEXT_TOKENS")
    DISPLAY_COMMAND+=("$CLAUDE_OLD_BIN" --model "$CLAUDE_MODEL" \
      --autocompact "$CLAUDE_AUTOCOMPACT" --output-format text)
    [[ -z $EFFORT ]] || DISPLAY_COMMAND+=(--effort "$EFFORT")
    DISPLAY_COMMAND+=(-p '<' "$PROMPT_FILE")
    ;;
  grok-build)
    if [[ $RESOLVED_API == grok-direct-api ]]; then
      GROK_OVERRIDE_HOME=
      DISPLAY_COMMAND=(grok --cwd "$WORK_DIR" --model "$RESOLVED_MODEL")
    else
      GROK_OVERRIDE_HOME=$GROK_RUNTIME_HOME
      DISPLAY_COMMAND=(env "GROK_HOME=$GROK_RUNTIME_HOME" grok --cwd "$WORK_DIR" \
        --model "$RESOLVED_MODEL")
    fi
    [[ -z $EFFORT ]] || DISPLAY_COMMAND+=(--reasoning-effort "$EFFORT")
    DISPLAY_COMMAND+=(--output-format json --prompt-file "$PROMPT_FILE")
    ;;
  *) die "unsupported harness: $RESOLVED_HARNESS" ;;
esac

if [[ $DRY_RUN == true ]]; then
  printf 'api=%s\nharness=%s\nmodel=%s\nstate=%s\neffort=%s\n' \
    "$RESOLVED_API" "$RESOLVED_HARNESS" "$RESOLVED_MODEL" "$BINDING_STATE" "${EFFORT:-default}"
  printf 'cwd=%s\nprompt_file=%s\n' "$WORK_DIR" "$PROMPT_FILE"
  emit_state_override 1
  printf 'command=%s\n' "$(shell_quote "${DISPLAY_COMMAND[@]}")"
  exit 0
fi

emit_state_override 2

set +x
[[ -r $CREDENTIALS_FILE ]] || die "credentials file is not readable: $CREDENTIALS_FILE (mount \$AGENTS_DATA_ROOT/private/credentials.env)"
set -a
# shellcheck source=/dev/null
source "$CREDENTIALS_FILE"
set +a

if [[ $RESOLVED_HARNESS == codex-cli || $RESOLVED_HARNESS == dsh ]]; then
  maybe_source_nvm
fi

case $RESOLVED_HARNESS in
  codex-cli)
    # Profiles live in the login-home ~/.codex; this is config, not binary lookup.
    COMMAND=(env HOME="$LOGIN_HOME" codex exec)
    if [[ -n $PROFILE ]]; then
      COMMAND+=(-p "$PROFILE")
      [[ -z $SELECTED_MODEL ]] || COMMAND+=(--model "$SELECTED_MODEL")
    else
      COMMAND+=(-m "$RESOLVED_MODEL")
    fi
    [[ -z $EFFORT ]] || COMMAND+=(-c "model_reasoning_effort=$EFFORT")
    COMMAND+=(-c "model_catalog_json=$CATALOG_PATH" \
      -C "$WORK_DIR" --skip-git-repo-check -)
    command -v codex >/dev/null 2>&1 || die 'executable not found: codex'
    ;;
  dsh)
    COMMAND=(env "DSH_HOME=$DSH_RUNTIME_HOME" "DSH_AGENTS_HOME=$PLUGIN_ROOT" \
      dsh --profile headless "$(<"$PROMPT_FILE")")
    command -v dsh >/dev/null 2>&1 || die 'executable not found: dsh'
    ;;
  claude-old)
    command -v "$CLAUDE_OLD_BIN" >/dev/null 2>&1 || die "executable not found: $CLAUDE_OLD_BIN"
    [[ -v $CLAUDE_CRED_REF ]] || \
      die "credentials file does not define required variable: $CLAUDE_CRED_REF"
    CLAUDE_AUTH_TOKEN=${!CLAUDE_CRED_REF}
    COMMAND=("$CLAUDE_OLD_BIN" --model "$CLAUDE_MODEL" \
      --autocompact "$CLAUDE_AUTOCOMPACT" --output-format text)
    [[ -z $EFFORT ]] || COMMAND+=(--effort "$EFFORT")
    COMMAND+=(-p)
    ;;
  grok-build)
    if [[ -n $GROK_OVERRIDE_HOME ]]; then
      COMMAND=(env "GROK_HOME=$GROK_OVERRIDE_HOME" grok --cwd "$WORK_DIR" \
        --model "$RESOLVED_MODEL")
    else
      COMMAND=(grok --cwd "$WORK_DIR" --model "$RESOLVED_MODEL")
    fi
    [[ -z $EFFORT ]] || COMMAND+=(--reasoning-effort "$EFFORT")
    COMMAND+=(--output-format json --prompt-file "$PROMPT_FILE")
    command -v grok >/dev/null 2>&1 || die 'executable not found: grok'
    ;;
esac

[[ -z $OUT ]] || mkdir -p -- "$(dirname -- "$OUT")"
[[ -z $STDERR_FILE ]] || mkdir -p -- "$(dirname -- "$STDERR_FILE")"

invoke() {
  local -a prefix=()
  ((TIMEOUT_SECONDS == 0)) || prefix=(timeout --signal=TERM --kill-after=5 "$TIMEOUT_SECONDS")
  case $RESOLVED_HARNESS in
    codex-cli) "${prefix[@]}" "${COMMAND[@]}" <"$PROMPT_FILE" ;;
    dsh) (cd -- "$WORK_DIR" && "${prefix[@]}" "${COMMAND[@]}") ;;
    claude-old)
      (
        cd -- "$WORK_DIR"
        export DISABLE_AUTOUPDATER=1
        export ANTHROPIC_BASE_URL="$CLAUDE_BASE_URL"
        export "$CLAUDE_TOKEN_ENV=$CLAUDE_AUTH_TOKEN"
        export ANTHROPIC_DEFAULT_OPUS_MODEL="$CLAUDE_DEFAULT_OPUS"
        export ANTHROPIC_DEFAULT_SONNET_MODEL="$CLAUDE_DEFAULT_SONNET"
        export ANTHROPIC_DEFAULT_HAIKU_MODEL="$CLAUDE_DEFAULT_HAIKU"
        export ANTHROPIC_DEFAULT_FABLE_MODEL="$CLAUDE_DEFAULT_FABLE"
        export ANTHROPIC_DEFAULT_OPUS_MODEL_NAME="$CLAUDE_DEFAULT_OPUS"
        export ANTHROPIC_DEFAULT_SONNET_MODEL_NAME="$CLAUDE_DEFAULT_SONNET"
        export ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME="$CLAUDE_DEFAULT_HAIKU"
        export ANTHROPIC_DEFAULT_FABLE_MODEL_NAME="$CLAUDE_DEFAULT_FABLE"
        export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
        export CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1
        if [[ -n $CLAUDE_CONTEXT_TOKENS ]]; then
          export CLAUDE_CODE_MAX_CONTEXT_TOKENS="$CLAUDE_CONTEXT_TOKENS"
        fi
        "${prefix[@]}" "${COMMAND[@]}"
      ) <"$PROMPT_FILE"
      ;;
    grok-build) "${prefix[@]}" "${COMMAND[@]}" ;;
  esac
}

if [[ -n $OUT && -n $STDERR_FILE ]]; then
  invoke >"$OUT" 2>"$STDERR_FILE"
elif [[ -n $OUT ]]; then
  invoke >"$OUT"
elif [[ -n $STDERR_FILE ]]; then
  invoke 2>"$STDERR_FILE"
else
  invoke
fi
