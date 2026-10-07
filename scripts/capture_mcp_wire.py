"""Capture real MCP traffic from synology-mcp for docs/mcp-explained.md.

    uv run python scripts/capture_mcp_wire.py

How it works (no real NAS involved):

    mcp.Client ──stdio──> [--proxy: records every JSON-RPC line] ──stdio──> [--serve-fake: the
    real build_server() talking to tests/fake_nas.py, recording every REST call it makes]

Writes docs/mcp-wire-sample.md. Session IDs and passwords are redacted.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # so `tests.fake_nas` is importable
SCRIPT = str(Path(__file__).resolve())
OUTPUT = ROOT / "docs" / "mcp-wire-sample.md"
REDACT = {"_sid", "passwd", "password"}


# -- role 1: the MCP server, backed by the fake NAS ------------------------------------
def serve_fake(writes: bool, nas_log: str) -> None:
    from synology_mcp.server import NasSession, ServerConfig, build_server
    from synology_poc import PathPolicy, SynologyClient
    from tests.fake_nas import PASSWORD, FakeNAS

    class Patcher:  # FakeNAS.install expects pytest's monkeypatch; setattr is all it needs
        setattr = staticmethod(setattr)

    nas = FakeNAS()
    nas.install(Patcher())
    nas.polls_until_done = 1
    nas.mkdirs("/poc-sandbox/reports")
    nas.fs["/poc-sandbox/reports/q3-summary.pdf"] = b"%PDF-1.7 " + b"x" * 48_000
    nas.fs["/poc-sandbox/notes.txt"] = b"Remember to back up the photos.\n"
    nas.fs["/poc-sandbox/team-photo.jpg"] = b"\xff\xd8" + b"y" * 210_000

    original = nas.handle

    def logged(params: dict[str, str], upload: Any = None):
        response = original(params, upload)
        shown = {k: ("<redacted>" if k in REDACT else v) for k, v in params.items()}
        try:
            body = response.json()
        except Exception:
            body = None
        if isinstance(body, dict) and "sid" in (body.get("data") or {}):
            body = {**body, "data": {**body["data"], "sid": "<redacted>"}}
        with open(nas_log, "a") as fh:
            fh.write(json.dumps({"request": shown, "response": body}) + "\n")
        return response

    nas.handle = logged

    def login() -> SynologyClient:
        client = SynologyClient("fakenas.local", cert_sha256="00" * 32)
        client.login("poc-user", PASSWORD)
        return client

    config = ServerConfig(settings=None, policy=PathPolicy(("/poc-sandbox",), allow_writes=writes))
    build_server(config, NasSession(login)).run("stdio")


# -- role 2: a transparent stdio proxy that records both directions ---------------------
def proxy(wire_log: str, command: list[str]) -> None:
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    lock = threading.Lock()

    def record(direction: str, line: bytes) -> None:
        with lock, open(wire_log, "a") as fh:
            fh.write(json.dumps({"dir": direction, "msg": json.loads(line)}) + "\n")

    def pump(src, dst, direction: str) -> None:
        for line in iter(src.readline, b""):
            if line.strip():
                record(direction, line)
            dst.write(line)
            dst.flush()
        dst.close()

    up = threading.Thread(target=pump, args=(sys.stdin.buffer, child.stdin, "client→server"), daemon=True)
    down = threading.Thread(target=pump, args=(child.stdout, sys.stdout.buffer, "server→client"), daemon=True)
    up.start()
    down.start()
    child.wait()
    down.join(timeout=2)


# -- role 3: drive scenarios with the real SDK client, then render markdown -------------
async def run_session(writes: bool, workdir: Path, scenario) -> tuple[list[dict], list[dict]]:
    from mcp import Client, StdioServerParameters

    tag = "writes" if writes else "readonly"
    wire_log, nas_log = workdir / f"wire-{tag}.jsonl", workdir / f"nas-{tag}.jsonl"
    server_cmd = [sys.executable, SCRIPT, "--serve-fake", str(nas_log)] + (["--writes"] if writes else [])
    params = StdioServerParameters(
        command=sys.executable,
        args=[SCRIPT, "--proxy", str(wire_log), "--", *server_cmd],
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    async with Client(params) as client:
        await scenario(client)
    read = lambda p: [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []  # noqa: E731
    return read(wire_log), read(nas_log)


async def readonly_scenarios(client) -> None:
    await client.list_tools()
    await client.call_tool("list_folder", {"path": "/poc-sandbox"})
    await client.get_prompt("find_large_files", {"folder": "/poc-sandbox", "top": "5"})


async def write_scenarios(client) -> None:
    await client.call_tool("create_folder", {"parent": "/video", "name": "holiday"})


def block(obj: Any) -> str:
    return "```json\n" + json.dumps(obj, indent=2, ensure_ascii=False) + "\n```"


def find(wire: list[dict], method: str, name: str | None = None) -> tuple[dict, dict]:
    """The request with this JSON-RPC method (and tool/prompt name) plus its response."""
    for i, entry in enumerate(wire):
        msg = entry["msg"]
        if msg.get("method") == method and (name is None or msg.get("params", {}).get("name") == name):
            reply = next(e["msg"] for e in wire[i + 1:] if e["msg"].get("id") == msg.get("id"))
            return msg, reply
    raise LookupError(f"{method} {name or ''} not captured")


def handshake(wire: list[dict]) -> list[dict]:
    """Everything exchanged before the first tools/list."""
    out = []
    for entry in wire:
        if entry["msg"].get("method") == "tools/list":
            break
        out.append(entry)
    return out


def trim_tools(reply: dict, keep: str) -> dict:
    tools = reply["result"]["tools"]
    shown = [t for t in tools if t["name"] == keep]
    others = [t["name"] for t in tools if t["name"] != keep]
    trimmed = json.loads(json.dumps(reply))
    trimmed["result"]["tools"] = shown + [f"… {len(others)} more tools: {', '.join(others)}"]
    return trimmed


def rest_line(entry: dict) -> str:
    req = dict(entry["request"])
    api, method, version = req.pop("api"), req.pop("method"), req.pop("version", "?")
    cgi = "query.cgi" if api == "SYNO.API.Info" else "entry.cgi"
    lines = [f"POST https://fakenas.local:5001/webapi/{cgi}",
             f"  api={api}  method={method}  version={version}"]
    lines += [f"  {k}={v}" for k, v in req.items()]
    return "\n".join(lines)


def render(ro_wire, ro_nas, w_wire) -> str:
    parts = [
        "# Captured MCP traffic (generated)",
        "",
        "Generated by `uv run python scripts/capture_mcp_wire.py`: the real `synology-mcp` server, "
        "driven by the official MCP Python SDK client over stdio, against the in-memory fake NAS "
        "(`tests/fake_nas.py`). Each JSON block is one line on the wire, pretty-printed. "
        "Session IDs and passwords are redacted. Explained in [mcp-explained.md](mcp-explained.md).",
        "",
        "## 1. Connecting (handshake)",
        "",
    ]
    for entry in handshake(ro_wire):
        parts += [f"**{entry['dir']}**", "", block(entry["msg"]), ""]

    req, reply = find(ro_wire, "tools/list")
    parts += ["## 2. Discovering tools: `tools/list`", "", "**client→server**", "", block(req), "",
              "**server→client** (only `list_folder` shown in full)", "", block(trim_tools(reply, "list_folder")), ""]

    req, reply = find(ro_wire, "tools/call", "list_folder")
    parts += ["## 3. Calling a tool: `tools/call list_folder`", "", "**client→server**", "", block(req), ""]
    during = [e for e in ro_nas if e["request"].get("method") != "logout"]
    at_exit = [e for e in ro_nas if e["request"].get("method") == "logout"]
    parts += ["### What the server did meanwhile: REST calls to the NAS", "",
              "First tool call, so the server discovers the NAS APIs and logs in (lazily); "
              "later calls reuse the same NAS session.", ""]
    for entry in during:
        parts += ["```http", rest_line(entry), "```", "", "NAS replied:", "", block(entry["response"]), ""]
    parts += ["**server→client**", "", block(reply), ""]

    req, reply = find(w_wire, "tools/call", "create_folder")
    parts += ["## 4. A refused write: `tools/call create_folder` outside the allowed roots", "",
              "Server started with `SYNO_MCP_ALLOW_WRITES=true`, roots = `/poc-sandbox`.", "",
              "**client→server**", "", block(req), "", "**server→client**", "", block(reply), ""]

    req, reply = find(ro_wire, "prompts/get", "find_large_files")
    parts += ["## 5. Using a prompt: `prompts/get find_large_files`", "", "**client→server**", "", block(req), "",
              "**server→client**", "", block(reply), ""]

    parts += ["## 6. Shutting down", "",
              "When the host closes the connection, the server process exits and logs out of the NAS:", ""]
    for entry in at_exit:
        parts += ["```http", rest_line(entry), "```", ""]
    return "\n".join(parts)


async def capture() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        ro_wire, ro_nas = await run_session(False, work, readonly_scenarios)
        w_wire, _ = await run_session(True, work, write_scenarios)
    OUTPUT.write_text(render(ro_wire, ro_nas, w_wire))
    print(f"wrote {OUTPUT.relative_to(ROOT)} ({len(ro_wire) + len(w_wire)} MCP messages, {len(ro_nas)} NAS calls)")


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["--serve-fake"]:
        serve_fake(writes="--writes" in args, nas_log=args[1])
    elif args[:1] == ["--proxy"]:
        proxy(args[1], args[args.index("--") + 1:])
    else:
        asyncio.run(capture())


if __name__ == "__main__":
    main()
