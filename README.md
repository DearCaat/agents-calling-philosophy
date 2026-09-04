# agents-calling-philosophy

Portable **calling philosophy** plugin for choosing harness + model, effort/context defaults, and native sub-agent dispatch.

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
cp -R /path/to/agents/skills/executing-model-combinations/references/local.example/. \
  "$AGENTS_DATA_ROOT/local/"

# 填写 inventory；凭据只写这一处（不要提交）：
#   $AGENTS_DATA_ROOT/private/credentials.env
```

建议写入 shell 配置（`~/.bashrc` / 各 harness 启动环境），保证 CC / Codex / Grok 进程都能看到 `AGENTS_DATA_ROOT`。

目录布局：

```text
$AGENTS_DATA_ROOT/
  local/                 # bindings.tsv、adapters.tsv、apis.md…
  private/
    credentials.env
    runtime/…            # 可选
```

解析顺序（`scripts/lib/data-root.sh`）：

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
`$AGENTS_DATA_ROOT` 在插件 cache 之外，**update / 重装哲学包不会覆盖** bindings 与 credentials。

## 仓库里有什么

- Skill: `skills/executing-model-combinations/`
- Philosophy + defaults: `references/{overview,models,harnesses,runtime-defaults.tsv}`
- Wrappers: `scripts/`（含 `lib/data-root.sh`）
- Empty overlay template: `references/local.example/`

## 仓库里没有什么

- 无 `private/` 凭据或 runtime catalog
- 无填好的 `references/local/` inventory
