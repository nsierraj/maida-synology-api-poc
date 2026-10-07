"""Example 4: non-blocking jobs (start -> poll -> result), each checked against local values.

DirSize -> MD5 -> Search (list/stop/clean) -> Compress -> Extract.list -> Extract.start
-> MD5 of the extracted copy -> BackgroundTask.list / clear_finished -> Delete.

All writes happen under <SYNO_SANDBOX>/poc-run-<timestamp>. Pass --keep to skip cleanup.
"""

import argparse
import hashlib
import os
import posixpath
import tempfile
from pathlib import Path

from synology_poc import Settings, SynologyClient, SynologyError, connect
from synology_poc.client import quoted
from synology_poc.sandbox import delete_folder, guard, names_in, new_run_path, progress, step

# name -> size in bytes; "docs/report.txt" goes into a subfolder so recursion is exercised.
LOCAL_FILES = {
    "notes-a.txt": 2_000,
    "notes-b.txt": 5_000,
    "data.bin": 200_000,
    "docs/report.txt": 1_000,
}


def check(label: str, expected: object, observed: object) -> bool:
    ok = expected == observed
    print(f"    {label:<14} expected={expected!s:<34} observed={observed!s:<34} {'MATCH' if ok else 'DIFF'}")
    return ok


def make_files(workdir: Path) -> dict[str, bytes]:
    contents = {}
    for name, size in LOCAL_FILES.items():
        if name.endswith(".txt"):
            line = f"{name}: Synology PoC sample line\n".encode()
            data = (line * (size // len(line) + 1))[:size]
        else:
            data = os.urandom(size)
        path = workdir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        contents[name] = data
    return contents


def remote_md5(client: SynologyClient, path: str) -> str:
    task = client.call("SYNO.FileStation.MD5", "start", file_path=path)
    status = client.wait_task("SYNO.FileStation.MD5", task["taskid"])
    if "md5" not in status:
        raise SystemExit(f"MD5 task for {path} finished without a checksum: {status}")
    return status["md5"]


def list_archive(client: SynologyClient, archive: str, item_id: int | None = None,
                 depth: int = 0, seen: set | None = None) -> None:
    """Extract.list returns a single folder level; pass a folder's item_id to list inside it."""
    seen = set() if seen is None else seen
    items = client.call("SYNO.FileStation.Extract", "list", file_path=quoted(archive),
                        limit=-1, codepage=quoted("enu"), item_id=item_id)
    for it in sorted(items.get("items", []), key=lambda i: (not i.get("is_dir"), i.get("path") or "")):
        if it.get("name") == "..":  # DSM adds a parent-folder entry (item_id -1) inside subfolders
            continue
        iid = it.get("item_id", it.get("itemid"))
        kind = "dir " if it.get("is_dir") else "file"
        size, packed = it.get("size", "-"), it.get("pack_size", "-")
        print(f"    {'  ' * depth}#{iid} {kind} {it.get('path') or '-':<20} size={size!s:>8} packed={packed!s:>8}")
        if "size" not in it or "path" not in it:
            print(f"    {'  ' * depth}   (unexpected item shape: {it})")
        # Guard against DSM echoing the folder itself or a cycle of item IDs.
        if it.get("is_dir") and iid not in seen and iid != item_id and depth < 4:
            seen.add(iid)
            list_archive(client, archive, iid, depth + 1, seen)


def run(client: SynologyClient, sandbox: str, run_path: str, workdir: Path) -> None:
    run_name = posixpath.basename(run_path)
    task_ids: list[str] = []

    step(0, f"Setup: create {run_path} and upload {len(LOCAL_FILES)} generated files")
    client.call("SYNO.FileStation.CreateFolder", "create", folder_path=[sandbox], name=[run_name])
    contents = make_files(workdir)
    for name in LOCAL_FILES:
        remote_dir = guard(sandbox, posixpath.dirname(f"{run_path}/{name}"))
        client.upload(remote_dir, workdir / name, overwrite="overwrite", create_parents=True)
    print(f"    uploaded: {', '.join(LOCAL_FILES)}")

    step(1, "DirSize.start -> status (file count and total bytes)")
    task = client.call("SYNO.FileStation.DirSize", "start", path=[run_path])
    size = client.wait_task("SYNO.FileStation.DirSize", task["taskid"])
    check("num_file", len(LOCAL_FILES), size.get("num_file"))
    check("total_size", sum(len(d) for d in contents.values()), size.get("total_size"))
    check("num_dir", 1, size.get("num_dir"))  # DSM 7 doesn't count the root folder itself

    step(2, "MD5.start -> status on data.bin")
    local_md5 = hashlib.md5(contents["data.bin"]).hexdigest()
    check("md5", local_md5, remote_md5(client, f"{run_path}/data.bin"))

    step(3, "Search.start pattern=*.txt (recursive) -> poll list -> stop + clean")
    search = client.call("SYNO.FileStation.Search", "start",
                         folder_path=[run_path], pattern="*.txt", recursive=True)
    sid = search["taskid"]
    try:
        client.wait_task("SYNO.FileStation.Search", sid, method="list", limit=0)
        found = client.call("SYNO.FileStation.Search", "list",
                            taskid=quoted(sid), limit=-1, additional=["size"])
        hits = sorted(posixpath.relpath(f["path"], run_path) for f in found.get("files", []))
        check("matches", sorted(n for n in LOCAL_FILES if n.endswith(".txt")), hits)
    finally:
        client.call("SYNO.FileStation.Search", "stop", taskid=quoted(sid))
        client.call("SYNO.FileStation.Search", "clean", taskid=quoted(sid))
        print("    search task stopped and its temporary database cleaned")

    archive = f"{run_path}/archive.zip"
    step(4, f"Compress.start -> {posixpath.basename(archive)}")
    sources = [f"{run_path}/{n}" for n in LOCAL_FILES if "/" not in n] + [f"{run_path}/docs"]
    # Mirrors the DSM 7 web UI's request: every string JSON-quoted, level "normal" and
    # mode "replace" (neither is in the guide), password=null. The guide's form got 105.
    task = client.call("SYNO.FileStation.Compress", "start",
                       path=sources, dest_file_path=quoted(guard(sandbox, archive)),
                       level=quoted("normal"), mode=quoted("replace"), format=quoted("zip"),
                       password="null", codepage=quoted("enu"))
    task_ids.append(task["taskid"])
    client.wait_task("SYNO.FileStation.Compress", task["taskid"], on_progress=progress)
    zipped = names_in(client, run_path)["archive.zip"]["additional"]["size"]
    print(f"    archive.zip = {zipped:,} bytes (sources {sum(len(d) for d in contents.values()):,})")

    step(5, "Extract.list: what's inside the archive (one level per call, so recurse into folders)")
    list_archive(client, archive)

    extracted = guard(sandbox, f"{run_path}/extracted")
    step(6, f"Extract.start -> {posixpath.basename(extracted)}/ and verify data.bin")
    client.call("SYNO.FileStation.CreateFolder", "create", folder_path=[run_path], name=["extracted"])
    task = client.call("SYNO.FileStation.Extract", "start",
                       file_path=quoted(archive), dest_folder_path=quoted(extracted),
                       keep_dir=True, create_subfolder=False, overwrite=True,
                       codepage=quoted("enu"))
    task_ids.append(task["taskid"])
    client.wait_task("SYNO.FileStation.Extract", task["taskid"], on_progress=progress)
    check("md5 after zip", local_md5, remote_md5(client, f"{extracted}/data.bin"))
    check("docs/ kept", True, "report.txt" in names_in(client, f"{extracted}/docs"))

    step(7, "BackgroundTask.list -> clear_finished (only this run's tasks)")
    tasks = client.call("SYNO.FileStation.BackgroundTask", "list",
                        api_filter=["SYNO.FileStation.Compress", "SYNO.FileStation.Extract"])
    ours = [t for t in tasks.get("tasks", []) if t.get("taskid") in task_ids]
    for t in ours:
        print(f"    {t.get('api'):<28} {t.get('taskid')}  finished={t.get('finished')}")
    print(f"    {len(ours)} of {tasks.get('total', len(tasks.get('tasks', [])))} listed task(s) are from this run")
    if ours:
        client.call("SYNO.FileStation.BackgroundTask", "clear_finished",
                    taskid=[t["taskid"] for t in ours])
        print("    cleared this run's finished tasks")
    else:
        print("    nothing to clear (DSM had already dropped this run's finished tasks)")

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="skip cleanup")
    args = parser.parse_args()

    settings = Settings.from_env()
    sandbox = settings.sandbox
    run_path = new_run_path(sandbox)

    with connect(settings) as client, tempfile.TemporaryDirectory() as tmp:
        try:
            run(client, sandbox, run_path, Path(tmp))
            print("\nAll async-task steps completed.")
        except SynologyError as e:
            print(f"\nAPI error: {e}")
        finally:
            if args.keep:
                print(f"\n--keep: left {run_path} in place for inspection.")
            elif posixpath.basename(run_path) in names_in(client, sandbox):
                step(8, f"Delete {run_path}")
                delete_folder(client, sandbox, run_path)


if __name__ == "__main__":
    main()
