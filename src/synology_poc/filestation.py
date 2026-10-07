"""High-level File Station operations, one method per use case (see docs/use-cases.md).

Every method here was verified against a real DSM 7 NAS. The wire-format details that
differ from the official guide live in this module, not in callers:

- Auth v7 login (client.DOC_VERSIONS): Compress returns 105 for v3/v6 sessions.
- Compress/Extract strings are JSON-quoted, with level "normal" and mode "replace",
  matching the DSM 7 web UI.
- Extract.list returns one folder level per call and adds a ".." parent entry.
- Search must always be stopped and cleaned, or its temporary database lingers.
- Thumb can 404 briefly while a thumbnail is generated.
- System folders (#recycle, @eaDir, ...) are hidden from listings unless asked for.

This layer has no printing and no policy: callers (examples, MCP server) decide what's allowed.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable, Iterable
from datetime import date
from pathlib import Path
from typing import Any

from .client import SynologyClient, quoted
from .errors import SynologyError

SYSTEM_PREFIXES = ("#", "@")
DEFAULT_ADDITIONAL = ("real_path", "size", "owner", "time", "perm", "type")
Progress = Callable[[dict], None] | None


def _as_list(paths: str | Iterable[str]) -> list[str]:
    return [paths] if isinstance(paths, str) else list(paths)


def _date(value: date | str | None) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, date) else value


class FileStation:
    def __init__(self, client: SynologyClient, *, task_timeout: float = 300):
        self.client = client
        self.task_timeout = task_timeout

    def _call(self, api: str, method: str, **params: Any) -> dict[str, Any]:
        return self.client.call(f"SYNO.FileStation.{api}", method, **params)

    def _wait(self, api: str, taskid: str, on_progress: Progress = None, **kw: Any) -> dict:
        """Poll until finished; the final status always carries its `taskid`."""
        status = self.client.wait_task(
            f"SYNO.FileStation.{api}", taskid, timeout=self.task_timeout, on_progress=on_progress, **kw
        )
        return {**status, "taskid": taskid}

    # -- UC-02 info / UC-03 shares ----------------------------------------------
    def info(self) -> dict[str, Any]:
        """hostname, is_manager, support_sharing, support_virtual_protocol (a list on DSM 7)."""
        return self._call("Info", "get")

    def list_shares(
        self,
        additional: Iterable[str] = ("real_path", "owner", "time", "perm", "volume_status"),
        only_writable: bool = False,
    ) -> list[dict[str, Any]]:
        data = self._call("List", "list_share", additional=list(additional), onlywritable=only_writable)
        return data.get("shares", [])

    # -- UC-04 list folder / UC-05 details --------------------------------------
    def list_folder(
        self,
        path: str,
        *,
        offset: int = 0,
        limit: int = 0,
        sort_by: str = "name",
        sort_direction: str = "asc",
        pattern: str | None = None,
        filetype: str = "all",
        additional: Iterable[str] = ("size", "time", "type"),
        include_system: bool = False,
    ) -> dict[str, Any]:
        """{total, offset, files, hidden_system}. `total` is the NAS count incl. system folders."""
        data = self._call(
            "List", "list", folder_path=path, offset=offset, limit=limit, sort_by=sort_by,
            sort_direction=sort_direction, pattern=pattern, filetype=filetype,
            additional=list(additional),
        )
        files = data.get("files", [])
        shown = files if include_system else [f for f in files if not f["name"].startswith(SYSTEM_PREFIXES)]
        return {
            "total": data.get("total", len(files)),
            "offset": data.get("offset", offset),
            "files": shown,
            "hidden_system": len(files) - len(shown),
        }

    def get_info(
        self, paths: str | Iterable[str], additional: Iterable[str] = DEFAULT_ADDITIONAL
    ) -> list[dict[str, Any]]:
        data = self._call("List", "getinfo", path=_as_list(paths), additional=list(additional))
        return data.get("files", [])

    # -- UC-06..11 file operations ----------------------------------------------
    def create_folder(self, parent: str, name: str, *, parents: bool = False) -> dict[str, Any]:
        data = self._call("CreateFolder", "create", folder_path=[parent], name=[name], force_parent=parents)
        return data.get("folders", [{}])[0]

    def upload(
        self,
        dest_folder: str,
        source: str | Path | bytes,
        *,
        filename: str | None = None,
        overwrite: str | None = "overwrite",
        create_parents: bool = True,
    ) -> dict[str, Any]:
        """overwrite: "overwrite" | "skip" | None. With None, a conflict raises 414 on DSM 7."""
        return self.client.upload(
            dest_folder, source, filename=filename, overwrite=overwrite, create_parents=create_parents
        )

    def download(self, path: str) -> bytes:
        return self.client.download(path)

    def rename(self, path: str, new_name: str) -> dict[str, Any]:
        data = self._call("Rename", "rename", path=[path], name=[new_name])
        return data.get("files", [{}])[0]

    def copy_move(
        self,
        paths: str | Iterable[str],
        dest_folder: str,
        *,
        move: bool = False,
        overwrite: bool | None = True,
        on_progress: Progress = None,
    ) -> dict[str, Any]:
        """overwrite=None: the NAS rejects conflicts with 1003."""
        task = self._call(
            "CopyMove", "start", path=_as_list(paths), dest_folder_path=dest_folder,
            overwrite=overwrite, remove_src=move,
        )
        return self._wait("CopyMove", task["taskid"], on_progress)

    def delete(
        self, paths: str | Iterable[str], *, recursive: bool = True, on_progress: Progress = None
    ) -> dict[str, Any]:
        """Async delete. With the recycle bin enabled, DSM moves items to #recycle."""
        task = self._call(
            "Delete", "start", path=_as_list(paths), recursive=recursive, accurate_progress=True
        )
        return self._wait("Delete", task["taskid"], on_progress)

    # -- UC-12 folder size / UC-13 MD5 ------------------------------------------
    def dir_size(self, paths: str | Iterable[str]) -> dict[str, int]:
        """{num_file, num_dir, total_size}; num_dir excludes the queried folder itself."""
        task = self._call("DirSize", "start", path=_as_list(paths))
        status = self._wait("DirSize", task["taskid"])
        return {k: status.get(k) for k in ("num_file", "num_dir", "total_size")}

    def md5(self, path: str) -> str:
        task = self._call("MD5", "start", file_path=path)
        status = self._wait("MD5", task["taskid"])
        if "md5" not in status:
            raise SynologyError("SYNO.FileStation.MD5", "status", 599)
        return status["md5"]

    # -- UC-14 search -----------------------------------------------------------
    def search(
        self,
        folders: str | Iterable[str],
        *,
        pattern: str | None = None,
        extension: str | None = None,
        filetype: str = "all",
        recursive: bool = True,
        size_from: int | None = None,
        size_to: int | None = None,
        mtime_from: int | None = None,
        mtime_to: int | None = None,
        owner: str | None = None,
        max_results: int = 500,
        additional: Iterable[str] = ("size", "time", "type"),
    ) -> dict[str, Any]:
        """pattern: glob(s), space-separated; extension: comma-separated. Always stops + cleans."""
        task = self._call(
            "Search", "start", folder_path=_as_list(folders), recursive=recursive, pattern=pattern,
            extension=extension, filetype=filetype, size_from=size_from, size_to=size_to,
            mtime_from=mtime_from, mtime_to=mtime_to, owner=owner,
        )
        taskid = task["taskid"]
        try:
            self._wait("Search", taskid, method="list", limit=0)
            found = self._call(
                "Search", "list", taskid=quoted(taskid), limit=max_results if max_results > 0 else -1,
                additional=list(additional),
            )
            return {"total": found.get("total", 0), "files": found.get("files", [])}
        finally:
            for method in ("stop", "clean"):
                try:
                    self._call("Search", method, taskid=quoted(taskid))
                except SynologyError:
                    pass

    # -- UC-15 compress / UC-16 list archive / UC-17 extract --------------------
    def compress(
        self,
        paths: str | Iterable[str],
        dest_file_path: str,
        *,
        level: str = "normal",
        mode: str = "replace",
        fmt: str = "zip",
        password: str | None = None,
        on_progress: Progress = None,
    ) -> dict[str, Any]:
        """Params mirror the DSM 7 web UI; the guide's form (level=moderate, raw strings) failed."""
        task = self._call(
            "Compress", "start", path=_as_list(paths), dest_file_path=quoted(dest_file_path),
            level=quoted(level), mode=quoted(mode), format=quoted(fmt),
            password=quoted(password) if password else "null", codepage=quoted("enu"),
        )
        return self._wait("Compress", task["taskid"], on_progress)

    def list_archive(
        self, path: str, *, recursive: bool = True, password: str | None = None
    ) -> list[dict[str, Any]]:
        """Flat list of archive items with a `depth` field (0 = archive root)."""
        out: list[dict[str, Any]] = []
        seen: set[int] = set()

        def walk(item_id: int | None, depth: int) -> None:
            data = self._call(
                "Extract", "list", file_path=quoted(path), limit=-1, codepage=quoted("enu"),
                item_id=item_id, password=quoted(password) if password else None,
            )
            items = [i for i in data.get("items", []) if i.get("name") != ".."]
            for it in sorted(items, key=lambda i: (not i.get("is_dir"), i.get("path") or "")):
                iid = it.get("item_id", it.get("itemid"))
                out.append({**it, "item_id": iid, "depth": depth})
                if recursive and it.get("is_dir") and iid not in seen and iid != item_id and depth < 8:
                    seen.add(iid)
                    walk(iid, depth + 1)

        walk(None, 0)
        return out

    def extract(
        self,
        archive: str,
        dest_folder: str,
        *,
        overwrite: bool = False,
        keep_dir: bool = True,
        create_subfolder: bool = False,
        password: str | None = None,
        on_progress: Progress = None,
    ) -> dict[str, Any]:
        """dest_folder must exist."""
        task = self._call(
            "Extract", "start", file_path=quoted(archive), dest_folder_path=quoted(dest_folder),
            overwrite=overwrite, keep_dir=keep_dir, create_subfolder=create_subfolder,
            codepage=quoted("enu"), password=quoted(password) if password else None,
        )
        return self._wait("Extract", task["taskid"], on_progress)

    # -- UC-18 background tasks -------------------------------------------------
    def background_tasks(self, api_filter: Iterable[str] | None = None) -> dict[str, Any]:
        """DSM 7 may already have dropped finished tasks whose status was read."""
        data = self._call(
            "BackgroundTask", "list", api_filter=list(api_filter) if api_filter else None
        )
        return {"total": data.get("total", 0), "tasks": data.get("tasks", [])}

    def clear_finished_tasks(self, taskids: Iterable[str] | None = None) -> None:
        ids = list(taskids) if taskids is not None else None
        if ids == []:
            return
        self._call("BackgroundTask", "clear_finished", taskid=ids)

    # -- UC-19 thumbnails -------------------------------------------------------
    def thumbnail(
        self, path: str, *, size: str = "medium", rotate: int = 0, retry_seconds: float = 5
    ) -> tuple[bytes, str]:
        """(bytes, content_type). Keeps the source format; `large` never upscales."""
        deadline = time.monotonic() + retry_seconds
        while True:
            try:
                return self.client.fetch_binary(
                    "SYNO.FileStation.Thumb", "get", path=path, size=size, rotate=rotate or None
                )
            except SynologyError as e:
                if not (e.http_status and e.code == 404) or time.monotonic() > deadline:
                    raise
                time.sleep(0.5)

    # -- UC-20 share links ------------------------------------------------------
    def create_link(
        self, path: str, *, password: str | None = None, expires: date | str | None = None
    ) -> dict[str, Any]:
        """Returns {id, url, path, qrcode_png (bytes|None), ...}. URLs may be public (gofile.me)."""
        data = self._call("Sharing", "create", path=[path], password=password)
        link = data["links"][0]
        if link.get("error"):
            raise SynologyError("SYNO.FileStation.Sharing", "create", int(link["error"]))
        qr = link.pop("qrcode", "") or ""
        link["qrcode_png"] = base64.b64decode(qr.split(",", 1)[-1]) if qr else None
        if expires is not None:
            self.edit_link(link["id"], expires=expires)
        return link

    def get_link(self, link_id: str) -> dict[str, Any]:
        """date_expired: "" when none, else "YYYY-MM-DD 23:59:59" on DSM 7."""
        return self._call("Sharing", "getinfo", id=quoted(link_id))

    def edit_link(
        self,
        link_id: str,
        *,
        password: str | None = None,
        expires: date | str | None = None,
        available: date | str | None = None,
    ) -> None:
        """password=None leaves it unchanged; "" removes it."""
        exp, avail = _date(expires), _date(available)
        self._call(
            "Sharing", "edit", id=[link_id], password=password,
            date_expired=quoted(exp) if exp else None,
            date_available=quoted(avail) if avail else None,
        )

    def list_links(self) -> list[dict[str, Any]]:
        return self._call("Sharing", "list").get("links", [])

    def delete_links(self, link_ids: Iterable[str]) -> None:
        ids = list(link_ids)
        if ids:
            self._call("Sharing", "delete", id=ids)
