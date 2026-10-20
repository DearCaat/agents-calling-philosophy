---
name: executing-model-combinations
description: 给派工 agent 用：一件活交给谁——脚本、判定模型（jev）、直接调用的 LLM API、harness+model——各自的特点与本机入口；选了 harness 时，当前 harness 默认派同 harness 的 native sub-agent；Grok 一律走 grok-build，禁止 grok+cc；Gemini 一律走 antigravity-cli，禁止 gemini+cc；DeepSeek 优先 dsh，其次 codex-cli；同一 harness+model 有多条通道时按本机 registry 的行序取第一条 verified，只在出错或额度用完时降级，或用户点名；只给任务则按选择程序自主执行；本机入口以 $AGENTS_DATA_ROOT/local/bindings.tsv 为准。只讨论厂商通用知识且不涉及本机资源时不使用。
---

# 执行模型组合

执行者有四类：脚本、判定模型、直接调用的 LLM API、harness 里的 agent，特点与取舍见 [executors.md](references/executors.md)；判定模型与 API 的本机入口在 `$AGENTS_DATA_ROOT/local/apis.md`，直接调用，不经 `dispatch.sh`。

选了 harness 时，本 skill 让当前调用方按**调用哲学**选出 `harness + model`，再在**本机 inventory**（`$AGENTS_DATA_ROOT/local/`）解析 `api` 并执行。profile、effort、搜索开关、权限、context 是运行参数，不属于 binding 身份。

- 哲学与默认 effort/context：见 [models.md](references/models.md) 与 [runtime-defaults.tsv](references/runtime-defaults.tsv)（`dispatch.sh` 读同一张表）。
- 本机通道与 registry：`$AGENTS_DATA_ROOT/local/`（模板 [local.example/](references/local.example/)）。它描述**跑 harness 的那台机器**；当前机器不是那台时（harness 不在 PATH），经该机器的 shell 桥执行 `dispatch.sh`，不要在当前机器照抄命令。
- harness 通用能力：[harnesses.md](references/harnesses.md)；本机 binary/URL 食谱：`local/harnesses.md`。

## 读取顺序

1. 认当前 harness；用下面口语表 / 选择程序得到 `(harness, model)` 与 effort/context 默认。
2. 读 `local/bindings.tsv`（或 `scripts/dispatch.sh --list-bindings`）。无命中 → **报告缺口，不改口**（不换成邻近 harness）。
3. 对入围 1–3 条，读 `local/apis.md` / `local/harnesses.md` 对应段；需要厂商边界再读 portable `models.md`。
4. 同 harness → native sub-agent（spawn 自带 effort/context）。跨 harness 或点名外部 CLI → `scripts/dispatch.sh`。

## 消费 agent 两大目标

1. **用户点名组合**：按「口语 → harness+model」还原后，用本机 registry 解析 api 并执行。不要问是不是另一个 harness。
2. **用户只给任务**：按选择程序选出，报出依据后执行。筛完为零才交回选择权。

`cc` = harness `claude-old`，不是模型名。三轴分开；api 由本机 inventory 决定。

### 当前 harness 用当前 harness

| 当前 main | 默认 worker | 入口 |
|---|---|---|
| cc + 已验证的 `claude-old` binding | 仍是同一 `claude-old` binding | Claude Code 子 agent |
| Codex + GPT | 仍是 Codex + GPT | native `spawn_agent`（`codex-subagent`，`wrapper=no`） |
| Grok CLI | 仍是 grok-build | grok 自己的 subagent |

Grok 是当前 harness 原则的例外：无论主 harness，Grok 都走 grok-build。其它跨 harness 情形是用户点名另一套、当前 harness 放不下该模型、角色要求换组合（杂事→luna-max，审查→terra/sol）或 `parallel.sh` fan-out。

### 口语 → harness + model

