"""The MCP server over streamable HTTP (container mode): a real uvicorn server against the fake NAS."""

from contextlib import asynccontextmanager

import anyio
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from synology_mcp.server import HttpConfig, http_app, http_server, transport_from_env

from .test_mcp_server import READ_TOOLS, make_server, payload

TOKEN = "t" * 40


@pytest.fixture
def anyio_backend():
    return "asyncio"


@asynccontextmanager
async def serving(server):
    config = HttpConfig(token=TOKEN, port=0)
    srv = http_server(http_app(server, config), config)
    async with anyio.create_task_group() as tg:
        tg.start_soon(srv.serve)
        while not srv.started:
            await anyio.sleep(0.01)
        port = srv.servers[0].sockets[0].getsockname()[1]
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            srv.should_exit = True


@pytest.mark.anyio
async def test_requests_without_the_token_are_refused(client, tmp_path):
    async with serving(make_server(client, tmp_path)) as base, httpx2.AsyncClient() as http:
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        assert (await http.post(f"{base}/mcp", json=init)).status_code == 401
        wrong = await http.post(f"{base}/mcp", json=init, headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401 and wrong.headers["www-authenticate"] == "Bearer"
        health = await http.get(f"{base}/healthz")
        assert health.status_code == 200 and health.text == "ok"


@pytest.mark.anyio
async def test_tools_work_with_the_token(fs, client, tmp_path):
    fs.create_folder("/poc-sandbox", "docs")
    async with serving(make_server(client, tmp_path)) as base:
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"})
        async with http, Client(streamable_http_client(f"{base}/mcp", http_client=http)) as c:
            assert {t.name for t in (await c.list_tools()).tools} == READ_TOOLS
            listing = payload(await c.call_tool("list_folder", {"path": "/poc-sandbox"}))
            assert [i["name"] for i in listing["items"]] == ["docs"]


def test_http_config_needs_a_long_token(monkeypatch):
    monkeypatch.setenv("SYNO_MCP_TOKEN", "short")
    with pytest.raises(SystemExit, match="SYNO_MCP_TOKEN"):
        HttpConfig.from_env()
    monkeypatch.setenv("SYNO_MCP_TOKEN", TOKEN)
    monkeypatch.setenv("SYNO_MCP_HOST", "0.0.0.0")
    monkeypatch.delenv("SYNO_MCP_PORT", raising=False)
    config = HttpConfig.from_env()
    assert (config.host, config.port) == ("0.0.0.0", 8000)
    assert TOKEN not in repr(config)


def test_transport_defaults_to_stdio(monkeypatch):
    monkeypatch.delenv("SYNO_MCP_TRANSPORT", raising=False)
    assert transport_from_env() == "stdio"
    monkeypatch.setenv("SYNO_MCP_TRANSPORT", " HTTP ")
    assert transport_from_env() == "http"
    monkeypatch.setenv("SYNO_MCP_TRANSPORT", "sse")
    with pytest.raises(SystemExit):
        transport_from_env()
