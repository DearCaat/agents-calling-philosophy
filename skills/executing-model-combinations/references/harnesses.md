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

| `harness_id` | 入口 | headless | session | 压缩 | 权限与内部 fan-out |
|---|---|---|---|---|---|
| `codex-subagent` | 当前 Codex 宿主协作工具 | `spawn_agent` | `followup_task` 继续同一 child；无 shell resume | 宿主管理，参数不暴露 | 共享 filesystem/cwd；当前团队总槽位 11；child 可再派 child |
| `codex-cli` | PATH 上的 `codex` | `codex exec` | exec `resume` / `fork` | request compression 与 remote compaction feature 当前开启；无 `--autocompact` | sandbox 三档、approval；本机 multi-agent v2 最大 11 子线程 |
| `claude-old` | PATH 上的 `claude_old`（本机 wrapper 与 HOME 见 `local/harnesses.md`） | `-p/--print` | continue/resume/fork | `--autocompact auto|100k–1M` | permission mode、allow/deny tools、agents；数值并发未核实 |
| `dsh` | `dsh`（DeepSeek Harness；交互版 `dst` = dsh-tui，不能脚本化） | `--profile headless`，`-` 读 stdin | 每次新 session；`--session-id` 可接续 | basic compaction：auto、threshold 0.8、retain 0.16、summary 8192 | 默认 workspace-write + ask；内建 retry；subagent maxDepth 3；parallel tool calls 默认 10 |
| `grok-build` | PATH 上的 `grok` | prompt / prompt-file | continue/resume/fork | 精确上下文压缩行为未核实 | sandbox、permission mode、subagents；数值并发未核实 |
| `antigravity-cli` | PATH 上的 `agy` | `-p/--print` | continue/conversation | 上下文超限由 CLI 内部自动处理 | sandbox/dangerously-skip-permissions、MCP、subagent |
| `agy-min` | PATH 上的 `agy-min`（本机启动器，见 `local/harnesses.md`） | `-p/--print` | 同 `antigravity-cli`，会话在隔离 HOME 里 | 同 `antigravity-cli` | 同 `antigravity-cli`，agent 固定为 `mini` |

## 选择时可比较的 harness 特点

以下是 harness 的运行事实或文浩的本机经验，不是对任一模型的通用排名。

- `codex-subagent`：唯一可直接使用当前 Codex 宿主协作工具的入口；child 与 main 共享 filesystem/cwd，可用 `followup_task` 延续局部上下文。不经 wrapper，effort 与 context 由宿主带上；其限制是不能作为独立 shell session resume，也不能承载其他厂商模型。
- `codex-cli`：可在新会话中显式选择已登记 profile / model / effort，支持 `exec resume` 与 `fork`、JSONL 和 output schema；适合需要独立 CLI session 或外部并行的已登记组合。它不会因为 catalog 有条目就让缺失 binding 的模型变得可执行。
- `claude-old`：支持 continue/resume/fork、`--autocompact` 与 permission/tool 参数。压缩较好，长程任务较稳（`local-experience`）；这不证明当前本地代理的 alias 对应某个 Claude 上游模型，也不代表每类任务更强。
- `dsh`：有原生 retry 和内部 subagent/parallel tool-call 能力。headless 没有逐次 `--model`/`--effort`；`dispatch.sh` 每次写一个一次性 `--patch` 覆盖 `agent-default-model` 来选模型，effort 不逐次传。cwd 取进程当前目录。preset（如 minimal）与 profile 是两层：preset 由 api-session-controller 在创建会话时挂载（web profile 加载它），headless runner 不经过它，所以 headless 下设默认 preset 不生效。要在 headless 用 minimal 的工具面，把 minimal 的插件表平铺成 `--patch`（覆盖已有行用 `- id:`，新增行如 MCP 工具用 `- insert:`）。
- `grok-build`：支持 session continue/resume/fork、sandbox 与 subagents；当前模型压缩行为和数值并发未核实，且已有请求 model 与 observed model 漂移的事实。需要精确复现时必须记录两者。
- `antigravity-cli`：入口为 PATH 上的 `agy`，Google Antigravity 原生 CLI。支持 `-p/--print`、`--model`、`--effort`、`--continue`、`--dangerously-skip-permissions`。可直连网关或 Gemini API，能联网，原生大上下文吞吐与多 Agent 编排速度快。`-p` 原生默认 5 分钟超时，到点可能带着半截输出 exit 0（`dispatch.sh` 默认传 12h）；对部分模型不接受 `--effort`。经网关时，上下文压缩靠网关模型列表里的 `inputTokenLimit`：用 `AGY_LLM_GATEWAY_MODELS` 静态列表时上限是 0，`/compact` 和自动压缩报 `summaryRequestMaxTokens ... is non-positive (modelMaxInputTokens=0, ...)`，长会话会停在这里；不设该变量、让 agy 从网关发现模型即可（agy 1.4.7，2026-10-26 local-test）。
- `agy-min`：本机给 agy 写的最小启动器，不改 agy 二进制。它隔离 HOME（不带对话历史、brain、技能目录），用一个只有一句 system prompt 的自定义 agent，经网关调用；参数与 `antigravity-cli` 相同。请求仍由 agy 生成，system 约为原版 agy 的一半（10-25 实测 6479 对 14572 字节，`local-test`）。要像调 API 一样、尽量少受 harness 影响地用 Gemini 时用它。

