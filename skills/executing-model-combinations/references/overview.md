# agents：调用哲学与本机 inventory

本目录分两层。派工程序在 [SKILL.md](../SKILL.md)。

## Portable（跨机器）

- [models.md](models.md)：厂商卡、角色排序、effort 默认；context **政策**见同文 + [runtime-defaults.tsv](runtime-defaults.tsv)（`dispatch.sh` 唯读此表实现）。
- [harnesses.md](harnesses.md)：原生命令形状、session、压缩语义、spawn_agent 规格——**无**本机 URL/HOME。
- 资料类型标签：`official` / `local-test` / `local-experience`（证据种类，不是目录名）。

## Machine data（本机，更新不覆盖）

推荐**一个共享目录**挂本机 inventory + 凭据，Claude Code / Codex / Grok 都解析到这里：

```text
$AGENTS_DATA_ROOT/
  local/                 # bindings、adapters、apis、evidence…
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
- `verified` 正常执行；推荐入口的 `configured` 先 `verify.sh`；其它缺口报告、**不改口**。
- effort / context / profile / sandbox 是运行参数，不改 binding 身份。
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
