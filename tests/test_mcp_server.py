"""The MCP server, driven through the SDK's in-process client against the fake NAS."""

import json

import pytest
from mcp import Client

from synology_poc import PathPolicy, Settings, SynologyClient
from synology_mcp.server import NasSession, ServerConfig, build_server

from .fake_nas import PASSWORD

READ_TOOLS = {
    "nas_info", "list_shares", "list_folder", "get_file_info", "folder_size", "file_md5",
    "search_files", "list_archive", "get_thumbnail", "download_file", "list_background_tasks",
    "list_share_links",
}
WRITE_TOOLS = {"create_folder", "upload_file", "rename", "copy_move", "compress", "extract", "delete"}
SHARING_TOOLS = {"create_share_link", "edit_share_link", "delete_share_link"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_server(client, tmp_path, writes=False, sharing=False):
    config = ServerConfig(
        settings=None,
        policy=PathPolicy(("/poc-sandbox",), allow_writes=writes, allow_sharing=sharing),
        local_dir=tmp_path,
    )
    def login():
        # Each MCP connection's shutdown logs the session out, so log in fresh like production.
        fresh = SynologyClient("fakenas.local", cert_sha256="00" * 32)
        fresh.login("poc-user", PASSWORD)
        return fresh

    return build_server(config, NasSession(login))


async def call(server, tool_name, /, **args):
    async with Client(server) as c:
        return await c.call_tool(tool_name, args)


def payload(result):
    assert not result.is_error, result.content
    if result.structured_content is not None:
        data = result.structured_content
        return data.get("result", data) if isinstance(data, dict) and set(data) == {"result"} else data
    return json.loads(result.content[0].text)


def error_text(result):
    assert result.is_error
    return result.content[0].text


@pytest.mark.anyio
@pytest.mark.parametrize("writes,sharing,expected", [
    (False, False, READ_TOOLS),
    (True, False, READ_TOOLS | WRITE_TOOLS),
    (True, True, READ_TOOLS | WRITE_TOOLS | SHARING_TOOLS),
])
async def test_tools_registered_per_policy(client, tmp_path, writes, sharing, expected):
    server = make_server(client, tmp_path, writes, sharing)
    async with Client(server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == expected
    assert all(tools[n].annotations.read_only_hint for n in READ_TOOLS)
    if writes:
        assert tools["delete"].annotations.destructive_hint is True


@pytest.mark.anyio
async def test_read_tools(nas, client, fs, tmp_path):
    fs.create_folder("/poc-sandbox", "docs")
    fs.upload("/poc-sandbox/docs", b"hello world", filename="note.txt")
    server = make_server(client, tmp_path)

    info = payload(await call(server, "nas_info"))
    assert info["server_policy"]["writes_enabled"] is False
    listing = payload(await call(server, "list_folder", path="/poc-sandbox"))
    assert [i["name"] for i in listing["items"]] == ["docs"] and listing["hidden_system"] == 1
    text = payload(await call(server, "download_file", path="/poc-sandbox/docs/note.txt"))
    assert text["text"] == "hello world"
    found = payload(await call(server, "search_files", folder="/poc-sandbox", pattern="*.txt"))
    assert [i["path"] for i in found["items"]] == ["/poc-sandbox/docs/note.txt"]
    md5 = payload(await call(server, "file_md5", path="/poc-sandbox/docs/note.txt"))
    assert md5["md5"] == "5eb63bbbe01eeed093cb22bb8f5acdc3"


@pytest.mark.anyio
async def test_binary_download_saved_locally(fs, client, tmp_path):
    fs.upload("/poc-sandbox", bytes(range(256)), filename="blob.bin")
    server = make_server(client, tmp_path)
    result = payload(await call(server, "download_file", path="/poc-sandbox/blob.bin"))
    assert (tmp_path / "blob.bin").read_bytes() == bytes(range(256))
    assert result["saved_to"] == str(tmp_path / "blob.bin")


@pytest.mark.anyio
async def test_thumbnail_is_image_content(fs, client, tmp_path):
    fs.upload("/poc-sandbox", b"\x89PNG\r\n\x1a\nfake", filename="i.png")
    result = await call(make_server(client, tmp_path), "get_thumbnail", path="/poc-sandbox/i.png")
    assert not result.is_error
    assert result.content[0].type == "image" and result.content[0].mime_type == "image/png"


@pytest.mark.anyio
async def test_nas_errors_become_tool_errors(client, tmp_path):
    result = await call(make_server(client, tmp_path), "list_folder", path="/homes")
    assert "407" in error_text(result)


@pytest.mark.anyio
async def test_write_tools_inside_roots(nas, fs, client, tmp_path):
    server = make_server(client, tmp_path, writes=True)
    payload(await call(server, "create_folder", parent="/poc-sandbox", name="w"))
    up = payload(await call(server, "upload_file", dest_folder="/poc-sandbox/w", text="hi", filename="a.txt"))
    assert up == {"path": "/poc-sandbox/w/a.txt", "skipped": False}
    again = payload(await call(server, "upload_file", dest_folder="/poc-sandbox/w", text="x", filename="a.txt"))
    assert again["skipped"] is True
    payload(await call(server, "compress", paths=["/poc-sandbox/w/a.txt"], dest_file_path="/poc-sandbox/w/a.zip"))
    names = [i["path"] for i in payload(await call(server, "list_archive", path="/poc-sandbox/w/a.zip"))]
    assert names == ["a.txt"]
    payload(await call(server, "delete", paths=["/poc-sandbox/w"]))
    assert "/poc-sandbox/w" not in nas.fs


@pytest.mark.anyio
@pytest.mark.parametrize("tool,args", [
    ("create_folder", {"parent": "/video", "name": "x"}),
    ("delete", {"paths": ["/poc-sandbox"]}),
    ("delete", {"paths": ["/poc-sandbox/../video/x"]}),
    ("rename", {"path": "/poc-sandbox/a", "new_name": "../b"}),
    ("upload_file", {"dest_folder": "/poc-sandbox", "local_file": "../../etc/passwd"}),
])
async def test_write_tools_refuse_escapes(nas, client, tmp_path, tool, args):
    before = dict(nas.fs)
    result = await call(make_server(client, tmp_path, writes=True), tool, **args)
    assert error_text(result)
    assert nas.fs == before


@pytest.mark.anyio
async def test_share_link_tools(nas, fs, client, tmp_path):
    fs.upload("/poc-sandbox", b"img", filename="s.png")
    server = make_server(client, tmp_path, sharing=True)
    link = payload(await call(server, "create_share_link", path="/poc-sandbox/s.png", password="pw"))
    assert link["public_internet"] is True and link["date_expired"].endswith("23:59:59")
    outside = await call(server, "create_share_link", path="/home/private.png")
    assert "outside the allowed roots" in error_text(outside)
    payload(await call(server, "delete_share_link", link_ids=[link["id"]]))
    assert not nas.links


@pytest.mark.anyio
async def test_rename_copy_move_extract_tools(nas, fs, client, tmp_path):
    fs.create_folder("/poc-sandbox", "w")
    fs.upload("/poc-sandbox/w", b"data", filename="a.txt")
    server = make_server(client, tmp_path, writes=True)
    renamed = payload(await call(server, "rename", path="/poc-sandbox/w/a.txt", new_name="b.txt"))
    assert renamed["path"] == "/poc-sandbox/w/b.txt"
    payload(await call(server, "create_folder", parent="/poc-sandbox/w", name="c"))
    payload(await call(server, "copy_move", paths=["/poc-sandbox/w/b.txt"], dest_folder="/poc-sandbox/w/c"))
    assert nas.fs["/poc-sandbox/w/c/b.txt"] == b"data"
    payload(await call(server, "compress", paths=["/poc-sandbox/w/b.txt"], dest_file_path="/poc-sandbox/w/z.zip"))
    payload(await call(server, "create_folder", parent="/poc-sandbox/w", name="x"))
    payload(await call(server, "extract", archive="/poc-sandbox/w/z.zip", dest_folder="/poc-sandbox/w/x"))
    assert nas.fs["/poc-sandbox/w/x/b.txt"] == b"data"
    moved = await call(server, "copy_move", paths=["/home/secret.txt"], dest_folder="/poc-sandbox/w", move=True)
    assert "outside the allowed roots" in error_text(moved)


@pytest.mark.anyio
async def test_edit_share_link_tool(nas, fs, client, tmp_path):
    fs.upload("/poc-sandbox", b"img", filename="s.png")
    server = make_server(client, tmp_path, sharing=True)
    link = payload(await call(server, "create_share_link", path="/poc-sandbox/s.png"))
    edited = payload(await call(server, "edit_share_link", link_id=link["id"], expires_in_days=7))
    assert edited["date_expired"].endswith("23:59:59") and edited["date_expired"] != link["date_expired"]
    bad = await call(server, "edit_share_link", link_id=link["id"], expires_in_days=90)
    assert "between 1 and 30" in error_text(bad)


@pytest.mark.anyio
async def test_prompts(client, tmp_path):
    async with Client(make_server(client, tmp_path)) as c:
        prompts = {p.name: p for p in (await c.list_prompts()).prompts}
        assert set(prompts) == {"audit_access", "find_large_files", "clean_sandbox"}
        audit = await c.get_prompt("audit_access")
        assert "list_shares" in audit.messages[0].content.text
        large = await c.get_prompt("find_large_files", {"folder": "/poc-sandbox/x", "top": "5"})
        assert "5 largest files under /poc-sandbox/x" in large.messages[0].content.text
        clean = (await c.get_prompt("clean_sandbox")).messages[0].content.text
        assert "SYNO_MCP_ALLOW_WRITES=true" in clean and "--cleanup" in clean
    async with Client(make_server(client, tmp_path, writes=True, sharing=True)) as c:
        clean = (await c.get_prompt("clean_sandbox")).messages[0].content.text
        assert "delete_share_link" in clean and "with the delete tool" in clean


@pytest.mark.anyio
@pytest.mark.parametrize("tls,expected", [
    ({}, "TLS pinning required"),
    ({"ca_cert": "/nonexistent/ca.pem"}, "doesn't exist"),
])
async def test_settings_errors_are_readable_tool_errors(tmp_path, tls, expected):
    settings = Settings(host="fakenas.local", port=5001, user="poc-user", password=PASSWORD, sandbox="/poc-sandbox",
                        ca_cert=tls.get("ca_cert"), cert_sha256=None, denied_path="/homes")
    config = ServerConfig(settings=settings, policy=PathPolicy(("/poc-sandbox",)), local_dir=tmp_path)
    result = await call(build_server(config), "nas_info")
    assert "NAS client settings" in error_text(result) and expected in error_text(result)
