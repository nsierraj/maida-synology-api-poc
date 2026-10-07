"""Example 5: thumbnails and sharing links.

Default run: upload a generated PNG -> Thumb.get (small/medium/large + rotated, saved to out/)
-> Sharing.create (password) -> getinfo -> edit (expires tomorrow) -> list.
The link and its file are LEFT ACTIVE so you can open it in a browser.

--cleanup: delete every link under <SYNO_SANDBOX>/poc-share-*, confirm each is gone (2000),
then delete the poc-share-* folders.
"""

import argparse
import base64
import posixpath
import secrets
import struct
import sys
import tempfile
import time
import zlib
from datetime import date, timedelta
from pathlib import Path

from synology_poc import Settings, SynologyClient, SynologyError, connect
from synology_poc.client import quoted
from synology_poc.sandbox import delete_folder, guard, names_in, new_run_path, step

OUT = Path(__file__).resolve().parent.parent / "out"
SHARE_PREFIX = "poc-share"


# -- local image helpers (stdlib only) ------------------------------------------
def write_png(path: Path, width: int = 640, height: int = 480) -> None:
    """A colour gradient, so thumbnails and rotation are easy to tell apart."""
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # filter type: none
        for x in range(width):
            rows += bytes((x * 255 // width, y * 255 // height, 160))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a PNG or JPEG header."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            length = struct.unpack(">H", data[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + length
    return None


def ext_for(content_type: str) -> str:
    return {"image/png": "png", "image/gif": "gif"}.get(content_type.split(";")[0], "jpg")


# -- API steps ------------------------------------------------------------------
def get_thumb(client: SynologyClient, path: str, **params: object) -> tuple[bytes, str]:
    """Thumb.get, retrying briefly in case DSM generates thumbnails lazily (HTTP 404)."""
    deadline = time.monotonic() + 5
    while True:
        try:
            return client.fetch_binary("SYNO.FileStation.Thumb", "get", path=path, **params)
        except SynologyError as e:
            if not (e.http_status and e.code == 404) or time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def show_link(link: dict) -> None:
    print(f"    status={link.get('status')}  has_password={link.get('has_password')}  "
          f"date_expired={link.get('date_expired')}  owner={link.get('link_owner')}")


def demo(client: SynologyClient, sandbox: str, workdir: Path) -> None:
    share_path = new_run_path(sandbox, SHARE_PREFIX)
    image = guard(sandbox, f"{share_path}/sample.png")

    step(1, f"Upload a generated 640x480 PNG to {share_path}")
    client.call("SYNO.FileStation.CreateFolder", "create",
                folder_path=[sandbox], name=[posixpath.basename(share_path)])
    local = workdir / "sample.png"
    write_png(local)
    client.upload(share_path, local, overwrite="overwrite")
    print(f"    uploaded sample.png ({local.stat().st_size:,} bytes)")

    step(2, "Thumb.get: small / medium / large, plus medium rotated 90°")
    thumbs_dir = OUT / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    variants = [("small", {}), ("medium", {}), ("large", {}), ("medium-rot90", {"rotate": 1})]
    for label, extra in variants:
        size = label.split("-")[0]
        data, ctype = get_thumb(client, image, size=size, **extra)
        target = thumbs_dir / f"sample-{label}.{ext_for(ctype)}"
        target.write_bytes(data)
        dims = image_size(data)
        dims_text = f"{dims[0]}x{dims[1]}" if dims else "?"
        print(f"    {label:<13} {ctype:<11} {dims_text:>9}  {len(data):>7,} bytes -> {target.relative_to(OUT.parent)}")

    step(3, "Sharing.create (password-protected, no expiry yet)")
    password = secrets.token_urlsafe(9)[:12]
    try:
        created = client.call("SYNO.FileStation.Sharing", "create", path=[image], password=password)
    except SynologyError as e:
        sys.exit(f"    {e}\n    Hint: the test user may lack the sharing permission "
                 "(DSM File Station → Settings → sharing permissions).")
    link = created["links"][0]
    if link.get("error"):
        sys.exit(f"    link creation failed with code {link['error']} "
                 "(check the user's sharing permission in File Station settings)")
    qr = link.get("qrcode", "")
    if qr:
        qr_path = OUT / "share-qr.png"
        qr_path.write_bytes(base64.b64decode(qr.split(",", 1)[-1]))
        print(f"    QR code saved to {qr_path.relative_to(OUT.parent)}")
    print(f"    id={link['id']}  url={link['url']}")

    step(4, "Sharing.getinfo")
    show_link(client.call("SYNO.FileStation.Sharing", "getinfo", id=quoted(link["id"])))

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    step(5, f"Sharing.edit -> expire on {tomorrow}, then getinfo again")
    client.call("SYNO.FileStation.Sharing", "edit", id=[link["id"]], date_expired=quoted(tomorrow))
    show_link(client.call("SYNO.FileStation.Sharing", "getinfo", id=quoted(link["id"])))

    step(6, "Sharing.list: this user's links")
    for item in client.call("SYNO.FileStation.Sharing", "list").get("links", []):
        print(f"    {item.get('id')}  {item.get('status'):<8} {item.get('path')}")

    print("\nLink left ACTIVE. Open it in a browser:")
    print(f"    URL      : {link['url']}")
    print(f"    password : {password}")
    print(f"    expires  : {tomorrow}")
    print("When done: uv run examples/05_sharing_and_thumbs.py --cleanup")


def cleanup(client: SynologyClient, sandbox: str) -> None:
    prefix = f"{sandbox}/{SHARE_PREFIX}-"

    step(1, "Sharing.list -> delete links under poc-share-*")
    links = client.call("SYNO.FileStation.Sharing", "list").get("links", [])
    ours = [link for link in links if link.get("path", "").startswith(prefix)]
    if ours:
        client.call("SYNO.FileStation.Sharing", "delete", id=[link["id"] for link in ours])
    for link in ours:
        try:
            client.call("SYNO.FileStation.Sharing", "getinfo", id=quoted(link["id"]))
            print(f"    {link['id']}: still exists")
        except SynologyError as e:
            print(f"    {link['id']}: deleted -> getinfo says {e.code} ({e.message})")
    if not ours:
        print("    no PoC links found")

    step(2, "Delete poc-share-* folders")
    folders = [n for n in names_in(client, sandbox) if n.startswith(f"{SHARE_PREFIX}-")]
    for name in folders:
        delete_folder(client, sandbox, f"{sandbox}/{name}")
    if not folders:
        print("    no poc-share-* folders found")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cleanup", action="store_true",
                        help="delete PoC share links and poc-share-* folders")
    args = parser.parse_args()

    settings = Settings.from_env()
    with connect(settings) as client, tempfile.TemporaryDirectory() as tmp:
        try:
            if args.cleanup:
                cleanup(client, settings.sandbox)
            else:
                demo(client, settings.sandbox, Path(tmp))
        except SynologyError as e:
            print(f"\nAPI error: {e}")


if __name__ == "__main__":
    main()
