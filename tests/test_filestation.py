"""One test (or more) per use case in docs/use-cases.md, against the fake DSM 7 NAS."""

import hashlib
from datetime import date, timedelta

import pytest

from synology_poc import FileStation, SynologyClient, SynologyError

from .fake_nas import PASSWORD


# UC-02 / UC-03
def test_info_and_shares(fs):
    assert fs.info()["is_manager"] is False
    assert [s["path"] for s in fs.list_shares()] == ["/home", "/poc-sandbox"]


# UC-04
def test_list_folder_hides_system_folders(fs, run_dir):
    listing = fs.list_folder("/poc-sandbox")
    assert [f["name"] for f in listing["files"]] == ["run"]
    assert listing["hidden_system"] == 1 and listing["total"] == 2
    with_system = fs.list_folder("/poc-sandbox", include_system=True)
    assert {f["name"] for f in with_system["files"]} == {"#recycle", "run"}


def test_list_folder_denied_and_missing(fs):
    with pytest.raises(SynologyError) as denied:
        fs.list_folder("/homes")
    assert denied.value.code == 407
    with pytest.raises(SynologyError) as missing:
        fs.list_folder("/poc-sandbox/nope")
    assert missing.value.code == 408


# UC-05
def test_get_info(fs, run_dir):
    fs.upload(run_dir, b"hello", filename="a.txt")
    (info,) = fs.get_info(f"{run_dir}/a.txt")
    assert info["additional"]["size"] == 5 and info["isdir"] is False


# UC-06 / UC-07 / UC-08
def test_create_upload_download_roundtrip(fs, run_dir):
    sub = fs.create_folder(run_dir, "sub")
    assert sub["path"] == f"{run_dir}/sub"
    fs.upload(f"{run_dir}/sub", b"payload", filename="p.bin")
    assert fs.download(f"{run_dir}/sub/p.bin") == b"payload"


def test_upload_conflict_modes(fs, run_dir):
    fs.upload(run_dir, b"v1", filename="c.txt")
    assert fs.upload(run_dir, b"v2", filename="c.txt", overwrite="skip")["blSkip"] is True
    with pytest.raises(SynologyError) as exc:
        fs.upload(run_dir, b"v3", filename="c.txt", overwrite=None)
    assert exc.value.code == 414  # DSM 7 says 414, the guide says 1805
    fs.upload(run_dir, b"v4", filename="c.txt", overwrite="overwrite")
    assert fs.download(f"{run_dir}/c.txt") == b"v4"


def test_upload_from_local_path(fs, run_dir, tmp_path):
    local = tmp_path / "local.txt"
    local.write_bytes(b"from disk")
    fs.upload(run_dir, local)
    assert fs.download(f"{run_dir}/local.txt") == b"from disk"


# UC-09 / UC-10 / UC-11
def test_rename_copy_move_delete(fs, run_dir):
    fs.upload(run_dir, b"x", filename="a.txt")
    renamed = fs.rename(f"{run_dir}/a.txt", "b.txt")
    assert renamed["path"] == f"{run_dir}/b.txt"
    fs.create_folder(run_dir, "copies")
    status = fs.copy_move(f"{run_dir}/b.txt", f"{run_dir}/copies")
    assert status["finished"] and status["taskid"]
    assert fs.download(f"{run_dir}/copies/b.txt") == b"x"
    fs.create_folder(run_dir, "moved")
    fs.copy_move(f"{run_dir}/b.txt", f"{run_dir}/moved", move=True)
    names = {f["name"] for f in fs.list_folder(run_dir)["files"]}
    assert "b.txt" not in names
    fs.delete(run_dir)
    assert "run" not in {f["name"] for f in fs.list_folder("/poc-sandbox")["files"]}


def test_progress_callback_sees_intermediate_status(fs, run_dir):
    fs.upload(run_dir, b"x", filename="a.txt")
    seen = []
    fs.delete(f"{run_dir}/a.txt", on_progress=seen.append)
    assert [s["finished"] for s in seen] == [False, True]


# UC-12 / UC-13
def test_dir_size_and_md5(fs, run_dir):
    fs.upload(run_dir, b"a" * 10, filename="a.txt")
    fs.upload(f"{run_dir}/docs", b"b" * 5, filename="b.txt")
    assert fs.dir_size(run_dir) == {"num_file": 2, "num_dir": 1, "total_size": 15}
    assert fs.md5(f"{run_dir}/a.txt") == hashlib.md5(b"a" * 10).hexdigest()


