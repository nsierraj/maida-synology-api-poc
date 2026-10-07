"""Example 2: read-only browsing. Changes nothing on the NAS.

List.list_share -> List.list (sandbox) -> List.getinfo -> a denied path (error handling).
"""

import json
from datetime import datetime

from synology_poc import Settings, SynologyError, connect


def human(n: int | None) -> str:
    if n is None:
        return "-"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return str(n)


def ts(epoch: int | None) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M") if epoch else "-"


def main() -> None:
    settings = Settings.from_env()
    with connect(settings) as client:
        print("[1] List.list_share: shared folders visible to this user")
        shares = client.call(
            "SYNO.FileStation.List",
            "list_share",
            additional=["real_path", "owner", "time", "volume_status"],
        )
        for s in shares.get("shares", []):
            add = s.get("additional", {})
            vol = add.get("volume_status", {})
            print(
                f"    {s['path']:<20} real={add.get('real_path', '-'):<24} "
                f"owner={add.get('owner', {}).get('user', '-'):<10} "
                f"free={human(vol.get('freespace'))} / {human(vol.get('totalspace'))}"
            )
        print(f"    total: {shares.get('total')}")

        print(f"\n[2] List.list: newest 50 entries in {settings.sandbox}")
        listing = client.call(
            "SYNO.FileStation.List",
            "list",
            folder_path=settings.sandbox,
            limit=50,
            sort_by="mtime",
            sort_direction="desc",
            additional=["size", "time", "type"],
        )
        files = listing.get("files", [])
        if not files:
            print("    (empty: drop a file into the share via DSM to see something here)")
        for f in files:
            add = f.get("additional", {})
            kind = "dir " if f["isdir"] else "file"
            size = "-" if f["isdir"] else human(add.get("size"))
            print(f"    {kind} {f['name']:<36} {size:>10}  {ts(add.get('time', {}).get('mtime'))}")
        print(f"    total: {listing.get('total')}")

        if files:
            first = files[0]["path"]
            print(f"\n[3] List.getinfo: {first}")
            detail = client.call(
                "SYNO.FileStation.List",
                "getinfo",
                path=[first],
                additional=["real_path", "owner", "perm"],
            )
            print(json.dumps(detail.get("files", [])[0], indent=2))

        print(f"\n[4] List.list on a path this user should not read: {settings.denied_path}")
        try:
            client.call("SYNO.FileStation.List", "list", folder_path=settings.denied_path, limit=1)
            print("    Unexpected: the listing succeeded. Check the test user's permissions.")
        except SynologyError as e:
            print(f"    Denied as expected -> {e}")


if __name__ == "__main__":
    main()
