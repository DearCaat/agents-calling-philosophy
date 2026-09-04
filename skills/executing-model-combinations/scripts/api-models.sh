#!/usr/bin/env bash

# Read the current /models catalogue of one explicitly selected API route.
# It is discovery only: it never invokes a model or chooses a harness binding.

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
SKILL_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
PLUGIN_ROOT=$(cd -- "$SKILL_DIR/../.." && pwd -P)
# shellcheck source=lib/data-root.sh
source "$SCRIPT_DIR/lib/data-root.sh"
agents_apply_data_root
ROUTES_FILE="$LOCAL_ROOT/api-routes.tsv"

API=
LIST_ROUTES=false
TIMEOUT_SECONDS=20

usage() {
  cat <<'EOF'
Usage:
  api-models.sh --api API_ID [--timeout-seconds N]
  api-models.sh --list-routes

Reads only the selected API's configured /models route and prints model IDs.
It does not invoke a model, choose a harness, or create a binding.
EOF
}

die() {
  printf 'api-models.sh: %s\n' "$*" >&2
  exit 2
}

need_arg() {
  (($# >= 2)) || die "missing value for $1"
}

is_uint() {
  [[ ${1:-} =~ ^[0-9]+$ ]]
}

validate_routes() {
  [[ -r $ROUTES_FILE ]] || die "route registry is not readable: $ROUTES_FILE"
  awk -F '\t' '
    /^#/ || NF == 0 { next }
    NF != 8 { printf "route registry line %d must have 8 tab-separated fields\\n", NR > "/dev/stderr"; bad = 1; next }
    $1 == "" || $2 == "" || $3 == "" || $4 == "" || $5 == "" || $6 == "" || $7 == "" {
      printf "route registry line %d has an empty required field\\n", NR > "/dev/stderr"; bad = 1
    }
    $7 !~ /^(available|degraded|unavailable)$/ {
      printf "route registry line %d has invalid state: %s\\n", NR, $7 > "/dev/stderr"; bad = 1
    }
    { if (++seen[$1] > 1) { printf "route registry line %d duplicates api_id: %s\\n", NR, $1 > "/dev/stderr"; bad = 1 } }
    END { exit bad }
  ' "$ROUTES_FILE" || die 'route registry is invalid'
}

while (($#)); do
  case $1 in
    --api) need_arg "$@"; API=$2; shift 2 ;;
    --timeout-seconds) need_arg "$@"; TIMEOUT_SECONDS=$2; shift 2 ;;
    --list-routes) LIST_ROUTES=true; shift ;;
    --help|-h) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

validate_routes
is_uint "$TIMEOUT_SECONDS" || die '--timeout-seconds must be a non-negative integer'

if [[ $LIST_ROUTES == true ]]; then
  [[ -z $API ]] || die '--list-routes cannot be combined with --api'
  awk -F '\t' 'BEGIN { print "api\tbase_url\tprotocol\tcredential_ref\tmodels_path\tstate" }
    /^#/ || NF == 0 { next }
    { print $1 "\t" $2 "\t" $3 "\t" $4 "\t" $5 "\t" $7 }' "$ROUTES_FILE"
  exit 0
fi

[[ -n $API ]] || die 'provide --api API_ID'
mapfile -t MATCHES < <(awk -F '\t' -v api="$API" '/^#/ || NF == 0 { next } $1 == api { print }' "$ROUTES_FILE")
((${#MATCHES[@]} == 1)) || die 'unregistered API route'
IFS=$'\t' read -r RESOLVED_API BASE_URL WIRE_PROTOCOL CREDENTIAL_REF MODELS_PATH AUTH_SCHEME ROUTE_STATE NOTES <<<"${MATCHES[0]}"

[[ $ROUTE_STATE == available || $ROUTE_STATE == degraded ]] || \
  die "no safe generic models route is registered for $RESOLVED_API"
[[ $WIRE_PROTOCOL == responses && $AUTH_SCHEME == bearer && $MODELS_PATH == /models ]] || \
  die "route metadata for $RESOLVED_API is not supported by this read-only helper"
[[ $CREDENTIAL_REF =~ ^[A-Z][A-Z0-9_]*$ ]] || die 'invalid credential reference in route registry'

set +x
[[ -r $CREDENTIALS_FILE ]] || die "credentials file is not readable: $CREDENTIALS_FILE (mount \$AGENTS_DATA_ROOT/private/credentials.env)"
set -a
# shellcheck source=/dev/null
source "$CREDENTIALS_FILE"
set +a
API_TOKEN=${!CREDENTIAL_REF-}
[[ -n $API_TOKEN ]] || die "credentials file does not define required variable: $CREDENTIAL_REF"

RAW_RESPONSE=$(curl --fail --silent --show-error --max-time "$TIMEOUT_SECONDS" \
  --header "Authorization: Bearer $API_TOKEN" \
  "$BASE_URL$MODELS_PATH") || die 'models request failed'

printf 'api=%s\nbase_url=%s\nprotocol=%s\ncredential_ref=%s\n' \
  "$RESOLVED_API" "$BASE_URL" "$WIRE_PROTOCOL" "$CREDENTIAL_REF"
if ! jq -er '.data | arrays | .[] | .id | strings' <<<"$RAW_RESPONSE" | sort -u; then
  die 'models response did not contain OpenAI-compatible data[].id values'
fi
