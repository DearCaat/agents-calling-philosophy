# CPA 工具网关：tool-call.py

CPA 在 8317 上提供工具网关，路径为 `/tool/<名字>/...`。客户端是 `scripts/tool-call.py`，每个工具一个模块（`scripts/tools/<名字>.py`）。当前工具：

- `jev`：判定（选项、是/否、打分），普通题型，字符串 state。
- `vela`：判定，接受对象 state 与 span 题（抽取式标注）。
- `mineru`：文档解析（MinerU V1 接口的透传，封装为提交、轮询、下载）。
- `oxr`：文档解析（OXR 解析器，每个文件一次同步请求，返回 Markdown 与 JSON）。

工具列表、接口和模型 id 以服务端为准，这里不写死：

```
tool-call.py --list                  # 已启用的工具（GET /tool/）
tool-call.py jev --schema            # 接口、限制与常见错误（GET /tool/jev/schema）
tool-call.py jev --list-models       # 模型 id（GET /tool/jev/models）
tool-call.py jev --health            # 健康状态（GET /tool/jev/health）
```

`--list-models`、`--health` 和 `--schema` 对各工具同样可用。

## 通用用法

批量输入是 JSONL，每行一个对象，必须有 `id`（同一文件内唯一）。结果逐条追加到 `--out`；同一命令重跑时，已成功（`ok` 为 true）的 id 会跳过，所以中断后直接重跑即可续跑。

```
tool-call.py jev --in items.jsonl --out results.jsonl --model <模型id>
tool-call.py vela --in items.jsonl --out results.jsonl --model <模型id>
tool-call.py mineru --file paper.pdf --out results.jsonl --out-dir ./parsed
```

常用选项：

- `-j N`：并发。默认 jev 1024、vela 64、mineru 4、oxr 4。客户端不替网关限流，排队与退避由服务端做。
- `--timeout S`：单个请求的秒数。jev、vela 默认 160；mineru 默认 3600，同时也是单个文件的总等待上限；oxr 默认 3600。
- `--attempts N`：429 和 503 `model_warming` 的最多尝试次数，默认 8，按 `Retry-After` 退避。
- `--stop-after-failures N`：连续失败 N 次后停止发起新请求，默认 10。
- `--dry-run`：打印第一条请求的预览，不发送任何请求。
- `--in -`：从标准输入读 JSONL。

退出码：全部成功为 0；有失败或未开始的条目为 1；输入或参数错误为 2。

重试规则：429 与 503 `model_warming` 按 `Retry-After` 退避后重试；其他状态（包括 502、504）与超时不重放，记为失败，因为服务端可能仍在处理。

路由与凭据：`--api` 默认 `tool`，从本机清单 `local/api-routes.tsv` 解析。凭据只按清单引用，任何输出（包括错误信息）都不含凭据值。

## jev

- 入口：`POST /tool/jev/systemone`。
- 输入：`state` 为字符串；`questions` 为非空对象，题型为 noul、choice、score；`model` 写在行内或用 `--model`；可选 `images`，为图片文件路径或 base64 data URI，`--max-side PIXELS` 可先缩小（需要 Pillow）。
- 发送前本地拒收（整批都不发送）：`state` 是对象；任一题 `type` 为 `span`；模型名带上游前缀。模型名只写 id，上游由 CPA 选择。对象 state 和 span 题改用 `vela`。
- 返回的 `answers` 按题给出概率。阈值按任务另定，不同模型的概率不能共用阈值。
- 默认 `-j 1024`，超时 160 秒。

输入行示例：

```
{"id":"s1","state":"Fix the off-by-one error in this loop.","questions":{"kind":{"type":"choice","instructions":"What kind of work is this?","criteria":{"code":"Writing or fixing code","math":"Mathematics","chat":"Anything else"}}}}
```

## vela

- 入口：`POST /tool/vela/systemone`。只接受 vela 模型，`model` 必填（行内或 `--model`）；模型 id 用 `tool-call.py vela --list-models` 取。其他模型会被服务端拒绝，应改用 `jev`。
- 输入：`state` 为字符串，或对象 `{"context": "...", "answer": "..."}`。`questions` 可以混用普通题和 `span` 题。
- span 题：`criteria` 列出要标注的标签，写成列表（如 `["city", "country"]`）或以标签为键的对象（如 `{"unsupported": null}`）。
- span 的偏移：`start`、`end` 为字符偏移。对象 state 时，偏移相对 `answer`；字符串 state 时，相对该字符串。
- evidence 标签：对象 state 时，返回的是 `answer` 里的句子，不指向 `context`（2026-10-28 实测）。
- 无官方兜底：vela 请求只由 vela 模型应答，没有其他通道回退。
- 默认 `-j 64`：服务端会排队处理，并发不必设高。超时 160 秒。

