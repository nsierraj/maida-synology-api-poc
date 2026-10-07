"""Example 3: full file lifecycle inside a throwaway sandbox folder.

CreateFolder -> Upload -> Upload conflict handling -> Rename -> CopyMove (async)
-> Download + SHA-256 round-trip check -> Delete (async) -> confirm gone.

All writes happen under <SYNO_SANDBOX>/poc-run-<timestamp>. Pass --keep to skip cleanup
and inspect the folder in DSM File Station.
"""

import argparse
import hashlib
import os
import posixpath
import tempfile
from datetime import datetime
from pathlib import Path

from synology_poc import Settings, SynologyClient, SynologyError, connect


def guard(sandbox: str, path: str) -> str:
    """Refuse any remote path that is not strictly inside the sandbox share."""
    norm = posixpath.normpath(path)
    if not norm.startswith(sandbox.rstrip("/") + "/"):
        raise SystemExit(f"Refusing to touch {path!r}: outside sandbox {sandbox!r}")
    return norm


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def step(n: int, text: str) -> None:
    print(f"\n[{n}] {text}")


def names_in(client: SynologyClient, folder: str) -> dict[str, dict]:
    listing = client.call(
        "SYNO.FileStation.List", "list", folder_path=folder, additional=["size"]
    )
    return {f["name"]: f for f in listing.get("files", [])}


def progress(status: dict) -> None:
    pct = status.get("progress")
    if pct is not None:
        print(f"    … {pct * 100:5.1f}%  finished={status.get('finished')}")


def run(client: SynologyClient, sandbox: str, run_path: str, workdir: Path) -> None:
    run_name = posixpath.basename(run_path)
    copies_path = guard(sandbox, f"{run_path}/copies")

    step(1, f"CreateFolder.create {run_path} and {copies_path}")
    made = client.call("SYNO.FileStation.CreateFolder", "create",
                       folder_path=[sandbox], name=[run_name])
    client.call("SYNO.FileStation.CreateFolder", "create",
                folder_path=[run_path], name=["copies"], force_parent=True)
    print(f"    created: {[f['path'] for f in made.get('folders', [])]} + copies/")

    step(2, "Upload.upload hello.txt")
    local = workdir / "hello.txt"
    payload = f"Hello from the Synology PoC at {datetime.now().isoformat()}\n".encode() + os.urandom(64)
    local.write_bytes(payload)
    original_hash = sha256(payload)
    client.upload(run_path, local, overwrite="overwrite")
    listed = names_in(client, run_path)
    size = listed["hello.txt"]["additional"]["size"]
    print(f"    uploaded {len(payload)} bytes, NAS reports size={size}, sha256={original_hash[:16]}…")

    step(3, "Upload conflict handling (same file again)")
    result = client.upload(run_path, local, overwrite="skip")
    print(f"    overwrite=skip -> OK {result or ''}".rstrip())
    try:
        client.upload(run_path, local, overwrite=None)
        print("    no overwrite param -> succeeded (guide says 1805; this DSM build is lenient)")
    except SynologyError as e:
        print(f"    no overwrite param -> rejected as expected: {e}")

    step(4, "Rename.rename hello.txt -> hello-renamed.txt")
    renamed = client.call("SYNO.FileStation.Rename", "rename",
                          path=[f"{run_path}/hello.txt"], name=["hello-renamed.txt"])
    src = guard(sandbox, renamed["files"][0]["path"])
    print(f"    now at {src}")

    step(5, f"CopyMove.start copy -> {copies_path} (async, polling status)")
    task = client.call("SYNO.FileStation.CopyMove", "start",
                       path=[src], dest_folder_path=copies_path,
                       overwrite=True, remove_src=False)
    client.wait_task("SYNO.FileStation.CopyMove", task["taskid"], on_progress=progress)
    copied = f"{copies_path}/hello-renamed.txt"
    print(f"    copy finished: {copied}")

    step(6, f"Download.download {copied} and verify")
    data = client.download(copied)
    downloaded_hash = sha256(data)
    match = downloaded_hash == original_hash
    print(f"    downloaded {len(data)} bytes, sha256={downloaded_hash[:16]}…")
    print(f"    round-trip integrity: {'MATCH' if match else 'MISMATCH'}")
    if not match:
        raise SystemExit("Downloaded content differs from the upload.")


def cleanup(client: SynologyClient, sandbox: str, run_path: str) -> None:
    step(7, f"Delete.start {run_path} (recursive, async)")
    task = client.call("SYNO.FileStation.Delete", "start",
                       path=[guard(sandbox, run_path)], recursive=True, accurate_progress=True)
    client.wait_task("SYNO.FileStation.Delete", task["taskid"], on_progress=progress)
    gone = posixpath.basename(run_path) not in names_in(client, sandbox)
    print(f"    removed from {sandbox}: {'yes' if gone else 'NO, still listed'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="skip cleanup")
    args = parser.parse_args()

    settings = Settings.from_env()
    sandbox = settings.sandbox
    run_path = guard(sandbox, f"{sandbox}/poc-run-{datetime.now():%Y%m%d-%H%M%S}")

    with connect(settings) as client, tempfile.TemporaryDirectory() as tmp:
        try:
            run(client, sandbox, run_path, Path(tmp))
            print("\nAll lifecycle steps passed.")
        except SynologyError as e:
            print(f"\nAPI error: {e}")
        finally:
            # Only ever clean up this run's folder, and only if it was created.
            if args.keep:
                print(f"\n--keep: left {run_path} in place for inspection.")
            elif posixpath.basename(run_path) in names_in(client, sandbox):
                cleanup(client, sandbox, run_path)


if __name__ == "__main__":
    main()
