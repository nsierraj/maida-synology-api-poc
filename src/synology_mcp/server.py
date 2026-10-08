"""MCP server exposing Synology File Station use cases (see docs/mcp-server.md).

Safety model:
- Read tools are always available, limited only by the DSM account's own permissions.
- Write tools exist only when SYNO_MCP_ALLOW_WRITES=true, and only inside SYNO_MCP_ROOTS.
- Share-link tools exist only when SYNO_MCP_ALLOW_SHARING=true (links can be public).
- Local files are read/written only inside SYNO_MCP_LOCAL_DIR.

Credentials come from the same .env as the PoC and never appear in tool output.
Nothing may print to stdout: with the stdio transport, stdout is the protocol channel.

Transports: stdio (default) or, with SYNO_MCP_TRANSPORT=http, streamable HTTP for running in a
container. Over HTTP every request needs `Authorization: Bearer $SYNO_MCP_TOKEN`.
"""

from __future__ import annotations

import copy
import functools
import hmac
import os
import posixpath
import sys
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
import uvicorn
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from synology_poc import FileStation, PathPolicy, PolicyError, Settings, SynologyClient, SynologyError

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)
EXPOSING = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

INLINE_TEXT_LIMIT = 256 * 1024  # download_file returns text inline up to this size


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class ServerConfig:
    settings: Settings | None
    policy: PathPolicy
    local_dir: Path | None = None

    @classmethod
    def from_env(cls) -> ServerConfig:
        settings = Settings.from_env()  # also loads .env
        roots_env = os.getenv("SYNO_MCP_ROOTS") or settings.sandbox
        roots = tuple(r.strip() for r in roots_env.split(",") if r.strip())
        local = os.getenv("SYNO_MCP_LOCAL_DIR")
        return cls(
            settings=settings,
            policy=PathPolicy(
                roots,
                allow_writes=_flag("SYNO_MCP_ALLOW_WRITES"),
                allow_sharing=_flag("SYNO_MCP_ALLOW_SHARING"),
            ),
            local_dir=Path(local).expanduser().resolve() if local else None,
        )


@dataclass
class NasSession:
    """One shared, lazily logged-in client. Calls are serialized: tools run in worker
    threads and a requests.Session is not safe for concurrent use."""

    client_factory: Callable[[], SynologyClient]
    lock: threading.Lock = field(default_factory=threading.Lock)
    _fs: FileStation | None = None

    def fs(self) -> FileStation:
        if self._fs is None:
            self._fs = FileStation(self.client_factory())
        return self._fs

    def close(self) -> None:
        if self._fs is not None:
            try:
                self._fs.client.logout()
            except (SynologyError, requests.RequestException):
                pass
            self._fs.client.session.close()
            self._fs = None


def login_factory(settings: Settings) -> Callable[[], SynologyClient]:
    def make() -> SynologyClient:
        client = settings.client()
        try:
            client.discover()
            client.login(settings.user, settings.password)
        except requests.exceptions.SSLError as e:
            raise ToolError(f"TLS check against the NAS failed ({e}). Check SYNO_CERT_SHA256 / SYNO_CERT_HOSTNAME / SYNO_CA_CERT.")
        except requests.exceptions.ConnectionError as e:
            raise ToolError(f"Cannot reach the NAS at {settings.host}:{settings.port}: {e}")
        except SynologyError as e:
            raise ToolError(f"NAS login failed: {e}")
        return client

    return make


# -- result shaping (keep tool output compact and JSON-friendly) -------------------
def _iso(epoch: int | None) -> str | None:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat() if epoch else None


def _file(entry: dict[str, Any]) -> dict[str, Any]:
    add = entry.get("additional", {})
    out = {"name": entry.get("name"), "path": entry.get("path"), "is_dir": entry.get("isdir")}
    if "size" in add and not entry.get("isdir"):
        out["size"] = add["size"]
    if add.get("time", {}).get("mtime"):
        out["modified"] = _iso(add["time"]["mtime"])
    if add.get("type"):
        out["type"] = add["type"]
    if "owner" in add:
        out["owner"] = add["owner"].get("user")
    if "real_path" in add:
        out["real_path"] = add["real_path"]
    return out


def _link(link: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "url", "path", "status", "has_password", "date_expired", "date_available", "link_owner")
    return {k: link.get(k) for k in keys if k in link}


