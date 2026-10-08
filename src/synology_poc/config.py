"""Load connection settings from .env and build a logged-in client."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

import requests
from dotenv import load_dotenv

from .client import SynologyClient
from .errors import SynologyError


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    user: str
    password: str
    sandbox: str
    ca_cert: str | None
    cert_sha256: str | None
    denied_path: str
    cert_hostname: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        missing = [k for k in ("SYNO_HOST", "SYNO_USER", "SYNO_PASS") if not os.getenv(k)]
        if missing:
            sys.exit(f"Missing settings in .env: {', '.join(missing)} (copy .env.example)")
        sandbox = "/" + os.getenv("SYNO_SANDBOX", "/poc-sandbox").strip("/")
        return cls(
            host=os.environ["SYNO_HOST"],
            port=int(os.getenv("SYNO_PORT", "5001")),
            user=os.environ["SYNO_USER"],
            password=os.environ["SYNO_PASS"],
            sandbox=sandbox,
            ca_cert=os.getenv("SYNO_CA_CERT") or None,
            cert_sha256=os.getenv("SYNO_CERT_SHA256") or None,
            denied_path=os.getenv("SYNO_DENIED_PATH", "/homes"),
            cert_hostname=os.getenv("SYNO_CERT_HOSTNAME") or None,
        )

    def client(self) -> SynologyClient:
        if not self.cert_sha256 and self.ca_cert and not os.path.isfile(self.ca_cert):
            sys.exit(
                f"SYNO_CA_CERT points to {self.ca_cert!r}, which doesn't exist. "
                "Export the cert from DSM or set SYNO_CERT_SHA256 / SYNO_CERT_HOSTNAME instead (see README)."
            )
        # A fingerprint wins over the other settings when several are set.
        return SynologyClient(
            self.host,
            self.port,
            ca_cert=None if self.cert_sha256 else self.ca_cert,
            cert_sha256=self.cert_sha256,
            cert_hostname=None if self.cert_sha256 else self.cert_hostname,
        )


def connect(settings: Settings) -> SynologyClient:
    """Discover + login, turning the usual setup mistakes into readable exits."""
    client = settings.client()
    try:
        client.discover()
        client.login(settings.user, settings.password)
    except requests.exceptions.SSLError as e:
        sys.exit(
            f"TLS check failed: {e}\n"
            "If the cert doesn't cover this host/IP, set SYNO_CERT_HOSTNAME to a name it covers, "
            "or use SYNO_CERT_SHA256 (see README)."
        )
    except requests.exceptions.ConnectionError as e:
        sys.exit(f"Cannot reach https://{settings.host}:{settings.port}: {e}")
    except SynologyError as e:
        sys.exit(f"Login failed: {e}")
    return client


def mask(secret: str, keep: int = 6) -> str:
    return secret[:keep] + "…" if len(secret) > keep else "…"
