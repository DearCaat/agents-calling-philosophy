# 模型卡、官方参考成本与候选发现

核实日期：2026-09-03。此文件维护文浩正在使用或倾向使用的稀疏模型卡，不维护全量模型目录。模型是否能在本机执行，只以 [bindings.tsv](local/bindings.tsv) 中某条 API 的 exact binding 为准。

## 阅读口径

- **模型官方参考成本**：模型厂商的公开 direct-API 价，单位均为 USD / 1M tokens，除非另注。它是模型层的可比较事实。
- **API 实际价格**：本机具体通道真正向文浩结算的价格、套餐或免费额度，只写在 [apis.md](local/apis.md)。它可能不同于模型参考成本，也可能未知。
- **优点 / 限制**：每项明确是厂商公开资料（`official`）、本机成功调用（`local-test`）还是文浩长期使用经验（`local-experience`）。厂商定位不是独立 benchmark 结论。
- **本机状态**：`verified` / `configured` / `degraded` 只由 registry 表示；`degraded` 是该 exact binding 最近真实 probe 失败或模型身份不能可靠还原，不能作为执行候选。`candidate` 表示模型卡存在，但尚无本机 API+harness binding。

## 本机调用口径（local-experience）

以下是文浩的使用经验，**不是** binding，也不能把未 `verified` 的推荐当成已执行。派工步骤在 [SKILL.md](../SKILL.md)，角色排序只以本节为准。API、harness、model 三轴分开：同一模型可走不同 API+harness；Grok 的入口固定为 grok-build。

协议与通道实参见本机 [local/apis.md](local/apis.md) / [local/harnesses.md](local/harnesses.md)。下面是**调用哲学**（跨机器仍用；本机有无 binding 另查 registry）：

- 官方 Anthropic Claude 默认不派。Worker 可复用：能 resume 就 resume；连续失败换新 worker。`dispatch.sh` 只是一次性包装。
- **当前 harness 用当前 harness**：Codex+GPT → Codex `spawn_agent`；Grok 是例外，一律走 grok-build。内部循环仍用 sub-agent。
- `grok-4.3` 不是可派模型。

角色（`local-experience` / 哲学）：

- **杂事**：luna-max（Codex）首选；其次 ds-flash / glm-flash。Grok 与 gemini 不当杂事默认。杂事 agent 可写代码；不自主改 skill/模型卡/对外叙述。
- **确定任务执行器**：Grok Build；具体 model 与 API 只从本机 registry 的 verified binding 解析。`claude-old` 不承载 Grok。
- **GPT** 默认 Codex；**DS** 默认 Codex；**Kimi** 以本机 registry 已 verified 的 Codex 行为准。
- **审查**：terra / sol（Codex）；关键用 sol。视觉：luna → terra。
- **知识性任务**：`gemini-3.8-flash-high` + `claude-old`。不当杂事，不替代 Grok 执行器。
- Codex `workspace-write` 不能用来判断本机 loopback 是否存活（见 local harnesses）。

默认 effort（运行参数，不是 binding；用户显式指定则用指定值）。`dispatch.sh` 未传 `--effort` 时套同一组；native sub-agent 必须自己带：

- luna → `max`
- terra → `high`
- sol → `medium`；特别重要或特别困难时再提高
- grok 系列 → `high`
- flash 系列（含 ds-flash、glm-flash、`gemini-3.8-flash-high`）→ `high`

未列入的模型不设默认，沿用 harness/profile。

默认有效 context（**调用哲学**；窗口与 autocompact 绑死，不是 binding）。规范源：[runtime-defaults.tsv](runtime-defaults.tsv)，`dispatch.sh` 读取同一张表：

- Codex GPT（luna / terra / sol）：catalog 维持 `272000`（官方 1.05M 本机 Codex 不兑现）。
- flash / 杂事（ds-flash、glm-flash、`gemini-3.8-flash-high`，以及 Codex flash catalog）：有效窗口 `272000`；cc 侧 `--autocompact 272k` + 同值 `CLAUDE_CODE_MAX_CONTEXT_TOKENS`。
- `dispatch.sh` 只为 cc 的非 Grok 路径套上述窗口；Codex 靠 catalog 的 `context_window` / `max_context_window`。Grok Build 使用其原生上下文管理。

