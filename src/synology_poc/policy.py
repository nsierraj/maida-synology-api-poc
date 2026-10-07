"""Path and capability policy for anything that changes the NAS.

Reads are limited only by the DSM account's own permissions. Writes must be explicitly
enabled and must stay inside the allowed roots. Share links are a separate opt-in because,
with QuickConnect, they are reachable from the internet (gofile.me).
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field


class PolicyError(PermissionError):
    """An operation was refused by the local policy (not by the NAS)."""


def normalize(path: str) -> str:
    """Absolute, normalized NAS path ('/share/a/../b' -> '/share/b')."""
    if not path or not path.startswith("/"):
        raise PolicyError(f"NAS paths must be absolute and start with a shared folder: {path!r}")
    return posixpath.normpath(path)


@dataclass(frozen=True)
class PathPolicy:
    roots: tuple[str, ...] = field(default_factory=tuple)
    allow_writes: bool = False
    allow_sharing: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "roots", tuple(normalize(r).rstrip("/") or "/" for r in self.roots))

    def is_inside_roots(self, path: str) -> bool:
        norm = normalize(path)
        return any(norm.startswith(root + "/") for root in self.roots)

    def check_write(self, path: str) -> str:
        """Return the normalized path if writing to it is allowed, else raise PolicyError.

        The path must be strictly *inside* a root: a root itself (e.g. the shared folder)
        can't be deleted or renamed.
        """
        if not self.allow_writes:
            raise PolicyError("Write operations are disabled (set SYNO_MCP_ALLOW_WRITES=true).")
        norm = normalize(path)
        if not self.is_inside_roots(norm):
            roots = ", ".join(self.roots) or "(none configured)"
            raise PolicyError(f"{path!r} is outside the allowed roots: {roots}")
        return norm

    def check_write_parent(self, folder: str) -> str:
        """For targets that are folders receiving new items (upload, create, extract):
        the folder may itself be a root."""
        if not self.allow_writes:
            raise PolicyError("Write operations are disabled (set SYNO_MCP_ALLOW_WRITES=true).")
        norm = normalize(folder)
        if norm in self.roots or self.is_inside_roots(norm):
            return norm
        roots = ", ".join(self.roots) or "(none configured)"
        raise PolicyError(f"{folder!r} is outside the allowed roots: {roots}")

    def check_sharing(self, path: str) -> str:
        if not self.allow_sharing:
            raise PolicyError(
                "Share links are disabled (set SYNO_MCP_ALLOW_SHARING=true). With QuickConnect "
                "they are reachable from the internet."
            )
        norm = normalize(path)
        if not self.is_inside_roots(norm):
            roots = ", ".join(self.roots) or "(none configured)"
            raise PolicyError(f"{path!r} is outside the allowed roots: {roots}")
        return norm
