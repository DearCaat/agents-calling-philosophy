# agents：参考手册与本机 inventory

本目录分两层。入口与解析步骤在 [SKILL.md](../SKILL.md)。

## Portable（跨机器）

- [executors.md](executors.md)：四类执行者（脚本、判定模型、直接调用 API、harness）的特点与取舍。
- [models.md](models.md)：模型卡（能力、局限、官方价格，标来源）、模型与 harness 的搭配经验及原因。
- [harnesses.md](harnesses.md)：各 harness 的能力、厚薄、原生命令形状、session、压缩语义、spawn_agent 规格——**无**本机 URL/HOME/版本。
- 资料类型标签：`official` / `local-test` / `local-experience`（证据种类，不是目录名）。

## Machine data（本机，更新不覆盖）

推荐**一个共享目录**挂本机 inventory + 凭据，Claude Code / Codex / Grok 都解析到这里：

```text
$AGENTS_DATA_ROOT/
  local/                 # bindings、adapters、apis、models、harnesses、runtime-defaults、dispatch.env、evidence…
  private/
    credentials.env
    runtime/…            # 可选：codex-home 等
```

解析顺序（`scripts/lib/data-root.sh`）：

1. `AGENTS_DATA_ROOT`（推荐显式设置，三家共用）
2. 否则 `CLAUDE_PLUGIN_DATA` / `PLUGIN_DATA` / `GROK_PLUGIN_DATA`（harness 注入的插件持久目录）
3. 否则回退插件树内 `references/local` + `private/`（目录版 marketplace 兼容）

细覆盖：`AGENTS_LOCAL_ROOT`、`AGENTS_CREDENTIALS_FILE`、`AGENTS_CODEX_CATALOG_DIR`。

空模板：[local.example/](local.example/)。若仍使用插件内 overlay，见 [local/README.md](local/README.md)（本机工作树可有；公开哲学仓不含填好的 `local/`）。

## 不变量

- 只有 registry 中的 exact `API + harness + model` 是现成入口；三集合不做笛卡尔积。
- `verified` 正常执行；`configured` 先 `verify.sh`；其它缺口报告、**不改口**。
- effort / context / profile / sandbox 是运行参数，不改 binding 身份；本机默认在 `local/runtime-defaults.tsv`。
- 模型官方参考成本 ≠ API 实际价格（后者只在 local apis）。
- 发现（OpenRouter、`/models`）≠ binding。

## 脚本

```bash
export AGENTS_DATA_ROOT=$HOME/.agents/calling-data
SKILL_DIR=/path/to/plugin/skills/executing-model-combinations
"$SKILL_DIR/scripts/dispatch.sh" --list-bindings
"$SKILL_DIR/scripts/api-models.sh" --api <id-from-local-routes>
```

缺 inventory 时失败并提示挂载 / 从 `local.example` 复制，不回退到某一台机器的 URL。
