# 查询入口

调用插件工具 `agents_ask` 时传入 `{"name":"gemini-4-neon"}`。回包给出调用所需字段、该名称的待核说明及 GitHub 原文行号。来源链接固定到查询时的提交，可取回整行和上下文。

```sh
python3 "$SKILL/scripts/agents-query.py" ask "gemini-4-neon"
python3 "$SKILL/scripts/agents-query.py" show harnesses
```

`ask` 与 MCP 查询使用相同的匹配规则，按入口表第一列标记的名称匹配，忽略大小写、空格和连字符的写法差异。待核说明按「待核」节的名称匹配。仅有待核说明、无匹配或别名有歧义时，CLI 退出码为 1。读取失败时退出码为 2。

`show` 读取整份参考，接受 `entries`、`routes` 或 `harnesses`。正文写到 stdout，来源提交写到 stderr。

查询通过 Git SSH 访问 `DearCaat/agents-local`，需要私库读取权限。可用 `GIT_SSH_COMMAND` 指定现有认证方式。已经配置数据根的机器可复用该 Git 仓库的 `core.sshCommand`，查询内容始终取自远端 `main`。
