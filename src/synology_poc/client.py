"""Minimal Synology File Station WebAPI client.

Reference: docs/synology/file-station-api.md
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from .errors import SynologyError

# Versions this PoC is written against (from the guide). The actual version used is
# clamped to what the NAS advertises via SYNO.API.Info.
DOC_VERSIONS = {
    "SYNO.API.Auth": 3,
    "SYNO.FileStation.Info": 2,
    "SYNO.FileStation.List": 2,
    "SYNO.FileStation.CreateFolder": 2,
    "SYNO.FileStation.Rename": 2,
    "SYNO.FileStation.CopyMove": 3,
    "SYNO.FileStation.Delete": 2,
    "SYNO.FileStation.Download": 2,
    "SYNO.FileStation.Upload": 3,
}


class FingerprintAdapter(HTTPAdapter):
    """Pin the server certificate by SHA-256 fingerprint (urllib3 assert_fingerprint).

    Used with verify=False: the chain/hostname is not checked, but the connection
    fails unless the presented certificate matches the fingerprint exactly.
    """

    def __init__(self, fingerprint: str, **kwargs: Any):
        self._fingerprint = fingerprint.replace(":", "").strip().lower()
        super().__init__(**kwargs)

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        kwargs["assert_fingerprint"] = self._fingerprint
        super().init_poolmanager(*args, **kwargs)


def encode(value: Any) -> str:
    """Encode a parameter the way the WebAPI expects (guide section 2)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value))
    return str(value)


def quoted(value: str) -> str:
    """JSON-quote a single string, as the guide's examples do for task IDs."""
    return json.dumps(value)


class SynologyClient:
    def __init__(
        self,
        host: str,
        port: int = 5001,
        *,
        ca_cert: str | None = None,
        cert_sha256: str | None = None,
        timeout: float = 30,
    ):
        if not ca_cert and not cert_sha256:
            raise ValueError(
                "TLS pinning required: set SYNO_CA_CERT or SYNO_CERT_SHA256 (see README)."
            )
        self.base_url = f"https://{host}:{port}/webapi"
        self.timeout = timeout
        self.session = requests.Session()
        if cert_sha256:
            self.session.verify = False
            self.session.mount("https://", FingerprintAdapter(cert_sha256))
        else:
            self.session.verify = ca_cert
        self.apis: dict[str, dict[str, Any]] = {}
        self.sid: str | None = None

    # -- context manager: always log out --------------------------------------
    def __enter__(self) -> SynologyClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.sid:
            try:
                self.logout()
            except (SynologyError, requests.RequestException):
                pass
        self.session.close()

    # -- discovery -------------------------------------------------------------
    def discover(self) -> dict[str, dict[str, Any]]:
        """SYNO.API.Info query: the only fixed endpoint; everything else is resolved from it."""
        resp = self.session.get(
            f"{self.base_url}/query.cgi",
            params={"api": "SYNO.API.Info", "version": 1, "method": "query", "query": "all"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("success"):
            raise SynologyError("SYNO.API.Info", "query", body.get("error", {}).get("code", 100))
        self.apis = body["data"]
        return self.apis

    def version(self, api: str) -> int:
        if not self.apis:
            self.discover()
        info = self.apis.get(api)
        if info is None:
            raise SynologyError(api, "-", 102)
        wanted = DOC_VERSIONS.get(api, info["maxVersion"])
        return max(info["minVersion"], min(wanted, info["maxVersion"]))

    def _url(self, api: str) -> str:
        if not self.apis:
            self.discover()
        return f"{self.base_url}/{self.apis[api]['path']}"

    # -- auth ------------------------------------------------------------------
    def login(self, account: str, passwd: str) -> str:
        # POST so the password never appears in a URL or access log.
        data = self.call(
            "SYNO.API.Auth",
            "login",
            account=account,
            passwd=passwd,
            session="FileStation",
            format="sid",
        )
        self.sid = data["sid"]
        return self.sid

    def logout(self) -> None:
        self.call("SYNO.API.Auth", "logout", session="FileStation")
        self.sid = None

    # -- generic call ----------------------------------------------------------
    def call(self, api: str, method: str, **params: Any) -> dict[str, Any]:
        form = {"api": api, "version": self.version(api), "method": method}
        form.update({k: encode(v) for k, v in params.items() if v is not None})
        if self.sid:
            form["_sid"] = self.sid
        resp = self.session.post(self._url(api), data=form, timeout=self.timeout)
        resp.raise_for_status()
        return self._unwrap(api, method, resp.json())

    @staticmethod
    def _unwrap(api: str, method: str, body: dict[str, Any]) -> dict[str, Any]:
        if body.get("success"):
            return body.get("data") or {}
        err = body.get("error", {})
        raise SynologyError(api, method, err.get("code", 100), err.get("errors"))

    # -- upload / download -----------------------------------------------------
    def upload(
        self,
        dest_folder: str,
        local_path: str | os.PathLike,
        *,
        overwrite: str | None = "overwrite",
        create_parents: bool = True,
    ) -> dict[str, Any]:
        """Multipart upload (RFC 1867). requests puts `data` fields first, the file part last.

        overwrite: "overwrite" | "skip" | None (None = omit; server errors 1805 on conflict).
        """
        api = "SYNO.FileStation.Upload"
        ver = self.version(api)
        query = {"api": api, "version": ver, "method": "upload", "_sid": self.sid}
        form = {"path": dest_folder, "create_parents": encode(create_parents)}
        if overwrite is not None:
            # v2 takes true/false, v3 takes overwrite/skip.
            form["overwrite"] = overwrite if ver >= 3 else encode(overwrite == "overwrite")
        local_path = Path(local_path)
        with local_path.open("rb") as fh:
            files = {"file": (local_path.name, fh, "application/octet-stream")}
            resp = self.session.post(
                self._url(api), params=query, data=form, files=files, timeout=self.timeout
            )
        resp.raise_for_status()
        return self._unwrap(api, "upload", resp.json())

    def download(self, path: str) -> bytes:
        api = "SYNO.FileStation.Download"
        params = {
            "api": api,
            "version": self.version(api),
            "method": "download",
            "path": encode([path]),
            "mode": "download",
            "_sid": self.sid,
        }
        resp = self.session.get(self._url(api), params=params, timeout=self.timeout, stream=True)
        resp.raise_for_status()
        # Errors come back as a JSON envelope instead of file bytes.
        if resp.headers.get("Content-Type", "").startswith("application/json"):
            self._unwrap(api, "download", resp.json())
        return resp.content

    # -- non-blocking tasks ----------------------------------------------------
    def wait_task(
        self,
        api: str,
        taskid: str,
        *,
        interval: float = 0.5,
        timeout: float = 60,
        on_progress: Any = None,
    ) -> dict[str, Any]:
        """Poll <api>.status until finished (CopyMove, Delete, ...)."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                status = self.call(api, "status", taskid=quoted(taskid))
            except SynologyError as e:
                if e.code == 599:  # task already completed and reaped
                    return {"finished": True, "note": "task no longer listed"}
                raise
            if on_progress:
                on_progress(status)
            if status.get("finished"):
                return status
            if time.monotonic() > deadline:
                raise TimeoutError(f"{api} task {taskid} not finished after {timeout}s")
            time.sleep(interval)
