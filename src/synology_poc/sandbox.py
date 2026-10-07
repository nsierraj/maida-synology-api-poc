"""Helpers shared by the examples that write to the NAS.

Every write goes through guard(), so nothing outside SYNO_SANDBOX can be touched.
"""

from __future__ import annotations

import posixpath
from datetime import datetime

from .client import SynologyClient
from .policy import PathPolicy, PolicyError


def guard(sandbox: str, path: str) -> str:
    """Refuse any remote path that is not strictly inside the sandbox share."""
    try:
        return PathPolicy((sandbox,), allow_writes=True).check_write(path)
    except PolicyError as e:
        raise SystemExit(f"Refusing to touch {path!r}: {e}") from None


def new_run_path(sandbox: str, prefix: str = "poc-run") -> str:
    return guard(sandbox, f"{sandbox}/{prefix}-{datetime.now():%Y%m%d-%H%M%S}")


def step(n: int, text: str) -> None:
    print(f"\n[{n}] {text}")


def names_in(client: SynologyClient, folder: str) -> dict[str, dict]:
    """All entries in a folder by name (system folders included)."""
    from .filestation import FileStation

    listing = FileStation(client).list_folder(folder, additional=["size"], include_system=True)
    return {f["name"]: f for f in listing["files"]}


def progress(status: dict) -> None:
    pct = status.get("progress")
    if pct is not None:
        print(f"    … {pct * 100:5.1f}%  finished={status.get('finished')}")


def delete_folder(client: SynologyClient, sandbox: str, path: str) -> bool:
    """Async recursive Delete, then confirm the folder is no longer listed."""
    from .filestation import FileStation

    FileStation(client).delete(guard(sandbox, path), on_progress=progress)
    parent = posixpath.dirname(path)
    gone = posixpath.basename(path) not in names_in(client, parent)
    print(f"    removed from {parent}: {'yes' if gone else 'NO, still listed'}")
    return gone