## 厚薄

最薄的是 dsh 套 minimal 工具面，基本只有模型加少量工具（headless 下要平铺成 `--patch`，见上文 dsh 条）；dsh 默认 profile 多了内建 retry、subagent 和并行工具调用；`codex-cli`、`claude-old`、`grok-build`、`antigravity-cli` 是成熟的厚 harness，有压缩、子代理、沙箱和会话续用；`agy-min` 是去掉历史与多余 system 段的 agy，工具与请求格式仍由 agy 决定。厚薄的取舍见 [executors.md](executors.md)。

## Codex native sub-agent

这是 Codex 宿主当前会话中的工具，不是 shell 命令，Claude Code、dsh 和 Grok 不能直接调用它。显式覆盖 model/effort 时，`fork_turns` 使用 `none` 或正整数：

```text
spawn_agent(
  task_name="bounded_task",
  fork_turns="none",
  model="<model>",
  reasoning_effort="<effort>",
  message="完整、自足的任务说明"
)
```

本机各模型的默认 effort 见 `local/runtime-defaults.tsv`；native sub-agent 不经 dispatch，要自己带。`followup_task` 触发已有 child 的后续轮次；`send_message` 只传消息，不触发新轮次。工具面没有 stdout/JSONL、独立 cwd、per-child sandbox 或 CLI 版本参数。

## Codex CLI

入口就是 PATH 上的 `codex`，不要解析成绝对路径。若 login shell 提供 nvm 则先加载。

```bash
codex --version

codex exec -p "$PROFILE" \
  -c model_catalog_json="$CATALOG_JSON" \
  -C "$DIR" --skip-git-repo-check \
  -s workspace-write -o "$LAST_MESSAGE" - <"$PROMPT_FILE" \
  >"$EVENTS" 2>"$ERR"
```

模型可用 `-m/--model`，effort 用 `-c model_reasoning_effort=...`，profile 用 `-p`。`--json` 输出 JSONL，`-o` 单独写最终消息，`--output-schema` 约束最终输出。sandbox 为 `read-only|workspace-write|danger-full-access`；`--approve-for-me` 与危险 bypass 参数只能按任务权限显式使用。

