# 模型卡：能力、局限、搭配与候选发现

核实日期：2026-09-03（各条另注日期的以该日期为准）。此文件只收文浩正在使用或考虑使用的模型，不维护全量目录。模型在本机能不能执行，只以 `local/bindings.tsv` 中某条 API 的 exact binding 为准；本机通道、本机限制与本机实测在 `local/models.md`、`local/apis.md`。

## 阅读口径

- **模型官方参考成本**：模型厂商的公开 direct-API 价，单位均为 USD / 1M tokens，除非另注。它是模型层的可比较事实，不是本机通道的账单；本机通道实际收不收费、收多少只写在 `local/apis.md`。
- **来源标签**：每条能力或局限标明是厂商公开资料（`official`）、本机成功调用或实测（`local-test`），还是文浩长期使用经验（`local-experience`）。厂商定位不是独立 benchmark；本机实测只说明那一次、那条通道上的情况。
- **本机状态**：`verified` / `configured` / `degraded` 只由 registry 表示。

## 模型与 harness 的搭配

同一个模型放进不同的 harness，效果和能不能跑通都不一样。厂商自家的 harness 是按自家模型的工具调用格式、提示和推理参数调过的，配自家模型通常最顺；跨厂商搭配要靠网关转换协议，模型名认不认、参数会不会被丢掉，只能实测。下表是本机的搭配经验和原因；某个组合在本机的验证与失败记录见 `local/bindings.tsv`、`local/binding-failures.tsv` 与 `local/models.md`。

| 模型 | 搭配的 harness | 原因 |
|---|---|---|
| GPT | Codex（在 Codex 宿主内用 `spawn_agent`，外部用 `codex-cli`） | Codex 是 OpenAI 自家 harness，走 Responses 协议；宿主内派 child 不经 wrapper，共享工作区 |
| Grok | `grok-build` | xAI 自家 CLI；经 `claude-old` 承载时，`claude-old` 不认 grok 的模型名，本机实测不通（`local-test`） |
| Gemini | `antigravity-cli`；需要输出约束时也可用 `codex-cli` | agy 是 Google 自家 CLI，能联网，长上下文吞吐快；经 `claude-old` 承载时本机最小探针多次失败（超时、上游 429，`local-test`）；`codex-cli` 经网关能跑，可用 `--output-schema` 约束输出 |
| DeepSeek | `dsh`，其次 `codex-cli` | dsh 是 DeepSeek 自家 harness，向 provider 说 Anthropic Messages，只认 Messages 的通道也能接；`codex-cli` 要求通道支持 Responses 协议（2026-10-19 文浩定此顺序） |
| GLM | `dsh` | 同上：dsh 能接只认 Messages 的通道；本机 glm 所在通道 codex 接不上，`claude-old` 被上游拒绝（`local-test`，见 `local/harnesses.md`） |
| Claude | Claude Code（`claude-old`） | Anthropic 自家 harness；本机 `claude-old` 也用来承载走 Messages 协议的其他模型 |

## GPT-5.6：Luna / Terra / Sol