# UC-14
def test_search_is_recursive_and_cleans_up(nas, fs, run_dir):
    for name in ("x.txt", "y.bin"):
        fs.upload(run_dir, b"1", filename=name)
    fs.upload(f"{run_dir}/docs", b"2", filename="z.txt")
    result = fs.search(run_dir, pattern="*.txt")
    assert sorted(f["path"] for f in result["files"]) == [f"{run_dir}/docs/z.txt", f"{run_dir}/x.txt"]
    assert ("SYNO.FileStation.Search", "stop") in nas.calls
    assert ("SYNO.FileStation.Search", "clean") in nas.calls


# UC-15 / UC-16 / UC-17
def test_compress_list_extract_roundtrip(fs, run_dir):
    data = bytes(range(256)) * 100
    fs.upload(run_dir, data, filename="data.bin")
    fs.upload(f"{run_dir}/docs", b"report", filename="report.txt")
    archive = f"{run_dir}/archive.zip"
    status = fs.compress([f"{run_dir}/data.bin", f"{run_dir}/docs"], archive)
    assert status["finished"] and status["taskid"]

    items = fs.list_archive(archive)
    assert [(i["path"], i["depth"]) for i in items] == [("docs", 0), ("docs/report.txt", 1), ("data.bin", 0)]
    assert all(i["name"] != ".." for i in items)
    assert [i["path"] for i in fs.list_archive(archive, recursive=False)] == ["docs", "data.bin"]

    fs.create_folder(run_dir, "out")
    fs.extract(archive, f"{run_dir}/out", overwrite=True)
    assert fs.md5(f"{run_dir}/out/data.bin") == hashlib.md5(data).hexdigest()
    assert fs.download(f"{run_dir}/out/docs/report.txt") == b"report"


def test_compress_needs_auth_v7(nas, monkeypatch):
    """Reproduces the real NAS: an Auth v3 session gets 105 on Compress."""
    from synology_poc import client as client_mod

    monkeypatch.setitem(client_mod.DOC_VERSIONS, "SYNO.API.Auth", 3)
    c = SynologyClient("fakenas.local", cert_sha256="00" * 32)
    c.login("poc-user", PASSWORD)
    old = FileStation(c)
    old.upload("/poc-sandbox", b"x", filename="a.txt")
    with pytest.raises(SynologyError) as exc:
        old.compress("/poc-sandbox/a.txt", "/poc-sandbox/a.zip")
    assert exc.value.code == 105


# UC-18
def test_background_tasks_drop_finished(fs, run_dir):
    fs.upload(run_dir, b"x", filename="a.txt")
    fs.compress(f"{run_dir}/a.txt", f"{run_dir}/a.zip")
    assert fs.background_tasks(["SYNO.FileStation.Compress"])["tasks"] == []
    fs.clear_finished_tasks([])  # no-op, no API call needed


# UC-19
def test_thumbnail_retries_first_404(nas, fs, run_dir):
    fs.upload(run_dir, b"\x89PNG\r\n\x1a\nfake", filename="img.png")
    data, ctype = fs.thumbnail(f"{run_dir}/img.png", size="small")
    assert ctype == "image/png" and data.startswith(b"\x89PNG")


def test_thumbnail_missing_file_is_http_404(fs, run_dir):
    with pytest.raises(SynologyError) as exc:
        fs.thumbnail(f"{run_dir}/missing.png", retry_seconds=0)
    assert exc.value.http_status and exc.value.code == 404


# UC-20
def test_share_link_lifecycle(fs, run_dir):
    fs.upload(run_dir, b"img", filename="s.png")
    tomorrow = date.today() + timedelta(days=1)
    link = fs.create_link(f"{run_dir}/s.png", password="pw123", expires=tomorrow)
    assert link["url"].startswith("https://gofile.me/")
    assert link["qrcode_png"].startswith(b"\x89PNG")
    info = fs.get_link(link["id"])
    assert info["has_password"] is True
    assert info["date_expired"] == f"{tomorrow.isoformat()} 23:59:59"
    assert [x["id"] for x in fs.list_links()] == [link["id"]]
    fs.delete_links([link["id"]])
    with pytest.raises(SynologyError) as exc:
        fs.get_link(link["id"])
    assert exc.value.code == 2000