def build_server(config: ServerConfig, session: NasSession | None = None) -> MCPServer:
    if session is None:
        if config.settings is None:
            raise ValueError("settings are required when no session is given")
        session = NasSession(login_factory(config.settings))
    policy, local_dir = config.policy, config.local_dir

    @asynccontextmanager
    async def lifespan(app: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            session.close()

    roots = ", ".join(policy.roots) or "(none)"
    server = MCPServer(
        name="synology-filestation",
        instructions=(
            "Tools for a Synology NAS via the File Station API. Paths are absolute and start "
            "with a shared folder, e.g. /poc-sandbox/reports/q3.pdf. "
            f"Writes are {'enabled' if policy.allow_writes else 'disabled'}"
            f"{f' and limited to: {roots}' if policy.allow_writes else ''}. "
            f"Share links are {'enabled' if policy.allow_sharing else 'disabled'}. "
            "Deleted items usually go to the share's #recycle bin."
        ),
        lifespan=lifespan,
    )

    def tool(annotations: ToolAnnotations) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a tool: serialize NAS access and turn known failures into ToolErrors."""

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                with session.lock:
                    try:
                        return fn(*args, **kwargs)
                    except SynologyError as e:
                        raise ToolError(str(e)) from e
                    except PolicyError as e:
                        raise ToolError(f"Refused by policy: {e}") from e
                    except requests.RequestException as e:
                        raise ToolError(f"NAS request failed: {e}") from e

            server.add_tool(wrapper, annotations=annotations)
            return fn

        return register

    def local_path(name: str) -> Path:
        if local_dir is None:
            raise ToolError("No local folder configured (set SYNO_MCP_LOCAL_DIR).")
        target = (local_dir / name).resolve()
        if not target.is_relative_to(local_dir):
            raise ToolError(f"{name!r} is outside SYNO_MCP_LOCAL_DIR")
        return target

    # == read-only tools =========================================================
    @tool(READ_ONLY)
    def nas_info() -> dict[str, Any]:
        """NAS hostname and File Station capabilities, plus what this server allows."""
        info = session.fs().info()
        return {
            "hostname": info.get("hostname"),
            "is_admin": info.get("is_manager"),
            "sharing_supported": info.get("support_sharing"),
            "server_policy": {
                "writes_enabled": policy.allow_writes,
                "sharing_enabled": policy.allow_sharing,
                "write_roots": list(policy.roots),
                "local_dir": str(local_dir) if local_dir else None,
            },
        }

    @tool(READ_ONLY)
    def list_shares() -> list[dict[str, Any]]:
        """Shared folders the NAS account can see, with free/total space in bytes."""
        out = []
        for share in session.fs().list_shares(additional=("real_path", "volume_status")):
            vol = share.get("additional", {}).get("volume_status", {})
            out.append({"path": share["path"], "free_bytes": vol.get("freespace"),
                        "total_bytes": vol.get("totalspace"), "read_only": vol.get("readonly")})
        return out

    @tool(READ_ONLY)
    def list_folder(
        path: str,
        offset: int = 0,
        limit: int = 100,
        sort_by: str = "name",
        sort_direction: str = "asc",
        pattern: str | None = None,
        filetype: str = "all",
        include_system: bool = False,
    ) -> dict[str, Any]:
        """List a folder. sort_by: name|size|mtime|type; filetype: all|file|dir; pattern: glob like *.pdf.
        System folders (#recycle, @eaDir) are hidden unless include_system is true."""
        data = session.fs().list_folder(
            path, offset=offset, limit=limit, sort_by=sort_by, sort_direction=sort_direction,
            pattern=pattern, filetype=filetype, include_system=include_system,
        )
        return {"total": data["total"], "offset": data["offset"],
                "hidden_system": data["hidden_system"], "items": [_file(f) for f in data["files"]]}

    @tool(READ_ONLY)
    def get_file_info(paths: list[str]) -> list[dict[str, Any]]:
        """Details (size, owner, modified time, real volume path) for one or more paths."""
        return [_file(f) for f in session.fs().get_info(paths)]

    @tool(READ_ONLY)
    def folder_size(paths: list[str]) -> dict[str, Any]:
        """Total bytes, file count and subfolder count (excluding the folder itself)."""
        return session.fs().dir_size(paths)

    @tool(READ_ONLY)
    def file_md5(path: str) -> dict[str, str]:
        """MD5 checksum of a file, computed on the NAS."""
        return {"path": path, "md5": session.fs().md5(path)}

    @tool(READ_ONLY)
    def search_files(
        folder: str,
        pattern: str | None = None,
        extension: str | None = None,
        filetype: str = "all",
        max_results: int = 100,
    ) -> dict[str, Any]:
        """Recursive search under a folder. pattern: glob(s) on the name, space-separated
        (e.g. "*report* *.md"); extension: comma-separated (e.g. "pdf,docx")."""
        found = session.fs().search(folder, pattern=pattern, extension=extension,
                                    filetype=filetype, max_results=max_results)
        return {"total": found["total"], "items": [_file(f) for f in found["files"]]}

    @tool(READ_ONLY)
    def list_archive(path: str, recursive: bool = True) -> list[dict[str, Any]]:
        """Contents of a zip/7z/tar/rar archive on the NAS without extracting it."""
        return [{"path": i.get("path"), "is_dir": i.get("is_dir"), "size": i.get("size"),
                 "packed_size": i.get("pack_size"), "depth": i.get("depth")}
                for i in session.fs().list_archive(path, recursive=recursive)]

    @tool(READ_ONLY)
    def get_thumbnail(path: str, size: str = "medium") -> Image:
        """Thumbnail of an image file. size: small (~120px) | medium (~360px) | large | original."""
        data, content_type = session.fs().thumbnail(path, size=size)
        fmt = content_type.split("/")[-1].split(";")[0] or "jpeg"
        return Image(data=data, format=fmt)

    @tool(READ_ONLY)
    def download_file(path: str, save: bool = False) -> dict[str, Any]:
        """Read a file. Small UTF-8 text comes back inline; anything else (or save=true)
        is saved under SYNO_MCP_LOCAL_DIR and the local path is returned."""
        data = session.fs().download(path)
        if not save and len(data) <= INLINE_TEXT_LIMIT:
            try:
                return {"path": path, "size": len(data), "text": data.decode("utf-8")}
            except UnicodeDecodeError:
                pass
        target = local_path(posixpath.basename(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return {"path": path, "size": len(data), "saved_to": str(target)}

    @tool(READ_ONLY)
    def list_background_tasks() -> dict[str, Any]:
        """Running copy/move/delete/compress/extract tasks (finished ones may already be gone)."""
        return session.fs().background_tasks()

    @tool(READ_ONLY)
    def list_share_links() -> list[dict[str, Any]]:
        """Share links owned by the NAS account."""
        return [_link(link) for link in session.fs().list_links()]

    # == write tools (opt-in) ======================================================
    if policy.allow_writes:

        @tool(WRITE)
        def create_folder(parent: str, name: str, create_parents: bool = False) -> dict[str, Any]:
            """Create a folder inside an allowed root."""
            policy.check_write_parent(parent)
            policy.check_write(f"{parent}/{name}")
            return _file(session.fs().create_folder(parent, name, parents=create_parents))

        @tool(WRITE)
        def upload_file(
            dest_folder: str,
            local_file: str | None = None,
            filename: str | None = None,
            text: str | None = None,
            overwrite: bool = False,
        ) -> dict[str, Any]:
            """Upload a file from SYNO_MCP_LOCAL_DIR (local_file, relative name) or inline
            text (text + filename). Existing files are skipped unless overwrite is true."""
            policy.check_write_parent(dest_folder)
            if (local_file is None) == (text is None):
                raise ToolError("Pass exactly one of local_file or text.")
            if text is not None:
                if not filename:
                    raise ToolError("filename is required with text.")
                source: Path | bytes = text.encode("utf-8")
            else:
                source = local_path(local_file)
                if not source.is_file():
                    raise ToolError(f"Local file not found: {local_file}")
            name = filename or Path(local_file).name
            policy.check_write(f"{dest_folder}/{name}")
            result = session.fs().upload(dest_folder, source, filename=name,
                                         overwrite="overwrite" if overwrite else "skip")
            return {"path": f"{dest_folder}/{name}", "skipped": bool(result.get("blSkip"))}

        @tool(WRITE)
        def rename(path: str, new_name: str) -> dict[str, Any]:
            """Rename a file or folder in place (new_name is a name, not a path)."""
            if "/" in new_name:
                raise ToolError("new_name must not contain '/'; use copy_move to move items.")
            policy.check_write(path)
            policy.check_write(f"{posixpath.dirname(path)}/{new_name}")
            return _file(session.fs().rename(path, new_name))

        @tool(WRITE)
        def copy_move(paths: list[str], dest_folder: str, move: bool = False,
                      overwrite: bool = False) -> dict[str, Any]:
            """Copy (or move) items into a folder inside an allowed root. Moving also
            requires the sources to be inside an allowed root. With overwrite=false, items
            that already exist in dest_folder are skipped."""
            policy.check_write_parent(dest_folder)
            if move:
                for p in paths:
                    policy.check_write(p)
            status = session.fs().copy_move(paths, dest_folder, move=move, overwrite=overwrite)
            return {"finished": status.get("finished"), "dest_folder": dest_folder, "moved": move}

        @tool(WRITE)
        def compress(paths: list[str], dest_file_path: str, password: str | None = None) -> dict[str, Any]:
            """Zip files/folders into dest_file_path (inside an allowed root)."""
            policy.check_write(dest_file_path)
            status = session.fs().compress(paths, dest_file_path, password=password)
            return {"finished": status.get("finished"), "archive": dest_file_path}

        @tool(WRITE)
        def extract(archive: str, dest_folder: str, overwrite: bool = False,
                    password: str | None = None) -> dict[str, Any]:
            """Extract an archive into an existing folder inside an allowed root."""
            policy.check_write_parent(dest_folder)
            status = session.fs().extract(archive, dest_folder, overwrite=overwrite, password=password)
            return {"finished": status.get("finished"), "dest_folder": dest_folder}

        @tool(DESTRUCTIVE)
        def delete(paths: list[str]) -> dict[str, Any]:
            """Delete files/folders (recursively) inside an allowed root. With the recycle
            bin enabled on the share, DSM moves them to #recycle."""
            for p in paths:
                policy.check_write(p)
            status = session.fs().delete(paths)
            return {"finished": status.get("finished"), "deleted": paths}

    # == share links (opt-in, may be public) ========================================
    if policy.allow_sharing:

        @tool(EXPOSING)
        def create_share_link(path: str, password: str | None = None,
                              expires_in_days: int = 1) -> dict[str, Any]:
            """Create a share link for a file/folder inside an allowed root. With QuickConnect
            the URL (gofile.me) is reachable from the internet. Expires after
            expires_in_days (1-30)."""
            policy.check_sharing(path)
            if not 1 <= expires_in_days <= 30:
                raise ToolError("expires_in_days must be between 1 and 30.")
            expires = date.today() + timedelta(days=expires_in_days)
            link = session.fs().create_link(path, password=password, expires=expires)
            info = session.fs().get_link(link["id"])
            return {**_link(info), "public_internet": "gofile.me" in (info.get("url") or "")}

        @tool(EXPOSING)
        def edit_share_link(link_id: str, password: str | None = None,
                            expires_in_days: int | None = None) -> dict[str, Any]:
            """Change a link's password ("" removes it) and/or expiry (1-30 days from today)."""
            current = session.fs().get_link(link_id)
            policy.check_sharing(current.get("path", ""))
            expires = None
            if expires_in_days is not None:
                if not 1 <= expires_in_days <= 30:
                    raise ToolError("expires_in_days must be between 1 and 30.")
                expires = date.today() + timedelta(days=expires_in_days)
            session.fs().edit_link(link_id, password=password, expires=expires)
            return _link(session.fs().get_link(link_id))

        @tool(EXPOSING)
        def delete_share_link(link_ids: list[str]) -> dict[str, Any]:
            """Delete share links whose target is inside an allowed root."""
            for link_id in link_ids:
                policy.check_sharing(session.fs().get_link(link_id).get("path", ""))
            session.fs().delete_links(link_ids)
            return {"deleted": link_ids}

    # == prompts: ready-made tasks that combine the tools ===========================
    # Prompts only produce instructions; the model still calls the tools (with the
    # client's usual approvals), so they never bypass the policy above.
    default_root = policy.roots[0] if policy.roots else "/"

    @server.prompt(title="Audit NAS access")
    def audit_access() -> str:
        """Check which shares the NAS account can reach and whether that matches the server policy."""
        return (
            "Audit what this Synology NAS account can access.\n"
            "1. Call nas_info and report the hostname, whether the account is an admin, and the "
            "server policy (writes, sharing, write roots).\n"
            "2. Call list_shares and show a table: share, free space, total space (human-readable), "
            "read-only.\n"
            f"3. The intended working area is: {', '.join(policy.roots) or '(none)'}. Flag every "
            "share outside it other than the account's own /home, and flag it loudly if the "
            "account is an admin.\n"
            "4. Call list_share_links and list any active links with their expiry.\n"
            "End with concrete DSM steps to remove access that isn't needed "
            "(Control Panel → Shared Folder → Edit → Permissions → No access)."
        )

    @server.prompt(title="Find large files")
    def find_large_files(folder: str = default_root, top: str = "10") -> str:
        """Report the largest files under a folder."""
        return (
            f"Find the {top} largest files under {folder} on the NAS.\n"
            f"1. Call folder_size for {folder} and report the total size and file count.\n"
            f"2. Call search_files with folder={folder}, filetype=file, max_results=500.\n"
            f"3. Sort the results by size and show the top {top} as a table: path, size "
            "(human-readable), modified date.\n"
            "If the search returned 500 results, say the list may be incomplete. Don't change "
            "anything on the NAS."
        )

    @server.prompt(title="Clean up PoC leftovers")
    def clean_sandbox(folder: str = default_root) -> str:
        """Find PoC leftovers (poc-run-*, poc-share-*, poc-probe-*) and offer to remove them."""
        steps = [
            f"Find leftovers from the Synology PoC in {folder}.",
            f"1. Call list_folder for {folder} and collect folders named poc-run-*, poc-share-* "
            "or poc-probe-*. Use folder_size on each to show how much space it uses.",
            "2. Call list_share_links and collect links whose path is inside a poc-share-* folder.",
            "3. Show everything you found and ask me to confirm before removing anything.",
        ]
        if policy.allow_sharing:
            steps.append("4. After I confirm, delete those share links first with delete_share_link.")
        else:
            steps.append("4. Share links can't be removed from here (SYNO_MCP_ALLOW_SHARING is off); "
                         "tell me to run `uv run examples/05_sharing_and_thumbs.py --cleanup` instead.")
        if policy.allow_writes:
            steps.append("5. After I confirm, delete the folders with the delete tool, then list "
                         f"{folder} again to show they're gone. Mention that DSM may keep them in #recycle.")
        else:
            steps.append("5. Deleting needs SYNO_MCP_ALLOW_WRITES=true in .env and a server restart; "
                         "say so instead of trying.")
        return "\n".join(steps)

    return server


# -- HTTP transport (e.g. a container in Synology Container Manager) ---------------
MIN_TOKEN_LENGTH = 32
HEALTH_PATH = "/healthz"


@dataclass(frozen=True)
class HttpConfig:
    token: str = field(repr=False)
    host: str = "127.0.0.1"
    port: int = 8000

    @classmethod
    def from_env(cls) -> HttpConfig:
        token = os.getenv("SYNO_MCP_TOKEN", "").strip()
        if len(token) < MIN_TOKEN_LENGTH:
            sys.exit(
                f"SYNO_MCP_TRANSPORT=http needs SYNO_MCP_TOKEN with at least {MIN_TOKEN_LENGTH} "
                "characters (e.g. `openssl rand -hex 32`)."
            )
        return cls(
            token=token,
            host=os.getenv("SYNO_MCP_HOST") or "127.0.0.1",
            port=int(os.getenv("SYNO_MCP_PORT") or "8000"),
        )


class BearerTokenGuard:
    """ASGI middleware: every HTTP request except the health check needs the shared token."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] != HEALTH_PATH:
            given = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(given, self.expected):
                deny = JSONResponse({"error": "unauthorized"}, 401, headers={"WWW-Authenticate": "Bearer"})
                await deny(scope, receive, send)
                return
        await self.app(scope, receive, send)


def http_app(server: MCPServer, config: HttpConfig) -> Starlette:
    """The MCP endpoint at /mcp behind the token check, plus an open GET /healthz."""

    @server.custom_route(HEALTH_PATH, methods=["GET"], include_in_schema=False)
    async def health(request: Request) -> Response:
        return PlainTextResponse("ok")

    # Plain JSON responses instead of SSE streams: reverse proxies (DSM's nginx) buffer SSE.
    app = server.streamable_http_app(host=config.host, json_response=True)
    app.add_middleware(BearerTokenGuard, token=config.token)
    return app


def http_server(app: ASGIApp, config: HttpConfig) -> uvicorn.Server:
    log_config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    log_config["handlers"]["access"]["stream"] = "ext://sys.stderr"  # keep stdout clean
    return uvicorn.Server(uvicorn.Config(app, host=config.host, port=config.port, log_config=log_config))


def transport_from_env() -> str:
    transport = (os.getenv("SYNO_MCP_TRANSPORT") or "stdio").strip().lower()
    if transport not in ("stdio", "http"):
        sys.exit(f"SYNO_MCP_TRANSPORT must be 'stdio' or 'http', not {transport!r}")
    return transport


def main() -> None:
    config = ServerConfig.from_env()  # also loads .env
    if transport_from_env() == "stdio":
        build_server(config).run("stdio")
        return
    http = HttpConfig.from_env()
    http_server(http_app(build_server(config), http), http).run()


if __name__ == "__main__":
    main()
