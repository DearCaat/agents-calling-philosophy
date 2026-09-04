#!/usr/bin/env bash
# Shared machine-local data root for Claude Code / Codex / Grok.
#
# Layout (one directory for all harnesses):
#   $AGENTS_DATA_ROOT/local/                 inventory (bindings, adapters, …)
#   $AGENTS_DATA_ROOT/private/credentials.env
#   $AGENTS_DATA_ROOT/private/runtime/…      optional harness runtime homes
#
# Precedence for DATA_ROOT:
#   1. AGENTS_DATA_ROOT
#   2. CLAUDE_PLUGIN_DATA | PLUGIN_DATA | GROK_PLUGIN_DATA (first non-empty)
#   3. empty → caller falls back to in-tree PLUGIN_ROOT / references/local
#
# Overrides:
#   AGENTS_LOCAL_ROOT, AGENTS_CREDENTIALS_FILE, AGENTS_CODEX_CATALOG_DIR

agents_resolve_data_root() {
  if [[ -n ${AGENTS_DATA_ROOT:-} ]]; then
    printf '%s\n' "$AGENTS_DATA_ROOT"
    return 0
  fi
  local candidate
  for candidate in "${CLAUDE_PLUGIN_DATA:-}" "${PLUGIN_DATA:-}" "${GROK_PLUGIN_DATA:-}"; do
    if [[ -n $candidate ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  printf '\n'
  return 0
}

# Requires SKILL_DIR and PLUGIN_ROOT already set by the caller.
agents_apply_data_root() {
  local data_root
  data_root=$(agents_resolve_data_root)

  if [[ -n $data_root ]]; then
    AGENTS_RESOLVED_DATA_ROOT=$data_root
    LOCAL_ROOT=${AGENTS_LOCAL_ROOT:-$data_root/local}
    CREDENTIALS_FILE=${AGENTS_CREDENTIALS_FILE:-$data_root/private/credentials.env}
    CODEX_CATALOG_DIR=${AGENTS_CODEX_CATALOG_DIR:-$data_root/private/runtime/codex-home}
    DSH_RUNTIME_HOME=${AGENTS_DSH_HOME:-$data_root/private/runtime/dsh-home}
    GROK_RUNTIME_HOME=${AGENTS_GROK_HOME:-$data_root/private/runtime/grok-home/.grok}
  else
    AGENTS_RESOLVED_DATA_ROOT=
    LOCAL_ROOT=${AGENTS_LOCAL_ROOT:-$SKILL_DIR/references/local}
    CREDENTIALS_FILE=${AGENTS_CREDENTIALS_FILE:-$PLUGIN_ROOT/private/credentials.env}
    CODEX_CATALOG_DIR=${AGENTS_CODEX_CATALOG_DIR:-$PLUGIN_ROOT/private/runtime/codex-home}
    DSH_RUNTIME_HOME=${AGENTS_DSH_HOME:-$PLUGIN_ROOT/private/runtime/dsh-home}
    GROK_RUNTIME_HOME=${AGENTS_GROK_HOME:-$PLUGIN_ROOT/private/runtime/grok-home/.grok}
  fi
}