2026-09-03 实测：`codex exec -s workspace-write` **连不上** `127.0.0.1` 上的本机 listener（curl exit 7 / connection refused 外观）；同一探针 `-s danger-full-access` 立即 OPEN。非 Codex-sandbox 的本机进程能通 loopback。因此在 workspace-write 下测本机网关会误报网关挂了——那是 Codex sandbox 隔了 loopback。探本机 listener 不要用 workspace-write。

Profiles 是 `~/.codex/<name>.config.toml`，以符号链接指向 bundle 内的 profile 文件。

```bash
cd "$DIR"
codex exec resume "$SESSION_ID" -o "$LAST_MESSAGE" - <"$FOLLOWUP_FILE"
codex exec fork "$SESSION_ID" -o "$LAST_MESSAGE" - <"$FORK_PROMPT_FILE"
```

无头 `codex exec` 挂 MCP 时加 `-c 'mcp_servers.<name>.default_tools_approval_mode="approve"'`，否则工具调用卡在审批；不用 stdin 时 `</dev/null`。长文本产物让它作最终消息，由 `-o` 接住。

resume/fork 从原 cwd 启动，不再传新会话的 profile 或 `-C`。外部并行且不需要内部 fan-out 时，可在新会话命令中加入：

```bash
--disable multi_agent -c 'features.multi_agent_v2.enabled=false'
```

## Claude Code：`claude_old`

入口是 PATH 上的 `claude_old`（不要解析成 `claude`）。说 Anthropic Messages。本机 BASE_URL、credential、HOME 见 [local/harnesses.md](local/harnesses.md) 与 `local/adapters.tsv`；`dispatch.sh` 按 adapter 组装，不在 portable 文档写死 URL。

通用行为：

- 用 `ANTHROPIC_DEFAULT_*` 把子 agent 的 sonnet/opus/haiku/fable alias 钉到请求模型族，避免落到目录里真实存在的 `claude-sonnet-4-6`。
- 内建 catalog 没有 grok/gemini 时会出现 `unrecognized_model` 警告；不等于调用失败。
- 哪些模型适合放在 `claude-old` 里、哪些在本机试过不通，见 [models.md](models.md) 的搭配一节与 `local/binding-failures.tsv`。
- `--autocompact` 与 `CLAUDE_CODE_MAX_CONTEXT_TOKENS` 绑死；本机默认见 `local/runtime-defaults.tsv`。
- 支持 continue/resume/fork、`--effort`、permission/tool 参数。压缩较好，长线程较稳（`local-experience`）。

```bash
cd "$DIR"
# BASE_URL / AUTH_TOKEN / DEFAULT_* / MAX_CONTEXT 由本机 inventory + dispatch 注入
claude_old --model "$MODEL" --effort "$EFFORT" --autocompact "$AUTOCOMPACT" \
  --output-format text -p < "$PROMPT_FILE"
claude_old --continue -p < "$FOLLOWUP_FILE"
claude_old --resume "$SESSION_ID" -p < "$FOLLOWUP_FILE"
```

Token 不得出现在 argv；prompt 用 `-p < file`。

## dsh

```bash
cd "$DIR"
printf -- "- id: agent-default-model\n  config:\n    provider: %s\n    model: %s\n" "$PROVIDER" "$MODEL" >"$PATCH"
DEEPSEEK_API_KEY=... dsh --profile headless --patch "$PATCH" - <"$PROMPT_FILE" >"$OUT" 2>"$ERR"
```

- `--patch` 要放在 app 参数（`--json`、任务文本）之前，否则报 unknown option。
- provider 的 endpoint 与模型列表在用户自己的 dsh 配置里（本机见 `local/harnesses.md`）；`dispatch.sh` 只覆盖所选模型。
- provider 的 key 环境变量必须非空：为空时 dsh 会回退读工作目录的 `.env`。
- stdout 只有最终文本，reasoning 与诊断进 stderr；`--json` 输出 JSONL 事件（不含模型名）。退出码 0 表示最后一轮完成。
- dsh 自带 `dsh-llm-retry`；`dispatch.sh` 不在其外层再重试。
- 长会话会丢任务说明：basic compaction 按估计 token 触发，阈值 = min(0.8×窗口, 窗口−maxTokens−65536)，300K 窗口、128K maxTokens 时约 106K；压缩连首条消息一起压掉，没有置顶保护（`local-test`）。任务说明在首条消息里的长任务，把 maxTokens 降到 64K 并禁用 compaction-basic，或拆成短会话。

