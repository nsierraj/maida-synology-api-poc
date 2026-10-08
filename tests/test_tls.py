"""TLS modes against a real local HTTPS server (no fake NAS: it would replace the transport)."""

import datetime
import http.server
import ipaddress
import ssl
import threading

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from synology_poc import SynologyClient
from synology_poc.config import Settings


def _cert(subject: str, issuer_key, public_key, issuer: str, *, ca: bool, sans=()):
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer)]))
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        # Python 3.13+ verifies strictly (VERIFY_X509_STRICT): these must be present.
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False)
        .add_extension(x509.KeyUsage(
            digital_signature=not ca, key_cert_sign=ca, crl_sign=ca, content_commitment=False,
            key_encipherment=False, data_encipherment=False, key_agreement=False,
            encipher_only=False, decipher_only=False), critical=True)
    )
    if sans:
        builder = builder.add_extension(x509.SubjectAlternativeName(list(sans)), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


@pytest.fixture(scope="module")
def https_nas(tmp_path_factory):
    """HTTPS on 127.0.0.1 with a certificate for fakenas.local only (like a DDNS cert reached by IP)."""
    tmp = tmp_path_factory.mktemp("tls")
    ca_key, leaf_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca = _cert("Test CA", ca_key, ca_key.public_key(), "Test CA", ca=True)
    leaf = _cert("fakenas.local", ca_key, leaf_key.public_key(), "Test CA", ca=False,
                 sans=[x509.DNSName("fakenas.local")])
    pem = serialization.Encoding.PEM
    (tmp / "ca.pem").write_bytes(ca.public_bytes(pem))
    (tmp / "leaf.pem").write_bytes(leaf.public_bytes(pem) + ca.public_bytes(pem))
    (tmp / "key.pem").write_bytes(leaf_key.private_bytes(
        pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"success": true}')

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp / "leaf.pem", tmp / "key.pem")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], str(tmp / "ca.pem")
    server.shutdown()


def get(client: SynologyClient) -> int:
    return client.session.get(f"{client.base_url}/query.cgi", timeout=5).status_code


def test_cert_hostname_verifies_against_that_name_while_connecting_by_ip(https_nas):
    port, ca = https_nas
    assert get(SynologyClient("127.0.0.1", port, ca_cert=ca, cert_hostname="fakenas.local")) == 200


def test_cert_hostname_rejects_a_certificate_for_another_name(https_nas):
    port, ca = https_nas
    with pytest.raises(requests.exceptions.SSLError):
        get(SynologyClient("127.0.0.1", port, ca_cert=ca, cert_hostname="other.local"))


def test_ca_file_alone_fails_by_ip(https_nas):
    port, ca = https_nas
    with pytest.raises(requests.exceptions.SSLError):
        get(SynologyClient("127.0.0.1", port, ca_cert=ca))


def test_cert_hostname_alone_uses_public_cas(https_nas):
    port, _ = https_nas
    client = SynologyClient("127.0.0.1", port, cert_hostname="fakenas.local")
    assert client.session.verify is True
    with pytest.raises(requests.exceptions.SSLError):  # our test CA isn't a public one
        get(client)


def test_fingerprint_wins_over_cert_hostname():
    settings = Settings(
        host=str(ipaddress.ip_address("127.0.0.1")), port=5001, user="u", password="p", sandbox="/s",
        ca_cert=None, cert_sha256="00" * 32, denied_path="/homes", cert_hostname="fakenas.local",
    )
    assert settings.client().session.verify is False
