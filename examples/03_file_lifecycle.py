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
from datetime import datetime

from synology_poc import FileStation, Settings, SynologyError, connect
from synology_poc.sandbox import delete_folder, guard, names_in, new_run_path, progress, step


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(fs: FileStation, sandbox: str, run_path: str) -> None:
    run_name = posixpath.basename(run_path)
    copies_path = guard(sandbox, f"{run_path}/copies")

    step(1, f"CreateFolder.create {run_path} and {copies_path}")
    made = fs.create_folder(sandbox, run_name)
    fs.create_folder(run_path, "copies", parents=True)
    print(f"    created: {[made.get('path')]} + copies/")

    step(2, "Upload.upload hello.txt")
    payload = f"Hello from the Synology PoC at {datetime.now().isoformat()}\n".encode() + os.urandom(64)
    original_hash = sha256(payload)
    fs.upload(run_path, payload, filename="hello.txt", overwrite="overwrite")
    size = names_in(fs.client, run_path)["hello.txt"]["additional"]["size"]
    print(f"    uploaded {len(payload)} bytes, NAS reports size={size}, sha256={original_hash[:16]}…")

    step(3, "Upload conflict handling (same file again)")
    result = fs.upload(run_path, payload, filename="hello.txt", overwrite="skip")
    print(f"    overwrite=skip -> OK {result or ''}".rstrip())
    try:
        fs.upload(run_path, payload, filename="hello.txt", overwrite=None)
        print("    no overwrite param -> succeeded (guide says 1805; this DSM build is lenient)")
    except SynologyError as e:
        print(f"    no overwrite param -> rejected as expected: {e}")

    step(4, "Rename.rename hello.txt -> hello-renamed.txt")
    src = guard(sandbox, fs.rename(f"{run_path}/hello.txt", "hello-renamed.txt")["path"])
    print(f"    now at {src}")

    step(5, f"CopyMove.start copy -> {copies_path} (async, polling status)")
    fs.copy_move(src, copies_path, overwrite=True, on_progress=progress)
    copied = f"{copies_path}/hello-renamed.txt"
    print(f"    copy finished: {copied}")

    step(6, f"Download.download {copied} and verify")
    data = fs.download(copied)
    downloaded_hash = sha256(data)
    match = downloaded_hash == original_hash
    print(f"    downloaded {len(data)} bytes, sha256={downloaded_hash[:16]}…")
    print(f"    round-trip integrity: {'MATCH' if match else 'MISMATCH'}")
    if not match:
        raise SystemExit("Downloaded content differs from the upload.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="skip cleanup")
    args = parser.parse_args()

    settings = Settings.from_env()
    sandbox = settings.sandbox
    run_path = new_run_path(sandbox)

    with connect(settings) as client:
        fs = FileStation(client)
        try:
            run(fs, sandbox, run_path)
            print("\nAll lifecycle steps passed.")
        except SynologyError as e:
            print(f"\nAPI error: {e}")
        finally:
            # Only ever clean up this run's folder, and only if it was created.
            if args.keep:
                print(f"\n--keep: left {run_path} in place for inspection.")
            elif posixpath.basename(run_path) in names_in(client, sandbox):
                step(7, f"Delete.start {run_path} (recursive, async)")
                delete_folder(client, sandbox, run_path)


if __name__ == "__main__":
    main()
