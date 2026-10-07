"""In-memory stand-in for a DSM 7 File Station, patched into requests.Session.

It models the behavior verified on the real NAS (2026-10-07), including the quirks the
official guide doesn't document:

- Compress returns 105 unless the session came from an Auth v7 login.
- Upload conflict without `overwrite` returns 414 (the guide says 1805).
- Extract.list returns one folder level per call and adds a ".." entry inside folders.
- BackgroundTask.list drops tasks whose finished status has already been read.
- Thumb returns HTTP 404 on the first request for a file (lazy generation).
- Sharing dates: "" when unset, "YYYY-MM-DD 23:59:59" after edit; URLs on gofile.me.
- DirSize num_dir excludes the queried folder itself.

It is deliberately lenient about raw vs JSON-quoted strings (the real NAS accepts both
for most params), so it can't catch quoting mistakes; the real-NAS runs do that.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import io
import json
import posixpath
import zipfile
from typing import Any

import requests

PASSWORD = "secret"
VERSIONS = {
    "SYNO.API.Auth": (1, 7), "SYNO.FileStation.Info": (1, 2), "SYNO.FileStation.List": (1, 2),
    "SYNO.FileStation.CreateFolder": (1, 2), "SYNO.FileStation.Rename": (1, 2),
    "SYNO.FileStation.CopyMove": (1, 3), "SYNO.FileStation.Delete": (1, 2),
    "SYNO.FileStation.Download": (1, 2), "SYNO.FileStation.Upload": (2, 3),
    "SYNO.FileStation.Search": (1, 2), "SYNO.FileStation.DirSize": (1, 2),
    "SYNO.FileStation.MD5": (1, 2), "SYNO.FileStation.Compress": (1, 3),
    "SYNO.FileStation.Extract": (1, 2), "SYNO.FileStation.BackgroundTask": (1, 3),
    "SYNO.FileStation.Thumb": (1, 3), "SYNO.FileStation.Sharing": (1, 3),
}
ASYNC_APIS = {
    "SYNO.FileStation.CopyMove", "SYNO.FileStation.Delete", "SYNO.FileStation.DirSize",
    "SYNO.FileStation.MD5", "SYNO.FileStation.Search", "SYNO.FileStation.Compress",
    "SYNO.FileStation.Extract",
}


class Response:
    def __init__(self, body: Any = None, content: bytes | None = None,
                 ctype: str = "application/json", status: int = 200):
        self._body = body
        self.content = content if content is not None else json.dumps(body).encode()
        self.headers = {"Content-Type": ctype}
        self.status_code = status

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def ok(data: Any = None) -> Response:
    return Response({"success": True, "data": data or {}})


def err(code: int, errors: list | None = None) -> Response:
    error: dict[str, Any] = {"code": code}
    if errors:
        error["errors"] = errors
    return Response({"success": False, "error": error})


def as_json(value: str) -> Any:
    return json.loads(value)


def as_str(value: str) -> str:
    """Accept both raw and JSON-quoted strings."""
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, str) else value
    except (ValueError, TypeError):
        return value


class FakeNAS:
    def __init__(self, shares: tuple[str, ...] = ("/poc-sandbox", "/home")):
        self.fs: dict[str, bytes | None] = {}  # None = folder
        for share in shares:
            self.fs[share] = None
        self.fs["/poc-sandbox/#recycle"] = None
        self.sessions: dict[str, int] = {}  # sid -> Auth version used at login
        self.logins = 0
        self.tasks: dict[str, dict[str, Any]] = {}
        self.links: dict[str, dict[str, Any]] = {}
        self.thumbs_served: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.polls_until_done = 2

    # -- wiring ------------------------------------------------------------------
    def install(self, monkeypatch: Any) -> None:
        nas = self

        def post(session: requests.Session, url: str, data: dict | None = None,
                 params: dict | None = None, files: dict | None = None, timeout: float | None = None):
            merged = {**(params or {}), **(data or {})}
            p = {k: str(v) for k, v in merged.items() if v is not None}
            upload = None
            if files:
                body = requests.Request("POST", url, params=params, data=data, files=files).prepare().body
                assert body.rfind(b'name="file"') > body.rfind(b'name="path"'), "file part must be last"
                name, payload, _ = files["file"]
                upload = (name, payload.read() if hasattr(payload, "read") else payload)
            return nas.handle(p, upload)

        def get(session: requests.Session, url: str, params: dict | None = None,
                timeout: float | None = None, stream: bool = False):
            return nas.handle({k: str(v) for k, v in (params or {}).items() if v is not None})

        monkeypatch.setattr(requests.Session, "post", post)
        monkeypatch.setattr(requests.Session, "get", get)

    def expire_sessions(self) -> None:
        """Simulate DSM dropping every session (e.g. timeout or reboot)."""
        self.sessions.clear()

    # -- helpers -------------------------------------------------------------------
    def is_dir(self, path: str) -> bool:
        return path in self.fs and self.fs[path] is None

    def tree(self, root: str) -> list[str]:
        return [k for k in self.fs if k == root or k.startswith(root + "/")]

    def children(self, folder: str) -> list[str]:
        return sorted(p for p in self.fs if posixpath.dirname(p) == folder and p != folder)

    def mkdirs(self, folder: str) -> None:
        parts = folder.strip("/").split("/")
        for i in range(1, len(parts) + 1):
            self.fs.setdefault("/" + "/".join(parts[:i]), None)

    def info(self, path: str) -> dict[str, Any]:
        data = self.fs[path]
        return {
            "path": path, "name": posixpath.basename(path), "isdir": data is None,
            "additional": {
                "size": 0 if data is None else len(data),
                "time": {"mtime": 1_790_000_000}, "type": "" if data is None else
                posixpath.splitext(path)[1].lstrip(".").upper(),
                "real_path": "/volume1" + path,
            },
        }

    def start_task(self, api: str, params: dict[str, str], run: Any) -> Response:
        taskid = f"FileStation_{len(self.tasks):04d}"
        self.tasks[taskid] = {"api": api, "params": params, "polls": 0, "run": run,
                              "result": None, "reaped": False}
        return ok({"taskid": taskid})

    # -- dispatcher ------------------------------------------------------------------
    def handle(self, p: dict[str, str], upload: tuple[str, bytes] | None = None) -> Response:
        api, method = p.get("api", ""), p.get("method", "")
        self.calls.append((api, method))
        if api == "SYNO.API.Info":
            return ok({k: {"path": "entry.cgi", "minVersion": lo, "maxVersion": hi}
                       for k, (lo, hi) in VERSIONS.items()})
        if api == "SYNO.API.Auth":
            if method == "login":
                if p.get("passwd") != PASSWORD:
                    return err(400)
                self.logins += 1
                sid = f"SID{self.logins:04d}xyz"
                self.sessions[sid] = int(p["version"])
                return ok({"sid": sid})
            self.sessions.pop(p.get("_sid", ""), None)
            return ok()
        sid = p.get("_sid")
        if sid not in self.sessions:
            return err(119)
        handler = getattr(self, "api_" + api.rsplit(".", 1)[-1], None)
        if api in ASYNC_APIS and method in ("status", "list") and "taskid" in p:
            return self.poll(api, p)
        if handler is None:
            return err(102)
        return handler(method, p, upload, self.sessions[sid])

    def poll(self, api: str, p: dict[str, str]) -> Response:
        taskid = as_json(p["taskid"])
        assert isinstance(taskid, str), "taskid must be JSON-quoted"
        task = self.tasks.get(taskid)
        if task is None:
            return err(599)
        task["polls"] += 1
        if task["polls"] < self.polls_until_done:
            return ok({"finished": False, "progress": 0.5})
        if task["result"] is None:
            task["result"] = task["run"](task["params"])
        task["reaped"] = True  # DSM 7 drops it from BackgroundTask.list once read
        result = {"finished": True, "progress": 1.0, **task["result"]}
        if api == "SYNO.FileStation.Search":
            files = result["files"]
            limit = int(p.get("limit", 0))
            shown = files if limit == -1 else files[:limit]
            return ok({"finished": True, "total": len(files), "offset": 0, "files": shown})
        return ok(result)

    # -- APIs --------------------------------------------------------------------------
    def api_Info(self, method, p, upload, auth_version):
        return ok({"hostname": "fakenas", "is_manager": False, "support_sharing": True,
                   "support_virtual_protocol": []})

    def api_List(self, method, p, upload, auth_version):
        if method == "list_share":
            shares = [self.info(s) for s in sorted(self.fs) if s.count("/") == 1]
            return ok({"total": len(shares), "offset": 0, "shares": shares})
        if method == "list":
            folder = as_str(p["folder_path"])
            if folder == "/homes":
                return err(407)
            if not self.is_dir(folder):
                return err(408)
            files = [self.info(c) for c in self.children(folder)]
            pattern = p.get("pattern")
            if pattern:
                files = [f for f in files if fnmatch.fnmatch(f["name"].lower(), pattern.lower())]
            offset, limit = int(p.get("offset", 0)), int(p.get("limit", 0))
            page = files[offset:offset + limit] if limit else files[offset:]
            return ok({"total": len(files), "offset": offset, "files": page})
        if method == "getinfo":
            return ok({"files": [self.info(x) for x in as_json(p["path"]) if x in self.fs]})
        return err(103)

    def api_CreateFolder(self, method, p, upload, auth_version):
        made = []
        for parent, name in zip(as_json(p["folder_path"]), as_json(p["name"])):
            if parent not in self.fs:
                if p.get("force_parent") != "true":
                    return err(1100, [{"code": 408, "path": parent}])
                self.mkdirs(parent)
            self.fs[f"{parent}/{name}"] = None
            made.append(self.info(f"{parent}/{name}"))
        return ok({"folders": made})

    def api_Upload(self, method, p, upload, auth_version):
        dest = p["path"]
        name, payload = upload
        if dest not in self.fs:
            if p.get("create_parents") != "true":
                return err(408)
            self.mkdirs(dest)
        target = f"{dest}/{name}"
        overwrite = p.get("overwrite")
        if target in self.fs:
            if overwrite is None:
                return err(414)
            if overwrite == "skip":
                return ok({"blSkip": True, "file": name})
        self.fs[target] = payload
        return ok({"blSkip": False, "file": name})

    def api_Download(self, method, p, upload, auth_version):
        path = as_json(p["path"])[0]
        if path not in self.fs or self.fs[path] is None:
            return err(408)
        return Response(content=self.fs[path], ctype="application/octet-stream")

    def api_Rename(self, method, p, upload, auth_version):
        renamed = []
        for src, name in zip(as_json(p["path"]), as_json(p["name"])):
            if src not in self.fs:
                return err(1200, [{"code": 408, "path": src}])
            new = f"{posixpath.dirname(src)}/{name}"
            for key in sorted(self.tree(src)):
                self.fs[new + key[len(src):]] = self.fs.pop(key)
            renamed.append(self.info(new))
        return ok({"files": renamed})

    def api_CopyMove(self, method, p, upload, auth_version):
        def run(t):
            dest = as_str(t["dest_folder_path"])
            for src in as_json(t["path"]):
                base = posixpath.dirname(src)
                for key in sorted(self.tree(src)):
                    self.fs[dest + key[len(base):]] = self.fs[key]
                if t.get("remove_src") == "true":
                    for key in self.tree(src):
                        del self.fs[key]
            return {"dest_folder_path": dest}
        return self.start_task("SYNO.FileStation.CopyMove", p, run)

    def api_Delete(self, method, p, upload, auth_version):
        def run(t):
            for root in as_json(t["path"]):
                for key in self.tree(root):
                    del self.fs[key]
            return {}
        return self.start_task("SYNO.FileStation.Delete", p, run)

    def api_DirSize(self, method, p, upload, auth_version):
        def run(t):
            roots = as_json(t["path"])
            keys = [k for r in roots for k in self.tree(r)]
            files = [k for k in keys if not self.is_dir(k)]
            dirs = [k for k in keys if self.is_dir(k) and k not in roots]
            return {"num_file": len(files), "num_dir": len(dirs),
                    "total_size": sum(len(self.fs[f]) for f in files)}
        return self.start_task("SYNO.FileStation.DirSize", p, run)

    def api_MD5(self, method, p, upload, auth_version):
        path = as_str(p["file_path"])
        if path not in self.fs:
            return err(408)
        return self.start_task("SYNO.FileStation.MD5", p,
                               lambda t: {"md5": hashlib.md5(self.fs[path]).hexdigest()})

    def api_Search(self, method, p, upload, auth_version):
        if method in ("stop", "clean"):
            as_json(p["taskid"])
            return ok()

        def run(t):
            pattern = t.get("pattern", "*").lower()
            hits = [k for r in as_json(t["folder_path"]) for k in self.tree(r)
                    if k != r and fnmatch.fnmatch(posixpath.basename(k).lower(), pattern)]
            return {"files": [self.info(h) for h in sorted(hits)]}
        return self.start_task("SYNO.FileStation.Search", p, run)

    def api_Compress(self, method, p, upload, auth_version):
        if auth_version < 7:
            return err(105)  # the real DSM 7 behavior for Auth v3/v6 sessions

        def run(t):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for src in as_json(t["path"]):
                    base = posixpath.dirname(src)
                    for key in sorted(self.tree(src)):
                        rel = key[len(base) + 1:]
                        if self.is_dir(key):
                            zf.writestr(rel + "/", b"")
                        else:
                            zf.writestr(rel, self.fs[key])
            dest = as_str(t["dest_file_path"])
            self.fs[dest] = buf.getvalue()
            return {"dest_file_path": dest}
        return self.start_task("SYNO.FileStation.Compress", p, run)

    def api_Extract(self, method, p, upload, auth_version):
        archive = as_str(p["file_path"])
        if archive not in self.fs:
            return err(408)
        with zipfile.ZipFile(io.BytesIO(self.fs[archive])) as zf:
            infos = list(enumerate(zf.infolist()))
        if method == "list":
            item_id = p.get("item_id")
            parent = "" if item_id in (None, "-1") else infos[int(item_id)][1].filename.rstrip("/")
            items = [{"item_id": i, "name": posixpath.basename(x.filename.rstrip("/")),
                      "path": x.filename.rstrip("/"), "size": x.file_size,
                      "pack_size": x.compress_size, "is_dir": x.is_dir(),
                      "mtime": "2026-10-07 12:00:00"}
                     for i, x in infos if posixpath.dirname(x.filename.rstrip("/")) == parent]
            if parent:
                items.insert(0, {"is_dir": True, "item_id": -1, "name": "..", "path": "root"})
            return ok({"items": items, "total": len(items)})

        def run(t):
            dest = as_str(t["dest_folder_path"])
            if dest not in self.fs:
                raise AssertionError("destination folder must exist")
            with zipfile.ZipFile(io.BytesIO(self.fs[archive])) as zf:
                for info in zf.infolist():
                    target = f"{dest}/{info.filename.rstrip('/')}"
                    self.mkdirs(posixpath.dirname(target))
                    self.fs[target] = None if info.is_dir() else zf.read(info)
            return {"dest_folder_path": dest}
        return self.start_task("SYNO.FileStation.Extract", p, run)

    def api_BackgroundTask(self, method, p, upload, auth_version):
        if method == "list":
            wanted = as_json(p["api_filter"]) if "api_filter" in p else None
            tasks = [{"api": t["api"], "taskid": k, "finished": t["result"] is not None}
                     for k, t in self.tasks.items()
                     if not t["reaped"] and (not wanted or t["api"] in wanted)]
            return ok({"total": len(tasks), "offset": 0, "tasks": tasks})
        if method == "clear_finished":
            for taskid in as_json(p["taskid"]):
                self.tasks.pop(taskid, None)
            return ok()
        return err(103)

    def api_Thumb(self, method, p, upload, auth_version):
        path = as_str(p["path"])
        if path not in self.fs:
            return Response(content=b"", ctype="text/html", status=404)
        if path not in self.thumbs_served:
            self.thumbs_served.add(path)
            return Response(content=b"generating", ctype="text/html", status=404)
        return Response(content=self.fs[path], ctype="image/png")

    def api_Sharing(self, method, p, upload, auth_version):
        if method == "create":
            path = as_json(p["path"])[0]
            if path not in self.fs:
                return ok({"links": [{"error": 408, "path": path}]})
            link_id = f"Lnk{len(self.links) + 1:05d}"
            self.links[link_id] = {
                "id": link_id, "path": path, "name": posixpath.basename(path),
                "url": f"https://gofile.me/fake1/{link_id}", "status": "valid",
                "has_password": bool(p.get("password")), "date_expired": "",
                "date_available": "", "link_owner": "poc-user", "isFolder": self.is_dir(path),
            }
            qr = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nfakeqr").decode()
            return ok({"links": [{"id": link_id, "path": path, "url": self.links[link_id]["url"],
                                  "qrcode": qr, "error": 0}]})
        if method == "getinfo":
            link_id = as_json(p["id"])
            return ok(self.links[link_id]) if link_id in self.links else err(2000)
        if method == "edit":
            for link_id in as_json(p["id"]):
                if link_id not in self.links:
                    return err(2000)
                if "date_expired" in p:
                    self.links[link_id]["date_expired"] = as_json(p["date_expired"]) + " 23:59:59"
                if "password" in p:
                    self.links[link_id]["has_password"] = bool(p["password"])
            return ok()
        if method == "list":
            return ok({"total": len(self.links), "offset": 0, "links": list(self.links.values())})
        if method == "delete":
            for link_id in as_json(p["id"]):
                self.links.pop(link_id, None)
            return ok()
        return err(103)
