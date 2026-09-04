# agents-calling-philosophy

Portable **calling philosophy** plugin for choosing harness + model, effort/context defaults, and native sub-agent dispatch. Machine credentials and live bindings stay out of this repo.

## What this is

- Skill: `skills/executing-model-combinations/`
- Philosophy + defaults: `references/{overview,models,harnesses,runtime-defaults.tsv}`
- Wrappers: `scripts/` (read `AGENTS_LOCAL_ROOT`, default `references/local/`)
- Empty machine overlay template: `references/local.example/`

## What this is not

- No `private/` credentials or runtime catalogs
- No filled `references/local/` inventory (bindings, adapters, live API URLs)

## 安装

本仓库同时是 Claude Code marketplace（`.claude-plugin/marketplace.json`）和 Codex marketplace（`.agents/plugins/marketplace.json`），市场名均为 `agents-calling-philosophy`。

### Claude Code

```bash
claude plugin marketplace add DearCaat/agents-calling-philosophy
claude plugin install agents@agents-calling-philosophy
```

（`owner/repo` 简写等价于 `https://github.com/DearCaat/agents-calling-philosophy.git`，SSH URL 亦可。）安装后 `/reload-plugins` 或开新会话生效。

### Codex

```bash
codex plugin marketplace add DearCaat/agents-calling-philosophy --ref main
codex plugin add agents@agents-calling-philosophy
```

安装后开一个新的 Codex session。

### Grok Build

```bash
grok plugin marketplace add DearCaat/agents-calling-philosophy
grok plugin install agents --trust
```

### 更新

发布新版本：bump `.claude-plugin/plugin.json` 与 `.codex-plugin/plugin.json` 的 `version` 并推送到 `main`。已安装用户执行：

```bash
# Claude Code
claude plugin marketplace update agents-calling-philosophy
claude plugin update agents@agents-calling-philosophy

# Codex
codex plugin marketplace upgrade agents-calling-philosophy
codex plugin add agents@agents-calling-philosophy
```

然后开新 session（或 Claude Code 里 `/reload-plugins`）加载更新。

## Use on another machine

1. Install this marketplace plugin (commands above).
2. `cp -R skills/executing-model-combinations/references/local.example skills/executing-model-combinations/references/local`  
   （或对已安装副本设置 `AGENTS_LOCAL_ROOT` 指向本机 overlay。）
3. Fill `local/bindings.tsv`, `adapters.tsv`, `apis.md`, and plugin `private/credentials.env` on that machine.
4. Follow `SKILL.md` reading order. Missing bindings → report gap, do not silently swap harnesses.

Context/effort policy lives in `runtime-defaults.tsv` (calling philosophy); `dispatch.sh` reads the same table.
