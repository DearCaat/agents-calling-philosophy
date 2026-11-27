# agents-calling-philosophy

Portable **model calling** plugin with the `agents_ask` MCP tool and two JSONL clients for direct LLM APIs and gateway tools (jev, vela, mineru, oxr).

本机 inventory 与凭据**不进本仓库**。只装插件即可调用：路由表本机没有时从私有仓库 `agents-local` 现取并缓存（需该私库读取权限）；每台机器只需放好凭据（`~/.config/agents/credentials.env`，或数据根下 `private/credentials.env`）。要覆盖路由时再挂数据目录，Claude Code / Codex / Grok 共用。

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

## 查询调用入口

调用插件工具 `agents_ask`，传入 `{"name":"gemini-4-neon"}`。查询直接读取私有仓库 `DearCaat/agents-local` 的当前入口表。参见[用法与认证](skills/calling-models/references/agents-query.md)。

## 配置调用客户端的本机数据

只装插件时只需放凭据：`~/.config/agents/credentials.env`（`NAME=value`，权限 600）。旧版默认放在插件目录 `private/credentials.env` 的安装照旧可用，但插件目录随版本变，升级前把它移到 `~/.config/agents/`。

下面的数据目录是**可选的本机覆盖**：要固定本机自己的路由表时才挂。入口查询（`agents_ask`）总是读远端 `agents-local` 的入口表，数据目录只为它提供 Git 认证（`.git/config` 的 `core.sshCommand`），本地 `entries.md` 不覆盖它。三家 harness **解析同一目录**：

```bash
export AGENTS_DATA_ROOT=$HOME/.agents/calling-data
mkdir -p "$AGENTS_DATA_ROOT/local" "$AGENTS_DATA_ROOT/private"

# 可选：拷入模板，其中 api-routes.tsv 覆盖远端路由表。<插件目录> 换成本机已安装的 agents 插件根目录（其下应有 skills/calling-models/）
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

解析顺序（SKILL.md §3）：

1. `AGENTS_DATA_ROOT`
2. `CLAUDE_PLUGIN_DATA` / `PLUGIN_DATA` / `GROK_PLUGIN_DATA`
3. 插件树内 `references/local`（兼容目录版安装）
4. 路由表：以上都没有 `api-routes.tsv` 时取私库 `agents-local` 的表（先 SSH 后 https，缓存一小时，stderr 写明来源）；凭据：没有数据根时为 `~/.config/agents/credentials.env`

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

### 长会话中的冷热更新边界

| 组件 | 生效方式 | 说明与刷新动作 |
| --- | --- | --- |
| 入口查询数据（`agents_ask` / `agents-query.py`） | 热生效 | 每次调用实时拉取远端 `agents-local` `main` 分支，不缓存数据内容。 |
| 路由表本地覆盖（`local/api-routes.tsv`） | 热生效 | 设了本地覆盖表时，客户端每次执行直接读磁盘文件。 |
| 路由表远端缓存（`api-routes.json`） | TTL 内沿用缓存 | 未设本地覆盖表时默认缓存 1 小时（`CACHE_TTL=3600`）；要立即获取远端更新，删除 `$AGENTS_CACHE_DIR/api-routes.json`（默认 `~/.cache/agents/api-routes.json`）后重跑即可。 |
| 客户端脚本（`scripts/*.py`） | 依安装方式而定 | 同目录直接改写脚本时，下次 `python3` 调用即加载最新代码；但插件按版本更新到新缓存目录时，已启动的长会话仍指向旧目录，需重载或重开会话。 |
| MCP 进程与配置（`agents-mcp.py`、`.mcp.json` / `.codex-mcp.json` / `mcp_config.json`） | 冷生效 | MCP server 在会话初始化时常驻启动，长会话内不会重启；配置变更或脚本更新需重载插件或重开会话（其内部 `tools/call` 子进程仍每次读最新远端入口表）。 |
| 技能正文与触发描述（`SKILL.md`、`references/*.md`） | 冷生效（按安全口径） | `SKILL.md` 的 `name`/`description` 在会话启动时注入上下文，且多版本缓存下技能路径已绑定旧版；更新后需在 Claude Code 运行 `/reload-plugins` 或在各宿主重开会话。 |

### 如何核对实际加载版本与路由缓存提交号

- **核对插件版本**：查看当前上下文读取到的 `SKILL.md` 所在插件根目录下的 manifest（`.claude-plugin/plugin.json`、`.codex-plugin/plugin.json` 或 `.antigravity-plugin/plugin.json`）中的 `version` 字段及插件缓存路径版本/提交目录。更新后在 Claude Code 中运行 `/reload-plugins` 或开新会话，在 Codex / 其他宿主中开新会话并核对工具列表里出现 `agents_ask`。若长会话无法直接读回内存中已注入的旧版 `description` 或常驻 MCP 版本，不要凭安装目录推断已生效，应显式说明未核实内存快照并开新会话从已知版本重新加载。
- **核对路由表提交号与缓存**：运行 `llm-call.py` 或 `tool-call.py` 时，**stderr** 会写明本次实际使用的路由来源（例如 `agents-local@<7位sha>` 或 `route registry from cache ... (agents-local@<7位sha>, ...)`；若走本地覆盖表则不输出远端提交号）。也可直接查看 `~/.cache/agents/api-routes.json` 中的 `commit` 与 `fetched_at` 字段，或执行 `python3 <技能目录>/scripts/agents-query.py show routes` 从 stderr 核对远端最新提交。

## 仓库里有什么

- Skill: `skills/calling-models/`（`SKILL.md`，入口表的查法与两个客户端）
- Reference: `references/agents-query.md`、`references/llm-call.md`、`references/tools.md`
- Scripts: `scripts/`（`agents-query.py`、`llm-call.py`、`tool-call.py`、旧包装 `jev-call.py`、`tools/`）
- Overlay template: `references/local.example/`（`api-routes.tsv`、`harnesses.md`、`entries.md`）

## 仓库里没有什么

- 无 `private/` 凭据
- 无填好的 `references/local/` inventory