## mineru

- 入口：`/tool/mineru/...`，是 MinerU V1 接口的透传。`tool-call.py mineru` 把"提交、轮询、下载"封装成一个批量命令。
- 输入：JSONL 每行 `{"id", "file", "tier"?, "output_formats"?}`；或命令行 `--file PATH`（可重复，id 取文件名），此时必须给 `--out-dir`。
- `id` 只用字母、数字、`_`、`.`、`-`，它会成为结果文件名。`file` 是本地文件路径（相对当前目录），单个文件不超过 200 MiB，超过则发送前拒收。
- `--tier` 默认 `advanced`。可选值用 `GET /tool/mineru/tiers` 查看。
- `output_formats` 为列表。含 `"zip"` 时下载 zip 包到 `<out-dir>/<id>.zip`，否则下载 JSON 到 `<out-dir>/<id>.json`。格式名以 MinerU 接口为准。
- 流程：把文件的原始字节提交到 `POST /tool/mineru/tasks`，得到 `task_id`；先等 5 秒再轮询状态，之后间隔加倍，最长 60 秒；状态为 `completed` 时下载结果，为 `failed` 时记为失败；总等待超过 `--timeout` 时记为超时。
- 结果在上游只保留一段时间，取到后应及时处理。
- 输出 JSONL 每行记录：`id`、`ok`、`task_id`、`task_status`、`result_file`、`status`（最后一次 HTTP 状态）、`attempts`、`latency_s`、`error`。
- 默认 `-j 4`：上游一次只解析一个任务，其余排队。每个并发文件都会整体读入内存，并发高时注意内存。
- `--poll-interval S`：首次轮询前的等待秒数，默认 5，之后翻倍，最长 60。

## oxr

- 入口：`POST /tool/oxr/parse/sync`，`multipart/form-data`。每个文件一次同步请求，结果直接写入 `--out-dir`。
- 输入：JSONL 每行 `{"id", "file", "output_formats"?, "element_formats"?}`；或命令行 `--file PATH`（可重复，id 取文件名），此时必须给 `--out-dir`。`id` 规则同 mineru，它成为结果文件名。
- `file` 是本地 PDF 或图片，整个读入内存，单个不超过 200 MiB，超过则发送前拒收。构造请求体时每个并发条目约占两倍文件大小的内存，并发 4 时注意。
- `output_formats`：`markdown`、`json` 的列表，默认两者都要。
- `element_formats`：可选 JSON 对象，原样传给服务端；不写则用服务端默认值。`--element-formats '<json>'` 作用于整批，行内字段可覆盖它。
- 输出：`<out-dir>/<id>.json` 为完整响应体；要求 markdown 时另写 `<out-dir>/<id>.md`。
- 图片：Markdown 中的图片引用是计算节点上的文件路径，无法下载。文字、公式（LaTeX）、表格（默认 HTML；`element_formats` 的 `table` 设为 `markdown` 则为 Markdown 表）都已在结果中。图片的位置和 bbox 在 JSON 输出中类型为 `Picture` 的块里。2026-10-28 经 CPA 实测：`image` 设为 `base64` 时服务端接受，但 Markdown 里的图片仍是路径、没有内嵌，所以客户端不把它设为默认。
- 错误：HTTP 200 而 `code` 不为 0 的（如 1133，参数错误）记为失败，`code` 与 `message` 原样记录。`503` 且 `code` 为 `model_warming` 按 `Retry-After` 重试；502、504 与超时不重放。
- 输出 JSONL 每行记录：`id`、`ok`、`status`（HTTP 状态）、`code`、`message`、`pages`（`metadata.page_num`）、`processing_time_ms`、`markdown_file`、`json_file`、`attempts`、`latency_s`、`error`。
- 默认 `-j 4`，`--timeout` 默认 3600 秒（单次请求）。吞吐有限；`-j` 超过 4 时请求在服务端排队，速度不会更快。
