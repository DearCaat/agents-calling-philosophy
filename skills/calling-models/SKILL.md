---
name: calling-models
description: 需要用模型、用户点名某个执行者（如「gemini」「luna」「astra」「cc」），或要批量或直接调 LLM API、调判定模型 jev 或其他网关工具（vela、mineru、oxr）时使用；先查入口表定走哪条入口。选哪类执行者（脚本、判定模型、API、harness）不在这里。
---

# 调用模型

`$SKILL` 为本 skill 目录。

## 1. 先查入口表

- 用插件工具 `agents_ask` 查询 `DearCaat/agents-local` 的最新入口。宿主未加载 MCP 时，用 `python3 "$SKILL/scripts/agents-query.py" ask "说法"`。用法和认证见 [查询入口](references/agents-query.md)。
- 委派给 harness 时，所有 harness 和模型都默认优先用当前 harness 的原生 sub-agent。先检查它能否满足指定的模型、effort 和任务环境。用户指定其他 harness，或当前 harness 的原生派发无法满足要求时，再查外部入口。派发后核对实际模型和 effort。
- 表里没有这个说法：报告缺口，不拿名字相近的模型顶替。

## 2. 两个客户端

- `scripts/llm-call.py`：直接调 LLM API 的 JSONL 批量客户端，协议为 Chat 或 Messages，并发、重试、续跑。用法见 `references/llm-call.md` 与 `--help`。
- `scripts/tool-call.py`：调 CPA 工具网关 `/tool/<name>` 的 JSONL 批量客户端，工具为 jev、vela、mineru、oxr，另可只读查询。用法见 `references/tools.md` 与 `--help`。

```
python3 "$SKILL/scripts/llm-call.py" --in items.jsonl --out results.jsonl --api <api_id> --model <前缀/模型名>
python3 "$SKILL/scripts/tool-call.py" jev --in items.jsonl --out results.jsonl --model <模型 id>
```

`<api_id>` 从路由表取（见第 3 节）。模型名以网关文档与实时接口为准。

## 3. 路由与凭据

- 两个调用客户端的本地清单目录取 `AGENTS_LOCAL_ROOT`。未设时为数据根下的 `local/`。数据根依次取 `AGENTS_DATA_ROOT`、`CLAUDE_PLUGIN_DATA`、`PLUGIN_DATA`、`GROK_PLUGIN_DATA` 中第一个非空值。都未设时，清单目录为插件内 `references/local/`。
- 路由表：清单目录下的 `api-routes.tsv`；本机没有这份时，客户端从私有仓库 `agents-local` 现取（同 `agents_ask` 的认证，先 SSH 后 https），缓存一小时，在 stderr 写明取自哪个提交或缓存；远端取不到时用旧缓存并写明。只装插件的机器不用另放清单。`llm-call.py --api` 按 api_id 查，必须恰好一行；base_url 为 `-`、认证不是 bearer、或状态为 unavailable 的行直接拒绝。
- 凭据变量名写在 `api-routes.tsv` 的 `credential_ref` 列。`llm-call.py` 先取同名环境变量，再从凭据文件按 `NAME=value` 读；`tool-call.py` 另认 `env:`、`file:` 前缀。
- 凭据文件：`AGENTS_CREDENTIALS_FILE`；未设时为数据根下的 `private/credentials.env`；没有数据根时为 `~/.config/agents/credentials.env`。凭据是各机器唯一必须本地放的东西。
- 凭据值不进 prompt、文档、回显或仓库。

## 4. 指针

- 选哪类执行者（脚本、判定模型、API、harness）：research-foundation 插件的 `delegating` 技能。
- 在线会话之间的通信：agentbus。
- harness 启动器的起法：见入口表指向的 `harnesses.md`。
- 通道、价格、授权、限流：网关文档「模型通道」与「自动池」，位置见 agents-local 的 `README.md`。
