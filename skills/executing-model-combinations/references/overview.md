# agents：调用哲学与本机 inventory

本目录分两层。派工程序在 [SKILL.md](../SKILL.md)。

## Portable（跨机器）

- [models.md](models.md)：厂商卡、角色排序、effort 默认；context **政策**见同文 + [runtime-defaults.tsv](runtime-defaults.tsv)（`dispatch.sh` 唯读此表实现）。
- [harnesses.md](harnesses.md)：原生命令形状、session、压缩语义、spawn_agent 规格——**无**本机 URL/HOME。
- 资料类型标签：`official` / `local-test` / `local-experience`（证据种类，不是目录名）。

## Local overlay（本机）

- 根目录：[local/](local/)（默认）或环境变量 `AGENTS_LOCAL_ROOT`。
- 模板：[local.example/](local.example/)——只有表头，不可当 registry。
- 内含：`bindings.tsv`、`adapters.tsv`、`api-routes.tsv`、evidence/failures、`apis.md`、`harnesses.md`。说明见 [local/README.md](local/README.md)。
- 凭据与 Codex catalog 仍在插件根 `private/`（可用 `AGENTS_CODEX_CATALOG_DIR`）。

机器 B：替换 `local/` + `private/`，portable 文件不动。

## 不变量

- 只有 registry 中的 exact `API + harness + model` 是现成入口；三集合不做笛卡尔积。
- `verified` 正常执行；推荐入口的 `configured` 先 `verify.sh`；其它缺口报告、**不改口**。
- effort / context / profile / sandbox 是运行参数，不改 binding 身份。
- 模型官方参考成本 ≠ API 实际价格（后者只在 local apis）。
- 发现（OpenRouter、`/models`）≠ binding。

## 脚本

```bash
SKILL_DIR=/path/to/agents/skills/executing-model-combinations
"$SKILL_DIR/scripts/dispatch.sh" --list-bindings
"$SKILL_DIR/scripts/api-models.sh" --api <id-from-local-routes>
```

`dispatch.sh` / `verify.sh` / `api-models.sh` 均读 `LOCAL_ROOT`；缺 inventory 时失败并提示从 `local.example` 复制，不回退到某一台机器的 URL。
