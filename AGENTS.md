# agents-calling-philosophy

跨 harness 的 **调用哲学** 插件（本仓库不含本机 credentials / 填好的 inventory）。处理组合选择或实际模型调用时，先读：

- `skills/executing-model-combinations/SKILL.md`

哲学（角色、推荐模型名、effort/context 政策、native sub-agent、缺口不改口）在 skill 的 portable `references/` 与 `runtime-defaults.tsv`。

本机通道与 registry 由各机自备：从 `references/local.example/` 复制为 `references/local/`（或设 `AGENTS_LOCAL_ROOT`），凭据放在插件 `private/`。凭据值不得写入 prompt、回显、文档或日志。
