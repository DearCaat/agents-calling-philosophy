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

# 首次：从已安装插件拷模板
PLUGIN=$(claude plugin list 2>/dev/null | true)  # 或手动定位 cache / marketplace 路径
cp -R /path/to/agents/skills/calling-models/references/local.example/. \
  "$AGENTS_DATA_ROOT/local/"

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
    runtime/…            # 可选
```

解析顺序（SKILL.md §1）：

1. `AGENTS_DATA_ROOT`（推荐）
2. `CLAUDE_PLUGIN_DATA` / `PLUGIN_DATA` / `GROK_PLUGIN_DATA`
3. 插件树内 `references/local` + `private/`（兼容目录版安装）

细覆盖：`AGENTS_LOCAL_ROOT`、`AGENTS_CREDENTIALS_FILE`、`AGENTS_CODEX_CATALOG_DIR`。

## 更新（只更新哲学，不动本机数据）

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
`$AGENTS_DATA_ROOT` 在插件 cache 之外，**update / 重装哲学包不会覆盖** 本机清单与 credentials。

## 仓库里有什么

- Skill: `skills/calling-models/`（`SKILL.md`，入口表的查法与两个客户端）
- Reference: `references/llm-call.md`、`references/tools.md`
- Scripts: `scripts/`（`llm-call.py`、`tool-call.py`、旧包装 `jev-call.py`、`tools/`）
- Overlay template: `references/local.example/`（`api-routes.tsv`、`harnesses.md`、`entries.md`）

## 仓库里没有什么

- 无 `private/` 凭据或 runtime catalog
- 无填好的 `references/local/` inventory
