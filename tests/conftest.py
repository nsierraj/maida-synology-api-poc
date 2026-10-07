import time

import pytest

from synology_poc import FileStation, SynologyClient

from .fake_nas import PASSWORD, FakeNAS


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Polling loops sleep between status calls; tests don't need to wait."""
    monkeypatch.setattr(time, "sleep", lambda seconds: None)


@pytest.fixture
def nas(monkeypatch) -> FakeNAS:
    fake = FakeNAS()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
def client(nas) -> SynologyClient:
    c = SynologyClient("fakenas.local", cert_sha256="00" * 32)
    c.login("poc-user", PASSWORD)
    yield c
    c.session.close()


@pytest.fixture
def fs(client) -> FileStation:
    return FileStation(client)


@pytest.fixture
def run_dir(fs) -> str:
    fs.create_folder("/poc-sandbox", "run")
    return "/poc-sandbox/run"
