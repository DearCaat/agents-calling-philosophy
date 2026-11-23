# agents-calling-philosophy

Portable **model calling** plugin: a local entry table (`entries.md`) plus two JSONL clients: `llm-call.py` for direct LLM APIs and `tool-call.py` for gateway tools (jev, vela, mineru, oxr).

本机 inventory 与凭据**不进本仓库**；挂在一个共享数据目录里，Claude Code / Codex / Grok 共用。

## 安装

本仓库同时是 Claude Code marketplace（`.claude-plugin/marketplace.json`）和 Codex marketplace（`.agents/plugins/marketplace.json`），市场名均为 `agents-calling-philosophy`。

### Claude Code

```bash
claude plugin marketplace add DearCaat/agents-calling-philosophy
claude plugin install agents@agents-calling-philosophy
```

安装后 `/reload-plugins` 或开新会话生效。

### Codex

```bash
codex plugin marketplace add DearCaat/agents-calling-philosophy --ref main
codex plugin add agents@agents-calling-philosophy
```

### Grok Build

```bash
grok plugin marketplace add DearCaat/agents-calling-philosophy
grok plugin install agents --trust
```

## 挂载本机数据（必做一次）

三家 harness **解析同一目录**：

```bash
export AGENTS_DATA_ROOT=$HOME/.agents/calling-data
mkdir -p "$AGENTS_DATA_ROOT/local" "$AGENTS_DATA_ROOT/private"

# 首次：拷入模板。<插件目录> 换成本机已安装的 agents 插件根目录（其下应有 skills/calling-models/）
cp -R <插件目录>/skills/calling-models/references/local.example/. "$AGENTS_DATA_ROOT/local/"

# 填写 inventory；凭据只写这一处（不要提交）：
#   $AGENTS_DATA_ROOT/private/credentials.env
```

建议写入 shell 配置（`~/.bashrc` / 各 harness 启动环境），保证 CC / Codex / Grok 进程都能看到 `AGENTS_DATA_ROOT`。

目录布局：

```text
$AGENTS_DATA_ROOT/
  local/                 # entries.md、api-routes.tsv、harnesses.md
  private/
    credentials.env
```

解析顺序（SKILL.md §1）：

1. `AGENTS_DATA_ROOT`（推荐）
2. `CLAUDE_PLUGIN_DATA` / `PLUGIN_DATA` / `GROK_PLUGIN_DATA`
3. 插件树内 `references/local` + `private/`（兼容目录版安装）

细覆盖：`AGENTS_LOCAL_ROOT`、`AGENTS_CREDENTIALS_FILE`。

## 更新（只更新插件，不动本机数据）

发布新版本：bump `.claude-plugin/plugin.json` 与 `.codex-plugin/plugin.json` 的 `version` 并推 `main`。已安装用户：

```bash
# Claude Code
claude plugin marketplace update agents-calling-philosophy
claude plugin update agents@agents-calling-philosophy

# Codex
codex plugin marketplace upgrade agents-calling-philosophy
codex plugin add agents@agents-calling-philosophy
```

然后开新 session（或 Claude Code `/reload-plugins`）。  
`$AGENTS_DATA_ROOT` 在插件 cache 之外，**update / 重装插件不会覆盖** 本机清单与 credentials。

## 改动何时生效

入口表（`local/entries.md`）、路由表（`local/api-routes.tsv`）、`local/harnesses.md` 在数据目录里，调用时现读，改了立即生效。SKILL.md、references、脚本随插件版本走，更新后要 `/reload-plugins` 或开新会话。已打开的会话在那之前用的是旧版，`claude plugin list` 可看已安装版本。

## 仓库里有什么

- Skill: `skills/calling-models/`（`SKILL.md`，入口表的查法与两个客户端）
- Reference: `references/llm-call.md`、`references/tools.md`
- Scripts: `scripts/`（`llm-call.py`、`tool-call.py`、旧包装 `jev-call.py`、`tools/`）
- Overlay template: `references/local.example/`（`api-routes.tsv`、`harnesses.md`、`entries.md`）

## 仓库里没有什么

- 无 `private/` 凭据
- 无填好的 `references/local/` inventory