## Grok Build

`grok-build` harness 由 `dispatch.sh` 按 `api_id` 选择两条 API 路径：

- `grok-direct-api`：不设置 `GROK_HOME`，使用用户真实 `~/.grok`。
- `grok-build-managed`：设置 `GROK_HOME="$PLUGIN_ROOT/private/runtime/grok-home/.grok"`。

```bash
# grok-direct-api
grok --cwd "$DIR" --model "$MODEL" --reasoning-effort "$EFFORT" \
  --output-format json --prompt-file "$PROMPT_FILE" \
  >"$OUT" 2>"$ERR"
```

```bash
# grok-build-managed
GROK_HOME="$PLUGIN_ROOT/private/runtime/grok-home/.grok" \
  grok --cwd "$DIR" --model "$MODEL" --reasoning-effort "$EFFORT" \
  --output-format json --prompt-file "$PROMPT_FILE" \
  >"$OUT" 2>"$ERR"
```

输出格式为 `plain|json|streaming-json|streaming-messages-json`。两条路径均支持 continue/resume/fork；direct 路径不设置 `GROK_HOME`：

```bash
grok --cwd "$DIR" --continue -p "继续同一目标"
grok --cwd "$DIR" --resume "$SESSION_ID_OR_TITLE" -p "继续同一目标"
grok --cwd "$DIR" --resume "$SESSION_ID" --fork-session -p "创建分支"
```

managed 路径的会话命令在上述命令前保留 `GROK_HOME="$PLUGIN_ROOT/private/runtime/grok-home/.grok"`。

支持 `--sandbox`、`--permission-mode`、allow/deny、`--agent/--agents` 与 `--no-subagents`。当前 config 的 `compact_mode=false` 是 UI 配置，不能据此推断上下文压缩策略。

## 各 harness 共有的坑

- 工具输出会被截断，worker 不察觉：codex 截中段（`…N tokens truncated…`），agy 截头留尾（`<truncated N lines>`）；它会对没看到的内容照判、编值。输入按小块给；归因前先查会话记录里它实际看到了什么（codex rollout 在 `~/.codex/sessions/YYYY/MM/DD/rollout-*<id>.jsonl`，exec 是 `custom_tool_call`、命令在 `input`）。
- 判定、核查类 worker 各自在独立空目录跑：共享目录里它们会翻别的 run 的输出照抄（本机 221/438 个 judge 读了别人的结果，`local-test`）。材料内联进 prompt，输出写绝对路径，同轮各臂、各数据集互不可见；事后扫会话记录查越界。agy 的相对路径落在 `~/.gemini/antigravity-cli/scratch/`。
- 杀进程按 pid 或进程组；`pkill -f <prompt 文件名>` 会连调用方自己的 shell wrapper 一起匹配上。
- 产物在进程退出后还可能被改写一次，采用前重新比对 sha256。

## 凭据与协议边界

- Codex providers 从 profile 的 `env_key` 读取 bearer；Claude Code 从 `ANTHROPIC_AUTH_TOKEN` 或 `ANTHROPIC_API_KEY` 读取，但请求格式仍是 Anthropic Messages。
- dsh provider 通过 `apiKeyEnv` 读取继承环境；Grok managed auth 使用自身结构化 session 状态。
- 把同一 token 注入多个 harness 只在 API 同时接受相应 header 与 wire protocol 时成立。没有这项兼容证据就不增加 binding。
