"""Example 4: non-blocking jobs (start -> poll -> result), each checked against local values.

DirSize -> MD5 -> Search (list/stop/clean) -> Compress -> Extract.list -> Extract.start
-> MD5 of the extracted copy -> BackgroundTask.list / clear_finished -> Delete.

All writes happen under <SYNO_SANDBOX>/poc-run-<timestamp>. Pass --keep to skip cleanup.
"""

import argparse
import hashlib
import os
import posixpath

from synology_poc import FileStation, Settings, SynologyError, connect
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


def make_contents() -> dict[str, bytes]:
    contents = {}
    for name, size in LOCAL_FILES.items():
        if name.endswith(".txt"):
            line = f"{name}: Synology PoC sample line\n".encode()
            contents[name] = (line * (size // len(line) + 1))[:size]
        else:
            contents[name] = os.urandom(size)
    return contents


def run(fs: FileStation, sandbox: str, run_path: str) -> None:
    step(0, f"Setup: create {run_path} and upload {len(LOCAL_FILES)} generated files")
    fs.create_folder(sandbox, posixpath.basename(run_path))
    contents = make_contents()
    for name, data in contents.items():
        remote_dir = guard(sandbox, posixpath.dirname(f"{run_path}/{name}"))
        fs.upload(remote_dir, data, filename=posixpath.basename(name), create_parents=True)
    print(f"    uploaded: {', '.join(LOCAL_FILES)}")

    step(1, "DirSize.start -> status (file count and total bytes)")
    size = fs.dir_size(run_path)
    check("num_file", len(LOCAL_FILES), size["num_file"])
    check("total_size", sum(len(d) for d in contents.values()), size["total_size"])
    check("num_dir", 1, size["num_dir"])  # DSM 7 doesn't count the root folder itself

    step(2, "MD5.start -> status on data.bin")
    local_md5 = hashlib.md5(contents["data.bin"]).hexdigest()
    check("md5", local_md5, fs.md5(f"{run_path}/data.bin"))

    step(3, "Search.start pattern=*.txt (recursive) -> poll list -> stop + clean")
    found = fs.search(run_path, pattern="*.txt")
    hits = sorted(posixpath.relpath(f["path"], run_path) for f in found["files"])
    check("matches", sorted(n for n in LOCAL_FILES if n.endswith(".txt")), hits)
    print("    search task stopped and its temporary database cleaned")

    archive = guard(sandbox, f"{run_path}/archive.zip")
    step(4, f"Compress.start -> {posixpath.basename(archive)}")
    sources = [f"{run_path}/{n}" for n in LOCAL_FILES if "/" not in n] + [f"{run_path}/docs"]
    compress_task = fs.compress(sources, archive, on_progress=progress)
    zipped = names_in(fs.client, run_path)["archive.zip"]["additional"]["size"]
    print(f"    archive.zip = {zipped:,} bytes (sources {sum(len(d) for d in contents.values()):,})")

    step(5, "Extract.list: what's inside the archive (one level per call, so recurse into folders)")
    for it in fs.list_archive(archive):
        kind = "dir " if it.get("is_dir") else "file"
        print(f"    {'  ' * it['depth']}#{it['item_id']} {kind} {it.get('path') or '-':<20} "
              f"size={it.get('size', '-')!s:>8} packed={it.get('pack_size', '-')!s:>8}")

    extracted = guard(sandbox, f"{run_path}/extracted")
    step(6, f"Extract.start -> {posixpath.basename(extracted)}/ and verify data.bin")
    fs.create_folder(run_path, "extracted")
    extract_task = fs.extract(archive, extracted, overwrite=True, on_progress=progress)
    check("md5 after zip", local_md5, fs.md5(f"{extracted}/data.bin"))
    check("docs/ kept", True, "report.txt" in names_in(fs.client, f"{extracted}/docs"))

    step(7, "BackgroundTask.list -> clear_finished (only this run's tasks)")
    task_ids = {compress_task["taskid"], extract_task["taskid"]}
    tasks = fs.background_tasks(["SYNO.FileStation.Compress", "SYNO.FileStation.Extract"])
    ours = [t for t in tasks["tasks"] if t.get("taskid") in task_ids]
    for t in ours:
        print(f"    {t.get('api'):<28} {t.get('taskid')}  finished={t.get('finished')}")
    print(f"    {len(ours)} of {tasks['total']} listed task(s) are from this run")
    if ours:
        fs.clear_finished_tasks(t["taskid"] for t in ours)
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

    with connect(settings) as client:
        fs = FileStation(client)
        try:
            run(fs, sandbox, run_path)
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
