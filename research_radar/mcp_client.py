"""External stdio MCP client registry and bounded tool invocation."""

import asyncio
import json
import os
import re
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .models import dump, utc_now
from .store import Store, write


def add_mcp_server(store: Store, name: str, command: str, *, args: list[str] | None = None,
                   env_keys: list[str] | None = None) -> dict:
    clean_name = name.strip()
    clean_cmd = command.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", clean_name):
        raise ValueError("MCP 服务名称仅支持字母、数字、下划线和短横线（最多 40 字符）")
    if not clean_cmd or any(ch in clean_cmd for ch in ("&", "|", ";", "`", "\n", "\r")):
        raise ValueError("MCP 启动命令无效")
    clean_args = [str(a) for a in (args or [])]
    clean_env = [k.strip() for k in (env_keys or []) if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k.strip())]
    sid = f"mcp_{uuid4().hex[:10]}"
    now = utc_now().isoformat()
    with store.transaction() as con:
        write(
            con,
            """INSERT INTO mcp_servers(id, name, command, args_json, env_keys_json, enabled, created_at)
               VALUES (:id, :name, :cmd, :args, :env, 1, :now)""",
            id=sid, name=clean_name, cmd=clean_cmd, args=dump(clean_args), env=dump(clean_env), now=now,
        )
    return get_mcp_server(store, clean_name)


def list_mcp_servers(store: Store, *, enabled_only: bool = False) -> list[dict]:
    rows_found = store.all(
        "SELECT * FROM mcp_servers WHERE (NOT :only_on OR enabled=1) ORDER BY created_at DESC",
        only_on=enabled_only,
    )
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "command": r["command"],
            "args": json.loads(r["args_json"]),
            "env_keys": json.loads(r["env_keys_json"]),
            "enabled": bool(r["enabled"]),
            "created_at": r["created_at"],
        }
        for r in rows_found
    ]


def get_mcp_server(store: Store, name: str) -> dict:
    item = next((s for s in list_mcp_servers(store) if s["name"] == name), None)
    if not item:
        raise ValueError(f"找不到已注册的 MCP 服务：{name}")
    return item


def set_mcp_server_enabled(store: Store, server_id: str, enabled: bool) -> None:
    store.execute("UPDATE mcp_servers SET enabled=:en WHERE id=:id", en=int(enabled), id=server_id)


def remove_mcp_server(store: Store, server_id: str) -> None:
    store.execute("DELETE FROM mcp_servers WHERE id=:id", id=server_id)


def _server_params(server: dict) -> StdioServerParameters:
    env = {k: os.environ[k] for k in server.get("env_keys", []) if k in os.environ}
    return StdioServerParameters(command=server["command"], args=server.get("args", []), env=env or None)


async def _discover_async(server: dict) -> list[dict]:
    async with stdio_client(_server_params(server)) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
            return [
                {"name": t.name, "description": t.description or "", "inputSchema": t.inputSchema}
                for t in listed.tools
            ]


async def _call_async(server: dict, tool_name: str, arguments: dict) -> dict:
    async with stdio_client(_server_params(server)) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            res = await session.call_tool(tool_name, arguments)
            content = []
            for part in res.content:
                text = getattr(part, "text", None)
                content.append(text if text is not None else str(part))
            return {"isError": bool(res.isError), "content": content}


def discover_mcp_tools(server: dict, *, timeout: float = 15.0) -> list[dict]:
    """Connect to an external stdio MCP server and list its tool schemas."""
    return asyncio.run(asyncio.wait_for(_discover_async(server), timeout=timeout))


def call_external_mcp_tool(store: Store, server_name: str, tool_name: str, arguments: dict,
                           *, timeout: float = 25.0) -> dict:
    """Invoke a tool on an enabled registered external MCP server."""
    server = get_mcp_server(store, server_name)
    if not server["enabled"]:
        raise ValueError(f"MCP 服务「{server_name}」当前处于禁用状态")
    return asyncio.run(asyncio.wait_for(_call_async(server, tool_name, arguments), timeout=timeout))
