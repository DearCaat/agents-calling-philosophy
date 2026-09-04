# agents bundle

跨 harness 的 **调用哲学 + 本机 inventory** 插件。处理组合选择或实际模型调用时，先读：

- `skills/executing-model-combinations/SKILL.md`

哲学（角色、推荐模型名、effort/context 政策、native sub-agent）在 skill 的 portable `references/`。  
本机通道与 registry 在 `skills/executing-model-combinations/references/local/`（可整夹替换；见该目录 README）。

Codex、Claude Code 与 Grok 经各自插件入口加载同一 skill。凭据值只在 `private/`，由脚本或原生命令注入；不得写入 prompt、回显、文档或日志。

本机已知偏差（inventory，非哲学）：`~/.codex/config.toml` / `~/.grok/config.toml` 可能含明文 token（不由本插件管理）；cc-switch 可能改写 `~/.claude_old_env`、`~/.codex`、`~/.grok`。
