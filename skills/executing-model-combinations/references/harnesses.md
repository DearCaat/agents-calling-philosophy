# Harness 事实与原生调用

核实日期：2026-09-03。原生命令是规范入口；wrapper 只覆盖其中一小部分。下面的 `PLUGIN_ROOT` 指源码根或宿主安装后的插件根。

```bash
PLUGIN_ROOT=/path/to/agents
set -a
source "$PLUGIN_ROOT/private/credentials.env"
set +a
```

静态 token 由这个文件注入；ChatGPT OAuth 与 Grok managed session 仍使用各自的最小原生状态文件。命令不得用 tracing 或回显凭据。

## 能力总览

| `harness_id` | 当前版本/入口 | headless | session | 压缩 | 权限与内部 fan-out |
|---|---|---|---|---|---|
| `codex-subagent` | 当前 Codex 宿主协作工具 | `spawn_agent` | `followup_task` 继续同一 child；无 shell resume | 宿主管理，参数不暴露 | 共享 filesystem/cwd；当前团队总槽位 11；child 可再派 child |
| `codex-cli` | PATH 上的 `codex`（当前 0.153.0） | `codex exec` | exec `resume` / `fork` | request compression 与 remote compaction feature 当前开启；无 `--autocompact` | sandbox 三档、approval；本机 multi-agent v2 最大 11 子线程 |
| `claude-old` | PATH 上的 `claude_old`（wrapper → `~/.local/share/claude_old/current`，当前 2.1.259；HOME=`~/.claude_old_env`） | `-p/--print` | continue/resume/fork | `--autocompact auto|100k–1M` | permission mode、allow/deny tools、agents；数值并发未核实 |
| `dsh` | 历史版本 `dsh 0.1.1-rc.2`；本机未安装（2026-09-03 探测：PATH / nvm login shell / `~/.dsh` 均无） | `--profile headless` | 每次新 session；headless 无 follow-up/resume | basic compaction：auto、threshold 0.8、retain 0.16、summary 8192 | 默认 workspace-write + ask；内建 retry；subagent maxDepth 3；parallel tool calls 默认 10 |
| `grok-build` | `grok 1.0.13` | prompt / prompt-file | continue/resume/fork | 精确上下文压缩行为未核实 | sandbox、permission mode、subagents；数值并发未核实 |

## 选择时可比较的 harness 特点

以下是 harness 的运行事实或文浩的本机经验，不是对任一模型的通用排名。

- `codex-subagent`：唯一可直接使用当前 Codex 宿主协作工具的入口；child 与 main 共享 filesystem/cwd，可用 `followup_task` 延续局部上下文。宿主内需要调用已登记 GPT 时优先它；其限制是不能作为独立 shell session resume，也不能承载其他厂商模型。
- `codex-cli`：可在新会话中显式选择已登记 profile / model / effort，支持 `exec resume` 与 `fork`、JSONL 和 output schema；适合需要独立 CLI session 或外部并行的已登记组合。它不会因为 catalog 有条目就让缺失 binding 的模型变得可执行。
- `claude-old`：支持 continue/resume/fork、`--autocompact` 与 permission/tool 参数。文浩的 `local-experience` 是其压缩较好，长程任务可优先考虑；这不证明当前本地代理的 alias 对应某个 Claude 上游模型，也不代表每类任务更强。
- `dsh`：有原生 retry 和内部 subagent/parallel tool-call 能力；但当前 headless profile 固定，不能逐次传 model、effort、cwd、JSON output 或 resume。因此它的灵活组合性低于可逐次 selector 的 Codex CLI，新增组合必须先有新的可核实 profile。
- `grok-build`：支持 session continue/resume/fork、sandbox 与 subagents；当前模型压缩行为和数值并发未核实，且已有请求 model 与 observed model 漂移的事实。需要精确复现时必须记录两者。

## Codex native sub-agent

这是 Codex 宿主当前会话中的工具，不是 shell 命令，Claude Code、dsh 和 Grok 不能直接调用它。显式覆盖 model/effort 时，`fork_turns` 使用 `none` 或正整数：

