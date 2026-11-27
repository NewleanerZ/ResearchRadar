"""External stdio MCP client registry, persistent connection pool, and native schema helpers."""

import asyncio
import atexit
from contextlib import AsyncExitStack
import json
import os
import re
import threading
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .models import dump, utc_now
from .store import Store, write


def add_mcp_server(
    store: Store,
    name: str,
    command: str,
    *,
    args: list[str] | None = None,
    env_keys: list[str] | None = None,
    auto_approve: bool = False,
) -> dict:
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
            """INSERT INTO mcp_servers(id, name, command, args_json, env_keys_json, enabled, created_at, tools_cache_json, auto_approve)
               VALUES (:id, :name, :cmd, :args, :env, 1, :now, '[]', :auto)""",
            id=sid, name=clean_name, cmd=clean_cmd, args=dump(clean_args), env=dump(clean_env),
            now=now, auto=int(bool(auto_approve)),
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
            "auto_approve": bool(r.get("auto_approve", 0)),
            "tools_cache": json.loads(r.get("tools_cache_json") or "[]"),
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
    if not enabled:
        MCP_MANAGER.invalidate(server_id)


def set_mcp_server_auto_approve(store: Store, server_id: str, auto_approve: bool) -> None:
    store.execute("UPDATE mcp_servers SET auto_approve=:aa WHERE id=:id", aa=int(bool(auto_approve)), id=server_id)


def remove_mcp_server(store: Store, server_id: str) -> None:
    MCP_MANAGER.invalidate(server_id)
    store.execute("DELETE FROM mcp_servers WHERE id=:id", id=server_id)


def _server_params(server: dict) -> StdioServerParameters:
    env = {k: os.environ[k] for k in server.get("env_keys", []) if k in os.environ}
    return StdioServerParameters(command=server["command"], args=server.get("args", []), env=env or None)


def _server_signature(server: dict) -> tuple:
    return (server["command"], tuple(server.get("args") or ()), tuple(server.get("env_keys") or ()))


