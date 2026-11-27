---
name: calling-models
description: 需要用模型、用户点名某个执行者（如「gemini」「luna」「astra」「cc」），或要批量或直接调 LLM API、调判定模型 jev 或其他网关工具（vela、mineru、oxr）时使用；先查入口表定走哪条入口。选哪类执行者（脚本、判定模型、API、harness）不在这里。
---

# 调用模型

`$SKILL` 为本 skill 目录。

## 1. 先查入口表

- 用插件工具 `agents_ask` 查询 `DearCaat/agents-local` 的最新入口。宿主未加载 MCP 时，用 `python3 "$SKILL/scripts/agents-query.py" ask "说法"`。用法和认证见 [查询入口](references/agents-query.md)。
- 委派给 harness 时，优先使用当前宿主的原生 sub-agent（前提是满足指定模型、effort 与环境）；仅在用户指定其他 harness 或原生派发无法满足时再查外部入口。
- **显式指定模型并核对**：派发 sub-agent 或外部 CLI 时务必显式传入目标模型（及所需 effort）。不少宿主在省略模型参数时会默认继承主会话的高成本旗舰模型，不会自动切到轻量模型。派发后通过派发回执/工具返回元数据、任务运行日志或宿主界面状态核对实际生效的模型与 effort，不符预期时及时调整或重派。
- **阻塞及时切换**：单条通道或单个执行者长时间无响应、反复超时或限流（429），或明显拖慢整体进度时，及时改派其他执行者或切换备用通道，不要在同一阻塞点空等；切往付费通道前，先核对已授权通道清单（入口表 / `api-routes.tsv` 及备注）与并发上限。
- 表里没有这个说法：报告缺口，不拿名字相近的模型顶替。

## 2. 两个客户端

- **判定器优先**：分类、筛选、打分等成批判定优先用 `scripts/tool-call.py jev`（成本低、可出概率、可复现）。初版效果不够时不要直接改调大模型，先改问法、收紧选项边界或补齐必要输入（如切图、局部上下文）；实测调优后精召仍不达标时再升级直接调 LLM API。
- **统一 API 入口**：直接调 LLM API 的批量任务统一走 `scripts/llm-call.py`（`references/llm-call.md` 明示例外除外），不另写平行封装或临时请求循环。
- `scripts/llm-call.py`：直接调 LLM API 的 JSONL 批量客户端，协议为 Chat 或 Messages，并发、重试、续跑。用法见 `references/llm-call.md` 与 `--help`。
- `scripts/tool-call.py`：调 CPA 工具网关 `/tool/<name>` 的 JSONL 批量客户端，工具为 jev、vela、mineru、oxr，另可只读查询。用法见 `references/tools.md` 与 `--help`。

```
python3 "$SKILL/scripts/llm-call.py" --in items.jsonl --out results.jsonl --api <api_id> --model <前缀/模型名>
python3 "$SKILL/scripts/tool-call.py" jev --in items.jsonl --out results.jsonl --model <模型 id>
```

`<api_id>` 从路由表取（见第 3 节）。模型名以网关文档与实时接口为准。

## 3. 路由与凭据

- 两个调用客户端的本地清单目录取 `AGENTS_LOCAL_ROOT`。未设时为数据根下的 `local/`。数据根依次取 `AGENTS_DATA_ROOT`、`CLAUDE_PLUGIN_DATA`、`PLUGIN_DATA`、`GROK_PLUGIN_DATA` 中第一个非空值。都未设时，清单目录为插件内 `references/local/`。
- 路由表：清单目录下的 `api-routes.tsv`；本机没有这份时，客户端从私有仓库 `agents-local` 现取（同 `agents_ask` 的认证，先 SSH 后 https），缓存一小时，在 stderr 写明取自哪个提交或缓存；远端取不到，或取回的表没有数据行、有缺列的行时，用上一份可用缓存并写明。本地表存在（含断开的软链接）但读不了、没有数据行或有缺列的行时直接报错，不换用远端。只装插件的机器不用另放清单。`llm-call.py --api` 按 api_id 查，必须恰好一行；base_url 为 `-`、认证不是 bearer、或状态为 unavailable 的行直接拒绝。
- 凭据变量名写在 `api-routes.tsv` 的 `credential_ref` 列。`llm-call.py` 先取同名环境变量，再从凭据文件按 `NAME=value` 读；`tool-call.py` 另认 `env:`、`file:` 前缀。
- 凭据文件：`AGENTS_CREDENTIALS_FILE`；未设时为数据根下的 `private/credentials.env`；没有数据根时为 `~/.config/agents/credentials.env`（只有旧默认位置插件目录下 `private/credentials.env` 的安装照旧用它，升级前把它移到新位置）。凭据是各机器唯一必须本地放的东西。
- 凭据值不进 prompt、文档、回显或仓库。
- **冷热更新与版本核对**：插件更新后，长会话中 MCP 进程、`SKILL.md` front matter 描述与按版本目录固定的技能快照均属冷生效，需重载插件或重开会话；客户端脚本在同一目录下修改时下次子进程即生效，未设本地覆盖表时远端路由缓存（`~/.cache/agents/api-routes.json`）1 小时内沿用旧值（删除可立即刷新）。核对实际版本见 `README.md`「改动何时生效」，路由表提交号看客户端 stderr 输出。

## 4. 指针

- 选哪类执行者（脚本、判定模型、API、harness）：research-foundation 插件的 `delegating` 技能。
- 在线会话之间的通信：agentbus。
- harness 启动器的起法：见入口表指向的 `harnesses.md`。
- 通道、价格、授权、限流：网关文档「模型通道」与「自动池」，位置见 agents-local 的 `README.md`。
