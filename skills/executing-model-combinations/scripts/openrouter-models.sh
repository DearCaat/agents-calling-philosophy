#!/usr/bin/env bash

# Read-only candidate discovery from OpenRouter. Output is not a local binding.

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
SKILL_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
PLUGIN_ROOT=$(cd -- "$SKILL_DIR/../.." && pwd -P)
# shellcheck source=lib/data-root.sh
source "$SCRIPT_DIR/lib/data-root.sh"
agents_apply_data_root
BASE_URL=${OPENROUTER_BASE_URL:-https://openrouter.ai/api/v1}

MODE=${1:-}
[[ -z $MODE ]] || shift
LIMIT=20
REQUIRES=
TERM=

usage() {
  cat <<'EOF'
Usage:
  openrouter-models.sh newest [--requires a,b] [--limit N]
  openrouter-models.sh popular [--requires a,b] [--limit N]
  openrouter-models.sh search TERM [--requires a,b] [--limit N]
  openrouter-models.sh inspect AUTHOR/MODEL

Modes:
  newest     Sort by models most recently added to OpenRouter.
  popular    Sort by tokens processed through OpenRouter in the last week.
  search     Filter the OpenRouter model catalog by ID or display name.
  inspect    Return selected metadata for one exact OpenRouter model ID.

This tool reports OpenRouter catalog facts only. It does not rank model
quality or prove that a model works through a local API or harness.
EOF
}

die() {
  printf 'openrouter-models.sh: %s\n' "$*" >&2
  exit 2
}

case $MODE in
  newest|popular) ;;
  search|inspect)
    [[ $# -gt 0 ]] || die "$MODE requires a value"
    TERM=$1
    shift
    ;;
  --help|-h|'') usage; [[ -n $MODE ]] || exit 2; exit 0 ;;
  *) die "unknown mode: $MODE" ;;
esac

while (($#)); do
  case $1 in
    --limit)
      (($# >= 2)) || die 'missing value for --limit'
      LIMIT=$2
      shift 2
      ;;
    --requires)
      (($# >= 2)) || die 'missing value for --requires'
      REQUIRES=$2
      shift 2
      ;;
    --help|-h) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ $LIMIT =~ ^[0-9]+$ ]] && ((LIMIT > 0 && LIMIT <= 1000)) || \
  die '--limit must be an integer from 1 to 1000'
[[ -z $REQUIRES || $REQUIRES =~ ^[a-z_]+(,[a-z_]+)*$ ]] || \
  die '--requires must be a comma-separated parameter list'
[[ $MODE != inspect || $LIMIT == 20 ]] || die '--limit does not apply to inspect'
[[ $MODE != inspect || -z $REQUIRES ]] || die '--requires does not apply to inspect'

command -v curl >/dev/null 2>&1 || die 'curl is required'
command -v jq >/dev/null 2>&1 || die 'jq is required'

set +x
if [[ -r $CREDENTIALS_FILE ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$CREDENTIALS_FILE"
  set +a
fi

CURL_ARGS=(--fail --silent --show-error --location \
  --header 'Accept: application/json')
if [[ -n ${OPENROUTER_API_KEY:-} ]]; then
  CURL_ARGS+=(--header "Authorization: Bearer $OPENROUTER_API_KEY")
fi

fetch() {
  curl "${CURL_ARGS[@]}" "$1"
}

list_models() {
  jq -r --argjson limit "$LIMIT" '
    def iso:
      if type == "number" then todateiso8601 else "unknown" end;
    def per_million:
      if . == null then "unknown" else ((tonumber * 1000000) | tostring) end;
    (["id", "created", "context", "input_$/M", "output_$/M", "parameters"] | @tsv),
    (.data[0:$limit][] |
      [.id,
       (.created | iso),
       ((.context_length // "unknown") | tostring),
       (.pricing.prompt | per_million),
       (.pricing.completion | per_million),
       ((.supported_parameters // []) | join(","))] | @tsv)
  '
}

requires_query=
[[ -z $REQUIRES ]] || requires_query="&supported_parameters=$REQUIRES"

case $MODE in
  newest)
    fetch "$BASE_URL/models?sort=newest&limit=$LIMIT$requires_query" | list_models
    ;;
  popular)
    fetch "$BASE_URL/models?sort=top-weekly&limit=$LIMIT$requires_query" | list_models
    ;;
  search)
    fetch "$BASE_URL/models?limit=1000$requires_query" | \
      jq --arg q "${TERM,,}" '{data: [.data[] | select(
        ((.id // "") | ascii_downcase | contains($q)) or
        ((.name // "") | ascii_downcase | contains($q))
      )]}' | list_models
    ;;
  inspect)
    fetch "$BASE_URL/model/$TERM" | jq '{
      id: .data.id,
      canonical_slug: .data.canonical_slug,
      name: .data.name,
      created: .data.created,
      expiration_date: .data.expiration_date,
      architecture: .data.architecture,
      context_length: .data.context_length,
      top_provider: .data.top_provider,
      pricing: .data.pricing,
      supported_parameters: .data.supported_parameters,
      benchmarks: .data.benchmarks
    }'
    ;;
esac