## GPT-5.6：Luna / Terra / Sol

- 身份：`gpt-5.6-luna`、`gpt-5.6-terra`、`gpt-5.6-sol`。官方模型目录将 Sol 定位为复杂推理和代码的旗舰，Terra 定位为能力/成本平衡，Luna 定位为成本敏感的高吞吐工作负载（`official`，见 [OpenAI Models](https://developers.openai.com/api/docs/models)）。这不是本机任务上的相对 benchmark。
- 公开能力：GPT-5.6 系列支持 text/image input、text output、多语言与 vision；官方列出 Functions、Web search、File search 和 Computer use（`official`）。本机通道是否开放这些工具另由 API 和 harness 决定。
- 模型官方参考成本：Luna 输入 `$0.20` / 输出 `$1.20`；Terra `$2.00` / `$12.00`；Sol `$4.00` / `$20.00`（`official`，[OpenAI Pricing](https://developers.openai.com/api/docs/pricing)）。
- 本机限制：官方 GPT-5.6 context 为 1,050,000、最大输出 128,000；本机 Codex catalog **有意**维持 `272000`（2026-09-04 口径），且没有 `ultra`。实际入口和状态见 registry（`local-test/configured`）。
- 本机经验（`local-experience`）：luna-max 干杂事、价格极低；可以写代码，不自主改 skill / 模型卡 / 对外叙述。terra 略逊于 sol、视觉较好，审查常用；sol 最贵，审查最佳，关键内容才用。GPT 默认走 Codex + 8317，不走官方 Claude。
- 其他 GPT catalog：`gpt-5.5`、`gpt-5.4`、`gpt-5.4-mini`、`gpt-5.3-codex-spark` 出现在本机 Luna catalog，但目前没有 registry binding；它们不是可直接执行的现成入口。

## DeepSeek V4：Flash / Pro

- 身份：DeepSeek 官方 stable API IDs 是 `deepseek-v4-flash` 与 `deepseek-v4-pro`；当前版本分别为 DeepSeek-V4-Flash-0731、DeepSeek-V4-Pro-0813（`official`，[Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/)）。官方说明 stable ID 会路由到最新对应版本。
- 公开能力：两者为 1M context、最大输出 384K，支持 thinking / non-thinking、JSON output 与 Tool Calls（`official`）。`deepseek-v4-flash-vision-exp` 是单独的实验性图像输入模型，不能因为 Flash 文本模型同名而推断视觉可用。
- 模型官方参考成本：Flash 的 cache-hit / cache-miss input / output 为 off-peak `$0.007` / `$0.22` / `$0.66`，peak `$0.014` / `$0.44` / `$1.32`；Pro 分别为 off-peak `$0.022` / `$0.66` / `$1.98`，peak `$0.044` / `$1.32` / `$3.96`（`official`）。Peak 为周一至周五 01:00–04:00、06:00–10:00 UTC；其余为 off-peak。
- 本机限制：代理真实请求 ID 不是统一值。registry 中同时存在 `deepseek-v4-flash`、`deepseek-v4-flash-0731` 与 `deepseek-v4-flash-responses`；它们不能互换。PJLab 路径对文浩免费但不改变上述模型官方参考成本（`local-test`，见 [apis.md](local/apis.md)）。
- 本机经验（`local-experience`）：DS 默认 Codex。Flash 可干杂事（次于 luna-max）；可以写代码，不自主改 skill / 模型卡 / 对外叙述。

## Kimi K3

- 身份：官方 ID 为 `kimi-k3`。厂商将其定位为长程代码、端到端知识工作和深度推理；公开 2.8T 参数、native visual understanding 与 1,048,576-token context（`official`，[Kimi Models](https://platform.kimi.ai/docs/models.md)）。
- 公开能力与限制：K3 始终 reasoning，可设 `reasoning_effort=low|high|max`；支持 context caching、ToolCalls、JSON Mode、structured output 和动态加载工具（`official`，[Kimi K3 Pricing](https://platform.kimi.ai/docs/pricing/chat-k3.md)）。该页明确提示 `web_search` 正在更新，不应把它当成当前可靠能力。
- 模型官方参考成本：cache-hit input `$0.30`、cache-miss input `$3.00`、output `$15.00`（税前；`official`）。
- 本机状态：`pjlab-ds + codex-cli + kimi-k3` 已 `verified`（2026-08-28；profile `deepseek-0731`、low、read-only、exit 0）。PJLab 的 `/models` 当日列出该 ID；CLI JSONL 没有返回 model 字段，故不把请求 ID 当作 CLI observed ID。独立 Responses 探针返回 `kimi-k3`。本 route 仅注册 text、禁用 search/parallel tool calls，实际 context/output 容量未压测；证据见 [binding-evidence.tsv](local/binding-evidence.tsv)（`local-test`）。
- 本机经验（`local-experience`）：Kimi 可派的是上述 Codex binding。8317 目录无 kimi；插件不再把 15721 当通道。

## GLM-5.3-Flash

- 身份：官方 ID 为 `glm-5.3-flash`。厂商说明其为 GLM-5 系列首个 native multimodal 模型，320B 总参数、18B activated parameters，并支持 1M context（`official`，[GLM-5.3-Flash](https://docs.z.ai/guides/vlm/glm-5.3-flash)）。
- 公开能力与限制：原生输入覆盖 image、video、file；支持 Function Calling 与 structured output。`thinking.type` 只支持 `enabled`，不能关闭（`official`）。这些是厂商 API 能力，目标 API/harness 是否能表达须另验。
- 模型官方参考成本：cache-hit `$0.015`、input `$0.075`、output `$0.25`；这是 50% promo 价，原价依次 `$0.03` / `$0.15` / `$0.50`。官方写明促销至 2026-09-09 24:00 UTC+8，cache storage 限时免费（`official`，[Z.AI Pricing](https://docs.z.ai/guides/overview/pricing.md)）。
- 本机状态：`pjlab-ds + codex-cli + glm-5.3-flash` 已 `verified`（2026-08-28；profile `deepseek-0731`、low、read-only、exit 0）。PJLab 的 `/models` 当日列出该 ID；CLI JSONL 没有返回 model 字段，独立 Responses 探针返回 `glm-5.3-flash`。本 route 只注册 text、禁用 search/parallel tool calls；官方 image/video/file 与 Function Calling 仍不能据此认为由 PJLab+Codex 开放，容量未压测；证据见 [binding-evidence.tsv](local/binding-evidence.tsv)（`local-test`）。
- 本机经验（`local-experience`）：杂事可用，次于 luna-max；可以写代码，不自主改 skill / 模型卡 / 对外叙述。

## Grok-4.5 / Grok-4.6

- 公开能力：xAI 当前将 Grok-4.6 定位为代码和一般文本任务的首选，并称其为最智能、最快的 Grok；这只是 `official` 厂商定位，不是本机 benchmark。两者官方 context 均为 500K。
- 模型官方参考成本：在 prompt 少于 200K tokens 时，Grok-4.5 为 input/cache/output `$2.00` / `$0.30` / `$6.00`，Grok-4.6 为 `$2.00` / `$0.50` / `$6.00`；达到 200K 后两者 input/output 都为 `$4.00` / `$12.00`，cache 分别为 `$0.60` / `$1.00`（`official`，[xAI Models](https://docs.x.ai/developers/models.md)）。
- 本机限制：`openai-local-8317` 上 Codex / grok-build 走 Responses，Claude Code 走同一 API 的 Messages 口、真 grok slug。`grok-build-managed` 的 `grok-4.5` 已验证请求曾 observed 为 `grok-4.6-build`；`grok-direct-api` 的 `grok-4.5` 于 2026-09-03 observed `grok-4.5-build`。通道实际账单未知，不能以 xAI 官方价反推（`local-test/configured`，见 [apis.md](local/apis.md)）。
- 本机经验（`local-experience`）：有确定任务时 Grok 是首选执行器，harness 为 `grok-build`。具体 model 与通道由本机 registry 的 verified binding 决定。Grok 不当杂事默认（成本高于 luna）。`grok-4.3` 不是可派模型；`claude-old` 不承载 Grok。

## Gemini 3.8 Flash

- 身份：厂商 ID 为 `gemini-3.8-flash`（`official`，[Gemini 3.8 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash)）。8317 目录与本插件 binding 的 slug 是 `gemini-3.8-flash-high`（thinking high）。两者不要当成可以互换的未核实 ID；cc `--model` 必须发 8317 的 slug。
- 公开能力：官方 context 1,048,576、最大输出 65,536；thinking 档 low/medium/high（默认 medium）；输入 text/image/video/audio/PDF，输出 text（`official`，同上）。本机 cc 通道是否打开多模态未压测。
- 模型官方参考成本：Gemini API 标价至 2026-12-31 为 input `$0.75` / output `$3.75`（含 thinking tokens）/ 1M；2027-01-01 起翻倍（`official`，[Gemini API Pricing](https://ai.google.dev/gemini-api/docs/pricing)）。**不是** 8317 通道账单。
- 本机限制：8317 `/models` 有 `gemini-3.8-flash-high`，无无后缀的 `gemini-3.8-flash`。2026-09-03 裸 HTTP `/v1/messages` 200，observed `gemini-3.8-flash`，文本 PING。同日 cc `verify.sh`：120s 超时（exit 124，unrecognized_model / 未知窗口），加长后 exit 1 为上游 429 `RESOURCE_EXHAUSTED`。registry 仍为 `configured`。请求 slug 与 observed `gemini-3.8-flash` 必须分列。
- 本机经验（`local-experience`）：知识性 flash，世界知识与专业知识好于其它 flash，成本同档。默认 harness 是 `claude-old`（8317）。不当杂事，也不替代 Grok 当确定任务执行器。

## Claude aliases

- 本机 `claude-old` 仍可能在 UI 里显示 `opus`/`sonnet`/`haiku`/`fable`。dispatcher 将这些 alias 全部钉到当前已登记的非 Grok 模型；它们不是模型身份，也不能作为 Grok 的入口。
- 8317 目录含 `claude-sonnet-4-6` 等 slug；它们不是本 bundle 的 binding。2026-08-28 经旧 15721 通道的 `claude-sonnet-4-6` probe 身份冲突，见 [binding-failures.tsv](local/binding-failures.tsv)（历史记录，不是当前 registry）。
- 不写 Anthropic 官方参考成本。默认不派官方 Claude。Claude Code 的压缩优势写在 [harnesses.md](harnesses.md)。Kimi 可派的是 Codex + pjlab，不是 cc。

## OpenRouter：动态候选，不是本机执行入口

OpenRouter Models API 可动态返回候选 ID、加入时间、input/output 模态、context、**OpenRouter API 路径价格**、top provider、支持参数、失效日期及部分 benchmark。使用提供的只读工具：

```bash
scripts/openrouter-models.sh newest --limit 20
scripts/openrouter-models.sh popular --requires tools,reasoning --limit 20
scripts/openrouter-models.sh search kimi
scripts/openrouter-models.sh search glm
scripts/openrouter-models.sh inspect moonshotai/kimi-k3
```

`newest` 只表示加入时间，`top-weekly` 只表示最近一周经 OpenRouter 处理的 token 使用量。OpenRouter price、provider、吞吐和延迟是 API 路径事实，不能覆盖厂商模型参考成本，也不能覆盖 PJLab、NewAPI、本地代理或宿主管理通道。其 benchmark 只覆盖部分模型和 arena，不能单独证明模型适合代码、论文或病理任务。

## 候选进入本机 binding 的证据

一个新模型只有满足以下事实才能进入 [bindings.tsv](local/bindings.tsv)：

1. 在模型卡中记录精确厂商 ID、官方能力、模型官方参考成本、来源和日期。
2. 目标 API 确实暴露该请求 model ID，并在 [apis.md](local/apis.md) 记录协议、API 实际价格/额度与 credential_ref。
3. 目标 harness 有明确 wire protocol、认证和 selector adapter；此时可登记为 `configured`。
4. 最小实际调用成功后才改为 `verified`，并保存请求 model、observed model、日期、退出状态和已知限制。

OpenRouter 自身若作为 API，还要验证目标 harness。其 Responses API 是 stateless，拒绝 `store=true` 和非空 `previous_response_id`；provider 默认路由可能 fallback，精确复现需要确认 harness 能表达 `allow_fallbacks=false`、`provider.only` 或等效账号策略。来源：[Responses API](https://openrouter.ai/docs/api_reference/responses/overview) 与 [Provider routing](https://openrouter.ai/docs/guides/routing/provider-selection)。