```text
spawn_agent(
  task_name="bounded_task",
  fork_turns="none",
  model="gpt-5.6-luna",
  reasoning_effort="max",
  message="完整、自足的任务说明"
)
```

Terra 默认 `reasoning_effort="high"`，Sol 默认 `"medium"`（特别重要或困难再提高）。口径见 [models.md](models.md)。`followup_task` 触发已有 child 的后续轮次；`send_message` 只传消息，不触发新轮次。工具面没有 stdout/JSONL、独立 cwd、per-child sandbox 或 CLI 版本参数。

## Codex CLI

入口就是 PATH 上的 `codex`，不要解析成绝对路径。若 login shell 提供 nvm 则仍可先加载，但 2026-09-03 探测本机没有 `~/.nvm/nvm.sh`。

```bash
codex --version

codex exec -p luna-max \
  -c model_catalog_json="$PLUGIN_ROOT/private/runtime/codex-home/model-catalogs/luna-v2.json" \
  -C "$DIR" --skip-git-repo-check \
  -s workspace-write -o "$LAST_MESSAGE" - <"$PROMPT_FILE" \
  >"$EVENTS" 2>"$ERR"
```

模型可用 `-m/--model`，effort 用 `-c model_reasoning_effort=...`，profile 用 `-p`。`--json` 输出 JSONL，`-o` 单独写最终消息，`--output-schema` 约束最终输出。sandbox 为 `read-only|workspace-write|danger-full-access`；`--approve-for-me` 与危险 bypass 参数只能按任务权限显式使用。

2026-09-03 实测（grok-4.5 via cc）：`codex exec -s workspace-write` **连不上** `127.0.0.1` 上的本机 listener（curl exit 7 / connection refused 外观）；同一探针 `-s danger-full-access` 立即 OPEN。cc 进程本身始终能通 loopback。因此 luna-max 在 workspace-write 下测本机网关会误报挂了——那是 Codex sandbox 隔了 loopback。探本机 listener 不要用 workspace-write。

Profiles 是 `~/.codex/<name>.config.toml`，以符号链接指向 bundle 内的 profile 文件。

```bash
cd "$DIR"
codex exec resume "$SESSION_ID" -o "$LAST_MESSAGE" - <"$FOLLOWUP_FILE"
codex exec fork "$SESSION_ID" -o "$LAST_MESSAGE" - <"$FORK_PROMPT_FILE"
```

resume/fork 从原 cwd 启动，不再传新会话的 profile 或 `-C`。外部并行且不需要内部 fan-out 时，可在新会话命令中加入：

```bash
--disable multi_agent -c 'features.multi_agent_v2.enabled=false'
```

## Claude Code：`claude_old`

入口是 PATH 上的 `claude_old`（不要解析成 `claude`）。说 Anthropic Messages。本机 BASE_URL、credential、HOME 见 [local/harnesses.md](local/harnesses.md) 与 `local/adapters.tsv`；`dispatch.sh` 按 adapter 组装，不在 portable 文档写死 URL。

通用行为：

- 用 `ANTHROPIC_DEFAULT_*` 把子 agent 的 sonnet/opus/haiku/fable alias 钉到请求模型族，避免落到目录里真实存在的 `claude-sonnet-4-6`。
- 内建 catalog 没有 grok/gemini 时会出现 `unrecognized_model` 警告；不等于调用失败。
- `--autocompact` 与 `CLAUDE_CODE_MAX_CONTEXT_TOKENS` 绑死；默认见 portable [runtime-defaults.tsv](runtime-defaults.tsv) / [models.md](models.md)（context 属调用哲学）。
- 支持 continue/resume/fork、`--effort`、permission/tool 参数。压缩较好适合长线程（harness 经验）。

```bash
cd "$DIR"
# BASE_URL / AUTH_TOKEN / DEFAULT_* / MAX_CONTEXT 由本机 inventory + dispatch 注入
claude_old --model grok-4.5 --effort high --autocompact 200k \
  --output-format text -p < "$PROMPT_FILE"
claude_old --continue -p < "$FOLLOWUP_FILE"
claude_old --resume "$SESSION_ID" -p < "$FOLLOWUP_FILE"
```

