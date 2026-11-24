#!/usr/bin/env python3
"""Expose the agents-local entry query as a read-only MCP tool."""

import json
from pathlib import Path
import subprocess
import sys


TOOL = {
    "name": "agents_ask",
    "description": "按登记名或别名查 agents-local 最新入口，返回调用信息和原表位置。",
    "inputSchema": {
        "type": "object",
        "properties": {"name": {"type": "string", "minLength": 1}},
        "required": ["name"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
}

PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}


def call_tool(params):
    arguments = params.get("arguments", {})
    if (
        params.get("name") != TOOL["name"]
        or not isinstance(arguments, dict)
        or set(arguments) != {"name"}
        or not isinstance(arguments["name"], str)
    ):
        return {"error": {"code": -32602, "message": "agents_ask 需要一个字符串参数 name。"}}
    name = arguments["name"]
    if not name.strip() or "\0" in name:
        return {"result": {
            "content": [{"type": "text", "text": "请填写有效的登记名或别名。"}],
            "isError": True,
        }}

    cli = Path(__file__).with_name("agents-query.py")
    try:
        query = subprocess.run(
            [sys.executable, str(cli), "ask", "--", name],
            capture_output=True, text=True, encoding="utf-8", timeout=40,
        )
        output = (query.stdout if query.returncode == 0 else query.stderr).strip()
        result = {"content": [{"type": "text", "text": output or "查询入口表失败。"}]}
        if query.returncode:
            result["isError"] = True
    except (OSError, subprocess.TimeoutExpired):
        result = {"content": [{"type": "text", "text": "调用入口表查询程序失败。"}],
                  "isError": True}
    return {"result": result}


def dispatch(method, params):
    if method == "initialize":
        version = params.get("protocolVersion")
        if not isinstance(version, str) or version not in PROTOCOL_VERSIONS:
            version = "2025-11-25"
        return {"result": {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "agents", "version": "0.2.2"},
        }}
    if method == "tools/list":
        return {"result": {"tools": [TOOL]}}
    if method == "tools/call":
        return call_tool(params)
    if method == "ping":
        return {"result": {}}
    return {"error": {"code": -32601, "message": "Method not found"}}


def serve():
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError:
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32700, "message": "Parse error"}}), flush=True)
            continue
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32600, "message": "Invalid request"}}), flush=True)
            continue
        if "id" not in request:
            continue
        params = request.get("params", {})
        if not isinstance(params, dict):
            response = {"error": {"code": -32602, "message": "Invalid params"}}
        else:
            response = dispatch(request.get("method"), params)
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], **response}), flush=True)


if __name__ == "__main__":
    serve()
