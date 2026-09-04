# agents-calling-philosophy

Portable **calling philosophy** for choosing harness + model, effort/context defaults, and native sub-agent dispatch.

## What this is

- Skill: `skills/executing-model-combinations/`
- Philosophy + defaults: `references/{overview,models,harnesses,runtime-defaults.tsv}`
- Wrappers: `scripts/` (read `AGENTS_LOCAL_ROOT`, default `references/local/`)
- Empty machine overlay template: `references/local.example/`

## What this is not

- No `private/` credentials or runtime catalogs
- No filled `references/local/` inventory (bindings, adapters, live API URLs)

## Use on another machine

1. Install / load this plugin skill.
2. `cp -R skills/executing-model-combinations/references/local.example skills/executing-model-combinations/references/local`
3. Fill `local/bindings.tsv`, `adapters.tsv`, `apis.md`, and plugin `private/credentials.env` on that machine.
4. Follow `SKILL.md` reading order. Missing bindings → report gap, do not silently swap harnesses.

Context/effort policy lives in `runtime-defaults.tsv` (calling philosophy); `dispatch.sh` reads the same table.