- 身份：`gpt-5.6-luna`、`gpt-5.6-terra`、`gpt-5.6-sol`。官方将 Sol 定位为复杂推理和代码的旗舰，Terra 为能力/成本平衡，Luna 为成本敏感的高吞吐（`official`，[OpenAI Models](https://developers.openai.com/api/docs/models)）。
- 公开能力：text/image 输入、text 输出、多语言与 vision；官方列出 Functions、Web search、File search 和 Computer use（`official`）。本机通道是否开放这些工具另由 API 和 harness 决定。官方 context 1,050,000、最大输出 128,000。
- 模型官方参考成本：Luna 输入 `$0.20` / 输出 `$1.20`；Terra `$2.00` / `$12.00`；Sol `$4.00` / `$20.00`（`official`，[OpenAI Pricing](https://developers.openai.com/api/docs/pricing)）。
- 能力与局限（`local-experience`）：Luna 价格极低，能写代码；Terra 能力略逊于 Sol，视觉较好；Sol 能力最强，也最贵。

## DeepSeek V4：Flash / Pro

- 身份：官方 stable API ID 是 `deepseek-v4-flash` 与 `deepseek-v4-pro`，会路由到最新对应版本（`official`，[Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/)）。
- 公开能力：1M context、最大输出 384K，支持 thinking / non-thinking、JSON output 与 Tool Calls（`official`）。`deepseek-v4-flash-vision-exp` 是单独的实验性图像输入模型，不能因为 Flash 文本模型同名就推断它能看图。
- 模型官方参考成本：Flash 的 cache-hit / cache-miss input / output 为 off-peak `$0.007` / `$0.22` / `$0.66`，peak `$0.014` / `$0.44` / `$1.32`；Pro 分别为 off-peak `$0.022` / `$0.66` / `$1.98`，peak `$0.044` / `$1.32` / `$3.96`（`official`）。Peak 为周一至周五 01:00–04:00、06:00–10:00 UTC。
- 缓存（`official`，[KV cache](https://api-docs.deepseek.com/guides/kv_cache)）：默认对所有用户开启、不分接口；新前缀要被看到两次才持久化，构建要几秒，不保证命中。

## Kimi K3

- 身份：官方 ID 为 `kimi-k3`。厂商定位为长程代码、端到端知识工作和深度推理；公开 2.8T 参数、native visual understanding 与 1,048,576-token context（`official`，[Kimi Models](https://platform.kimi.ai/docs/models.md)）。
- 公开能力与限制：始终 reasoning，可设 `reasoning_effort=low|high|max`；支持 context caching、ToolCalls、JSON Mode、structured output（`official`，[Kimi K3 Pricing](https://platform.kimi.ai/docs/pricing/chat-k3.md)）。该页提示 `web_search` 正在更新，不能当作可靠能力。
- 模型官方参考成本：cache-hit input `$0.30`、cache-miss input `$3.00`、output `$15.00`（税前；`official`）。

## GLM-5.3-Flash

- 身份：官方 ID 为 `glm-5.3-flash`。厂商说明其为 GLM-5 系列首个 native multimodal 模型，320B 总参数、18B activated，1M context（`official`，[GLM-5.3-Flash](https://docs.z.ai/guides/vlm/glm-5.3-flash)）。
- 公开能力与限制：原生输入覆盖 image、video、file；支持 Function Calling 与 structured output。`thinking.type` 只支持 `enabled`，不能关闭（`official`）。目标 API/harness 是否能表达这些能力须另验。
- 模型官方参考成本：cache-hit `$0.015`、input `$0.075`、output `$0.25`（50% promo 价，原价依次 `$0.03` / `$0.15` / `$0.50`；`official`，[Z.AI Pricing](https://docs.z.ai/guides/overview/pricing.md)）。
- 局限（`local-test`，2026-10-21）：思考关不掉，`max_tokens` 给小了会被推理吃光，回空答案。

## Grok-4.5 / Grok-4.6

- 公开能力：xAI 将 Grok-4.6 定位为代码和一般文本任务的首选、最智能最快的 Grok；这只是厂商定位，不是本机 benchmark。两者官方 context 均为 500K（`official`）。
- 模型官方参考成本：prompt 少于 200K tokens 时，Grok-4.5 为 input/cache/output `$2.00` / `$0.30` / `$6.00`，Grok-4.6 为 `$2.00` / `$0.50` / `$6.00`；达到 200K 后两者 input/output 都为 `$4.00` / `$12.00`，cache 分别为 `$0.60` / `$1.00`（`official`，[xAI Models](https://docs.x.ai/developers/models.md)）。
- 局限（`local-test`）：请求的模型与实际回包的模型可能不一致，需要精确复现时两者都要记录。

## Gemini 3.8 Flash

- 身份：厂商 ID 为 `gemini-3.8-flash`（`official`，[Gemini 3.8 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash)）。网关上可能另有带 thinking 档后缀的 slug，与厂商 ID 不能当成可以互换。
- 公开能力：context 1,048,576、最大输出 65,536；thinking 档 low/medium/high（默认 medium）；输入 text/image/video/audio/PDF，输出 text（`official`）。
- 模型官方参考成本：至 2026-12-31 为 input `$0.75` / output `$3.75`（含 thinking tokens）/ 1M；2027-01-01 起翻倍（`official`，[Gemini API Pricing](https://ai.google.dev/gemini-api/docs/pricing)）。
- 能力与局限：世界知识与专业知识好于其它 flash，成本同档（`local-experience`）；论文存在性大体可靠，但引句、DOI、机构、日期会编，引用要回原文核（`local-test`）。

## Claude aliases

- `claude-old` 的 UI 可能显示 `opus`/`sonnet`/`haiku`/`fable`。dispatcher 把这些 alias 全部钉到请求的模型；它们不是模型身份，不能用来判断实际跑的是哪个模型。

## 判定模型：jev

- 身份：返回概率的判定模型服务，不生成文字；只在给定的题型里判（选项 choice、是/否/不确定 noul、打分 score），不能抽取。网关上有哪些模型、deadline 多少会变，有的一天内就变，用前先查 `/v1/models`；部分模型能看图。本机入口见 `local/apis.md`。
- 能力与局限（`local-experience`）：证据就在输入里、答案闭合的问题较可靠；没有标准答案的开放评价、依赖多步推理的规则判断接近随机。原因是它只给概率、不展开推理，问题越开放、越依赖多步推理就越不可靠。不同模型的阈值不能互用。

## OpenRouter：动态候选，不是本机执行入口

OpenRouter Models API 可动态返回候选 ID、加入时间、input/output 模态、context、**OpenRouter API 路径价格**、top provider、支持参数、失效日期及部分 benchmark。使用提供的只读工具：

```bash
scripts/openrouter-models.sh newest --limit 20
scripts/openrouter-models.sh popular --requires tools,reasoning --limit 20
scripts/openrouter-models.sh search <关键词>
scripts/openrouter-models.sh inspect <provider/model-id>
```

`newest` 只表示加入时间，`top-weekly` 只表示最近一周经 OpenRouter 处理的 token 使用量。OpenRouter price、provider、吞吐和延迟是 API 路径事实，不能覆盖厂商模型参考成本，也不能覆盖本机通道。其 benchmark 只覆盖部分模型和 arena，不能单独证明模型适合代码、论文或病理任务。

## 候选进入本机 binding 的证据

一个新模型只有满足以下事实才能进入 `local/bindings.tsv`：

1. 在模型卡中记录精确厂商 ID、官方能力、模型官方参考成本、来源和日期。
2. 目标 API 确实暴露该请求 model ID，并在 `local/apis.md` 记录协议、API 实际价格/额度与 credential_ref。
3. 目标 harness 有明确 wire protocol、认证和 selector adapter；此时可登记为 `configured`。
4. 最小实际调用成功后才改为 `verified`，并保存请求 model、observed model、日期、退出状态和已知限制。

OpenRouter 自身若作为 API，还要验证目标 harness。其 Responses API 是 stateless，拒绝 `store=true` 和非空 `previous_response_id`；provider 默认路由可能 fallback，精确复现需要确认 harness 能表达 `allow_fallbacks=false`、`provider.only` 或等效账号策略。来源：[Responses API](https://openrouter.ai/docs/api_reference/responses/overview) 与 [Provider routing](https://openrouter.ai/docs/guides/routing/provider-selection)。
