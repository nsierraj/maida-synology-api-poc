"""Run the example scripts end to end against the fake NAS (the real NAS runs are manual)."""

import runpy
import sys
from pathlib import Path

import pytest

from synology_poc import config

from .fake_nas import PASSWORD

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture
def env(nas, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)  # never read the real .env
    for key, value in {
        "SYNO_HOST": "fakenas.local", "SYNO_USER": "poc-user", "SYNO_PASS": PASSWORD,
        "SYNO_SANDBOX": "/poc-sandbox", "SYNO_CERT_SHA256": "00" * 32, "SYNO_CA_CERT": "",
        "SYNO_DENIED_PATH": "/homes", "SYNO_POC_OUT": str(tmp_path / "out"),
    }.items():
        monkeypatch.setenv(key, value)
    return nas


def run(script: str, *args: str, monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", [script, *args])
    runpy.run_path(str(EXAMPLES / script), run_name="__main__")


def leftovers(nas) -> list[str]:
    return sorted(p for p in nas.fs if p.startswith("/poc-sandbox/poc-"))


@pytest.mark.parametrize("script", ["01_discover_and_login.py", "02_browse.py"])
def test_read_only_examples(env, monkeypatch, capsys, script):
    run(script, monkeypatch=monkeypatch)
    out = capsys.readouterr().out
    assert "Traceback" not in out and "API error" not in out


def test_03_file_lifecycle(env, monkeypatch, capsys):
    run("03_file_lifecycle.py", monkeypatch=monkeypatch)
    out = capsys.readouterr().out
    assert "round-trip integrity: MATCH" in out and "All lifecycle steps passed." in out
    assert "rejected as expected" in out and "414" in out
    assert leftovers(env) == []


def test_04_async_tasks(env, monkeypatch, capsys):
    run("04_async_tasks.py", monkeypatch=monkeypatch)
    out = capsys.readouterr().out
    assert "DIFF" not in out and out.count("MATCH") == 7
    assert "docs/report.txt" in out.split("[5]")[1].split("[6]")[0]
    assert "nothing to clear" in out
    assert leftovers(env) == []


def test_05_sharing_then_cleanup(env, monkeypatch, capsys, tmp_path):
    run("05_sharing_and_thumbs.py", monkeypatch=monkeypatch)
    out = capsys.readouterr().out
    assert "Link left ACTIVE" in out and "reachable from the internet" in out
    assert len(list((tmp_path / "out" / "thumbs").iterdir())) == 4
    assert env.links and leftovers(env)

    run("05_sharing_and_thumbs.py", "--cleanup", monkeypatch=monkeypatch)
    out = capsys.readouterr().out
    assert "getinfo says 2000" in out
    assert not env.links and leftovers(env) == []
