# agents-calling-philosophy

Portable **model calling** plugin with `agents_ask` for cloud entry lookup, `llm-call.py` for direct LLM APIs, and `tool-call.py` for gateway tools (jev, vela, mineru, oxr). 本机路由和凭据放在共享目录 `$AGENTS_DATA_ROOT`（见 README）。

调用模型时先读：`skills/calling-models/SKILL.md`。  
数据根解析见 `skills/calling-models/SKILL.md` §3。