Token 不得出现在 argv；prompt 用 `-p < file`。

## dsh

本机未安装（2026-09-03 探测：PATH / nvm login shell / `~/.dsh` 均无）；以下保留历史能力事实。`dispatch.sh` 的 dsh 分支为未来安装保留。

```bash
source ~/.nvm/nvm.sh
cd "$DIR"
DSH_HOME="$PLUGIN_ROOT/private/runtime/dsh-home" \
DSH_AGENTS_HOME="$PLUGIN_ROOT" \
  dsh --profile headless "$(<"$PROMPT_FILE")" >"$OUT" 2>"$ERR"
```

headless 任务文本作为 argv 传入，可能短暂出现在本机进程列表。该入口没有逐次 `--model`、`--effort`、`--cwd`、JSON output 或 resume；当前 API/model/effort 从打包的 `private/runtime/dsh-home/settings.yaml` 读取。需要切 API/model 时先形成新的可核实 profile 和 binding，不能把 Codex profile 参数套给 dsh。`DSH_AGENTS_HOME="$PLUGIN_ROOT"` 让 dsh 加载同一 `skills/`，不是安装 Cordis/npm plugin。

dsh 当前加载 `dsh-llm-retry`。provider 未覆盖 `retryPolicy` 时，normal mode 默认对 rate limit、server、timeout、transport 和 empty-response 错误最多重试 5 次，backoff 约 500 ms–10 s。这是原生 harness 行为；`dispatch.sh` 不在其外层再重试。

## Grok Build

当前 CLI 为 `grok 1.0.13`。`grok-build` harness 由 `dispatch.sh` 按 `api_id` 选择两条 API 路径：

- `grok-direct-api`（默认）：不设置 `GROK_HOME`，使用用户真实 `~/.grok`。
- `grok-build-managed`：仍设置 `GROK_HOME="$PLUGIN_ROOT/private/runtime/grok-home/.grok"`；这是保留的非默认路径。

```bash
# grok-direct-api（默认）
grok --cwd "$DIR" --model grok-4.5 --reasoning-effort high \
  --output-format json --prompt-file "$PROMPT_FILE" \
  >"$OUT" 2>"$ERR"
```

```bash
# grok-build-managed（保留路径）
GROK_HOME="$PLUGIN_ROOT/private/runtime/grok-home/.grok" \
  grok --cwd "$DIR" --model grok-4.5 --reasoning-effort high \
  --output-format json --prompt-file "$PROMPT_FILE" \
  >"$OUT" 2>"$ERR"
```

输出格式为 `plain|json|streaming-json|streaming-messages-json`。两条路径均支持 continue/resume/fork；默认 direct 路径不设置 `GROK_HOME`：

```bash
grok --cwd "$DIR" --continue -p "继续同一目标"
grok --cwd "$DIR" --resume "$SESSION_ID_OR_TITLE" -p "继续同一目标"
grok --cwd "$DIR" --resume "$SESSION_ID" --fork-session -p "创建分支"
```

managed 路径的会话命令在上述命令前保留 `GROK_HOME="$PLUGIN_ROOT/private/runtime/grok-home/.grok"`。

支持 `--sandbox`、`--permission-mode`、allow/deny、`--agent/--agents` 与 `--no-subagents`。当前 config 的 `compact_mode=false` 是 UI 配置，不能据此推断上下文压缩策略。

## 凭据与协议边界

- Codex providers 从 profile 的 `env_key` 读取 bearer（`luna-max` / `luna-high` 为 `openai-local-8317`）；Claude Code 从 `ANTHROPIC_AUTH_TOKEN` 或 `ANTHROPIC_API_KEY` 读取，但请求格式仍是 Anthropic Messages。
- dsh provider 通过 `apiKeyEnv` 读取继承环境；Grok managed auth 使用自身结构化 session 状态。
- 把同一 token 注入多个 harness 只在 API 同时接受相应 header 与 wire protocol 时成立。没有这项兼容证据就不增加 binding。
