---
name: executing-model-combinations
description: 给要把活交出去的 agent 用的参考手册：四类执行者（脚本、判定模型 jev、直接调用的 LLM API、harness+model）的特点与优劣；各模型、各 harness 的能力、局限、搭配经验和接口；本机入口怎么从 $AGENTS_DATA_ROOT/local/ 解析与调用（dispatch.sh、llm-call.py、verify.sh）。只讨论厂商通用知识且不涉及本机资源时不使用。
---

# 执行模型组合

本 skill 是参考手册：讲各执行者、模型、harness 的优劣和接口，以及本机入口怎么查、怎么调。选谁由调用方按任务判断，或由用户点名；手册不替任务指定模型，因为模型的有效性随任务、通道和版本变化，写死的名字会过时，也会误导。

- 四类执行者的特点与取舍：[executors.md](references/executors.md)。
- 模型的能力与局限、与 harness 的搭配经验：[models.md](references/models.md)。
- harness 的能力、厚薄、原生命令：[harnesses.md](references/harnesses.md)。
- 本机清单 `$AGENTS_DATA_ROOT/local/`（模板 [local.example/](references/local.example/)）：`bindings.tsv` 是本机可执行的 `API + harness + model` 组合；`apis.md` 是各通道的价格、免费与否、协议和实测；`models.md`、`harnesses.md` 是本机接线与本机实测；`runtime-defaults.tsv` 是本机的 effort/context 默认。清单描述的是跑 harness 的那台机器；当前机器不是那台时（harness 不在 PATH），经那台机器的 shell 桥执行，不在当前机器照抄命令。

## 同 harness 优先派 native sub-agent

当前 harness 能承载要用的模型时，派它自己的 sub-agent，不经外部 CLI。native sub-agent 共享当前工作区和权限，不需要 wrapper，启动开销小；能续用同一个 child（Claude Code 的 SendMessage、Codex 的 `followup_task`），局部上下文不丢；effort 和 context 由宿主带上，不会因为 wrapper 漏传参数而悄悄变了。

跨 harness 才经 `dispatch.sh`：用户点名了另一套组合、当前 harness 承载不了要用的模型，或者要在外部并行。

## 本机入口怎么解析

1. 用户的口语名先还原成 `harness + model`（别名见下表），再查 `local/bindings.tsv`（或 `scripts/dispatch.sh --list-bindings`）。
2. `verified` 直接执行。`configured` 先用 `scripts/verify.sh` 跑最小探针，通过才晋级。没有命中就报告缺口，不换成邻近的组合：换了，用户以为跑的是 A，实际跑的是 B，结果和成本都对不上。
3. 同一 `harness + model` 有多条通道时，`bindings.tsv` 的行序就是本机所有者定的通道优先级：取最靠前的 `verified` 行，只在出错或额度用完时降到下一条，或者用户点名某条。不因为慢或想对比而换，因为换通道可能同时换了计费和模型版本；降级要在交还里写明原因。
4. 调用前查 `local/apis.md` 的免费段。免费按"通道＋模型"判，同一通道上不同模型的价格可以不同。
5. 入口：harness 用 `scripts/dispatch.sh`（单条）或 `scripts/parallel.sh`（JSONL 批量）；LLM API 用 `scripts/llm-call.py`；判定模型见 `local/apis.md` 的 jev 段；新组合晋级用 `scripts/verify.sh`。effort/context 未指定时，`dispatch.sh` 按 `local/runtime-defaults.tsv` 补；native sub-agent 要自己带。

| 说法 | 指的是 |
|---|---|
| cc | harness `claude-old`，不是模型名 |
| agy | harness `antigravity-cli` |
| ds-flash / ds-pro | `deepseek-flash` / `deepseek-pro` |
| glm | `glm-5.3-flash` |
| gemini | `gemini-3.8-flash` |
| luna / terra / sol | GPT 系列的三个档位，具体版本以 `bindings.tsv` 为准 |
| grok | Grok 系列，具体版本以 `bindings.tsv` 为准 |

`cc`、`agy` 是 harness，模型另说；harness、model、api 三轴分开，api 由本机清单决定。

## 派多个组合

批量或高 fan-out 先取得**用户**确认。JSONL → `parallel.sh`；每行唯一 `job_id` / `out` / `stderr`。示例路径用 `$SKILL`，三元组从本机 `--list-bindings` 抄，不要抄死另一台机器的 api_id。

## 约束

- 晋级只用 `scripts/verify.sh`（探针规范化后须为 `PING`）。
- OpenRouter / `/models` 只用来发现候选，不产生 binding：目录里有，不等于本机的 harness 能接上。
- 本机数据根：优先 `$AGENTS_DATA_ROOT`（`local/` + `private/`，三家 harness 共用）；否则 `CLAUDE_PLUGIN_DATA` / `PLUGIN_DATA` / `GROK_PLUGIN_DATA`；再否则插件树内 `references/local` + `private/`。细覆盖见 `AGENTS_LOCAL_ROOT` / `AGENTS_CREDENTIALS_FILE`。凭据值不进 prompt、文档、回显或仓库。

## 交还格式

至少报告：实际 API/harness/model、profile 或 alias、effort/context、原生或 wrapper、退出状态、session（若有）、是否降级及原因。请求模型与观察到的模型分列。

完成判据：每次调用能由上述字段还原 binding 与运行参数；失败与漂移作偏差列出（未发现则带查证链的 null 报告）。推荐语不能充当完成。
