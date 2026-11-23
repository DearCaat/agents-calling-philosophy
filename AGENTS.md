# agents-calling-philosophy

Portable **model calling** plugin: a local entry table (`entries.md`) plus two JSONL clients: `llm-call.py` for direct LLM APIs and `tool-call.py` for gateway tools (jev, vela, mineru, oxr). 本机 inventory / 凭据挂在共享目录 `$AGENTS_DATA_ROOT`（见 README），不进本仓库。

调用模型时先读：`skills/calling-models/SKILL.md`。  
数据根解析：见 `skills/calling-models/SKILL.md` §1。