class McpConnectionManager:
    """Thread-safe persistent stdio MCP session pool running on a dedicated asyncio daemon loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._sessions: dict[str, dict] = {}

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and not self._loop.is_closed():
                return self._loop
            ready = threading.Event()
            created: list[asyncio.AbstractEventLoop] = []

            def _runner():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                created.append(loop)
                ready.set()
                try:
                    loop.run_forever()
                finally:
                    loop.close()

            self._thread = threading.Thread(target=_runner, name="radar-mcp-pool", daemon=True)
            self._thread.start()
            ready.wait(timeout=5.0)
            self._loop = created[0]
            return self._loop

    def _run(self, coro, *, timeout: float):
        loop = self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return fut.result(timeout=timeout)
        except Exception:
            fut.cancel()
            raise

    async def _close_entry(self, server_id: str) -> None:
        entry = self._sessions.pop(server_id, None)
        if entry and entry.get("stack") is not None:
            try:
                await entry["stack"].aclose()
            except Exception:
                pass

    async def _get_entry(self, server: dict) -> dict:
        sid = server["id"]
        sig = _server_signature(server)
        existing = self._sessions.get(sid)
        if existing and existing.get("signature") == sig:
            return existing
        if existing:
            await self._close_entry(sid)
        stack = AsyncExitStack()
        try:
            read_stream, write_stream = await stack.enter_async_context(stdio_client(_server_params(server)))
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            listed = await session.list_tools()
            tools = [
                {"name": t.name, "description": t.description or "", "inputSchema": t.inputSchema}
                for t in listed.tools
            ]
            entry = {"signature": sig, "stack": stack, "session": session, "tools": tools}
            self._sessions[sid] = entry
            return entry
        except Exception:
            await stack.aclose()
            raise

    async def _discover_async(self, server: dict, *, refresh: bool = False) -> list[dict]:
        if refresh:
            await self._close_entry(server["id"])
        entry = await self._get_entry(server)
        return list(entry["tools"])

    async def _call_async(self, server: dict, tool_name: str, arguments: dict) -> dict:
        for attempt in range(2):
            try:
                entry = await self._get_entry(server)
                res = await entry["session"].call_tool(tool_name, arguments)
                content = []
                for part in res.content:
                    text = getattr(part, "text", None)
                    content.append(text if text is not None else str(part))
                return {"isError": bool(res.isError), "content": content}
            except Exception:
                await self._close_entry(server["id"])
                if attempt == 1:
                    raise
        raise RuntimeError("MCP invocation failed")

    def discover(self, server: dict, *, timeout: float = 15.0, refresh: bool = False) -> list[dict]:
        return self._run(self._discover_async(server, refresh=refresh), timeout=timeout)

    def call(self, server: dict, tool_name: str, arguments: dict, *, timeout: float = 25.0) -> dict:
        return self._run(self._call_async(server, tool_name, arguments), timeout=timeout)

    def invalidate(self, server_id: str) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        try:
            self._run(self._close_entry(server_id), timeout=5.0)
        except Exception:
            pass

    def shutdown(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        async def _close_all():
            for sid in list(self._sessions):
                await self._close_entry(sid)

        try:
            self._run(_close_all(), timeout=5.0)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._loop = None


MCP_MANAGER = McpConnectionManager()
atexit.register(MCP_MANAGER.shutdown)


def discover_mcp_tools(server: dict, *, timeout: float = 15.0, store: Store | None = None, refresh: bool = False) -> list[dict]:
    """Connect (or reuse persistent session) to an external stdio MCP server and list tool schemas."""
    tools = MCP_MANAGER.discover(server, timeout=timeout, refresh=refresh)
    if store is not None and server.get("id"):
        store.execute("UPDATE mcp_servers SET tools_cache_json=:cache WHERE id=:id", cache=dump(tools), id=server["id"])
    return tools


def call_external_mcp_tool(store: Store, server_name: str, tool_name: str, arguments: dict,
                           *, timeout: float = 25.0) -> dict:
    """Invoke a tool on an enabled registered external MCP server via persistent session pool."""
    server = get_mcp_server(store, server_name)
    if not server["enabled"]:
        raise ValueError(f"MCP 服务「{server_name}」当前处于禁用状态")
    return MCP_MANAGER.call(server, tool_name, arguments, timeout=timeout)


def _clean_json_schema(schema: dict | None) -> dict:
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    cleaned = dict(schema)
    cleaned.pop("title", None)
    cleaned.pop("$schema", None)
    if "type" not in cleaned:
        cleaned["type"] = "object"
    if cleaned.get("type") == "object" and "properties" not in cleaned:
        cleaned["properties"] = {}
    return cleaned


def native_mcp_registry(store: Store) -> tuple[list[dict], dict[str, tuple[dict, dict]]]:
    """Build stable OpenAI tool definitions for cached tools of enabled MCP servers."""
    definitions: list[dict] = []
    lookup: dict[str, tuple[dict, dict]] = {}
    for srv in list_mcp_servers(store, enabled_only=True):
        tools = srv.get("tools_cache") or []
        if not tools:
            try:
                tools = discover_mcp_tools(srv, timeout=6.0, store=store)
            except Exception:
                tools = []
        for t in tools:
            raw_name = t.get("name") or ""
            if not raw_name:
                continue
            safe_tool = re.sub(r"[^A-Za-z0-9_-]+", "_", raw_name)[:40]
            alias = f"mcp__{srv['name']}__{safe_tool}"[:64]
            if alias in lookup:
                continue
            desc = (t.get("description") or f"External MCP tool {srv['name']}.{raw_name}")[:400]
            definitions.append({
                "type": "function",
                "function": {
                    "name": alias,
                    "description": f"[MCP:{srv['name']}] {desc}",
                    "parameters": _clean_json_schema(t.get("inputSchema")),
                },
            })
            lookup[alias] = (srv, t)
    return definitions, lookup
