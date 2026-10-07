import pytest

from synology_poc import SynologyClient, SynologyError



def test_discovery_and_auth_v7(nas, client):
    assert client.version("SYNO.API.Auth") == 7
    assert client.version("SYNO.FileStation.Upload") == 3  # doc version within 2-3
    assert nas.sessions[client.sid] == 7


def test_wrong_password_maps_auth_message(nas):
    c = SynologyClient("fakenas.local", cert_sha256="00" * 32)
    with pytest.raises(SynologyError) as exc:
        c.login("poc-user", "nope")
    assert exc.value.code == 400
    assert "incorrect password" in exc.value.message


def test_relogin_after_session_expiry(nas, client):
    old_sid = client.sid
    nas.expire_sessions()
    info = client.call("SYNO.FileStation.Info", "get")  # 119 -> re-login -> retry
    assert info["hostname"] == "fakenas"
    assert client.sid != old_sid


def test_relogin_covers_upload_and_download(nas, client):
    nas.expire_sessions()
    client.upload("/poc-sandbox", b"abc", filename="a.txt")
    nas.expire_sessions()
    assert client.download("/poc-sandbox/a.txt") == b"abc"


def test_logout_forgets_credentials(nas, client):
    client.logout()
    nas.expire_sessions()
    with pytest.raises(SynologyError) as exc:
        client.call("SYNO.FileStation.Info", "get")
    assert exc.value.code == 119


def test_tls_pinning_is_mandatory():
    with pytest.raises(ValueError):
        SynologyClient("fakenas.local")


def test_upload_bytes_requires_filename(client):
    with pytest.raises(ValueError):
        client.upload("/poc-sandbox", b"abc")