| 用户说 | harness | model | 入口哲学 |
|---|---|---|---|
| grok / grok-4.5 | `grok-build` | `grok-4.5` | 从本机 registry 选已验证 binding |
| grok-4.6 | `grok-build` | `grok-4.6` | 仅在本机 registry 为 verified 时执行 |
| grok + cc | — | — | 不可用；改用 grok-build，不得派 `claude-old` |
| gemini + cc | — | — | 不可用；改用 antigravity-cli，不得派 `claude-old` |
| gemini / gemini-3.8-flash | `antigravity-cli` | `gemini-3.8-flash` | 知识性 flash；禁止走 `claude-old` |
| ds-flash / deepseek-flash | `dsh` | `deepseek-flash` | 首选 dsh；dsh 无 verified binding 时用 `codex-cli` |
| ds-pro / deepseek-pro | `dsh` | `deepseek-pro` | 同上 |
| glm / glm-5.3-flash | `dsh` | `glm-5.3-flash` | 本机 registry 有 verified `claude-old` binding 时也可用 cc |
| luna-max / 杂事 | `codex-cli` | `gpt-6-luna` | 已在 Codex：优先 `spawn_agent`；否则 `codex exec` |
| GPT terra / sol | `codex-cli` | `gpt-5.6-terra` / `gpt-6-sol` | Codex |
| grok CLI / grok-build | `grok-build` | `grok-4.5` | 与默认 Grok 路径相同 |

官方 Anthropic Claude 默认不派。具体 BASE_URL / binary / 通道名见本机 `local/`。

## 选择程序

用户指定了 harness+model（或口语可还原）：对本机 bindings 按原值核对。`verified` 则执行。`configured` 且是上表推荐入口时，`verify.sh` 晋级后再派。其它 `configured` / `degraded` / 未登记：**报告缺口，不改口**。

用户只给任务时：

1. **划可派面。** 已在目标 harness 内则含 native。跨 harness 才要 `wrapper=yes`。官方 Claude、默认不派的 `openai-direct` 不进默认候选。
2. **硬约束筛。** resume/fork → 复用 worker。Grok → `grok-build`（`grok+cc` 不可用）；Gemini → `antigravity-cli`（`gemini+cc` 不可用）；DeepSeek → `dsh`，其次 `codex-cli`；GPT → `codex-cli`。
3. **排序**（见 models.md）：杂事与日常执行 → 优先 dsh 上的 deepseek-flash / glm-5.3-flash，或 luna-max；执行器 → `grok-build` + 已验证 Grok；知识性任务 → `antigravity-cli` + `gemini-3.8-flash`；审查 → terra/sol。Grok/gemini 不当杂事默认。
4. **选通道。** 同一 harness+model 有多条 api 时，取 `bindings.tsv` 中**最靠前的 verified 行**（行序即本机通道优先级）。**只在出错或额度用完时降到下一条**，或用户点名某条；不因慢、不因想对比而换。降级要在交还里写明原因。
5. **报出所选** 后再调用，并带 effort/context（未指定则用 `runtime-defaults.tsv`）。

## 派多个组合

批量或高 fan-out 先取得**用户**确认。JSONL → `parallel.sh`；每行唯一 `job_id` / `out` / `stderr`。示例路径用 `$SKILL`，三元组从本机 `--list-bindings` 抄，不要抄死另一台机器的 api_id。

## 约束

- 缺口交还是报告，不是补「接近的」组合。晋级只用 `scripts/verify.sh`（探针规范化后须为 `PING`）。
- OpenRouter / `/models` 只发现候选，不产生 binding。
- 本机数据根：优先 `$AGENTS_DATA_ROOT`（`local/` + `private/`，三家 harness 共用）；否则 `CLAUDE_PLUGIN_DATA` / `PLUGIN_DATA` / `GROK_PLUGIN_DATA`；再否则插件树内 `references/local` + `private/`。细覆盖见 `AGENTS_LOCAL_ROOT` / `AGENTS_CREDENTIALS_FILE`。凭据值不进 prompt/文档/回显/仓库。

## 交还格式

至少报告：实际 API/harness/model、profile 或 alias、effort/context、原生或 wrapper、退出状态、session（若有）、是否降级及原因。请求模型与观察到的模型分列。

完成判据：每次调用能由上述字段还原 binding 与运行参数；失败与漂移作偏差列出（未发现则带查证链的 null 报告）。推荐语不能充当完成。
