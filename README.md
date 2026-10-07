# maida-synology-api-poc

Proof of concept for the Synology **File Station** Web API (DSM 7.x), in Python.
API reference: [docs/synology/file-station-api.md](docs/synology/file-station-api.md).

| Example | What it shows | Writes to NAS? |
|---------|---------------|----------------|
| `01_discover_and_login.py` | `SYNO.API.Info` discovery, login, `FileStation.Info`, logout | No |
| `02_browse.py` | List shares, list a folder, file details, error handling on a denied path | No |
| `03_file_lifecycle.py` | Create folder → upload → conflict handling → rename → async copy → download + SHA-256 check → async delete | Only inside `SYNO_SANDBOX/poc-run-*` |

## 1. Prepare the NAS (one time)

1. **Shared folder:** Control Panel → Shared Folder → Create `poc-sandbox`.
2. **Test user:** Control Panel → User & Group → Create `poc-user`:
   - not in `administrators`
   - Permissions: **Read/Write** on `poc-sandbox`, **No access** on everything else
   - Applications: allow **File Station**
   - leave 2-step verification off for this user
3. **Auto Block:** DSM blocks an IP after repeated failed logins (Control Panel → Security → Protection). Double-check the password before the first run.

## 2. Pin the NAS certificate

The client refuses to run without TLS pinning. Pick one option:

**Option A: fingerprint (simplest when connecting by IP).**

```bash
openssl s_client -connect <NAS_IP>:5001 </dev/null 2>/dev/null \
  | openssl x509 -noout -fingerprint -sha256
```

Put the hex value in `SYNO_CERT_SHA256=` (colons are fine). Compare it with the certificate shown in DSM (Control Panel → Security → Certificate) so you know you're pinning the real NAS.

**Option B: exported CA file.**
Control Panel → Security → Certificate → select the cert → **Export**. Copy the CA/chain file from the zip (e.g. `syno-ca-cert.pem`) to `certs/synology-ca.pem` and set `SYNO_CA_CERT`.

- The export also contains `privkey.pem`. Delete it; never keep the private key in this repo. `certs/` is gitignored.
- DSM's default certificate usually doesn't list the LAN IP. If you get a hostname mismatch error, connect via a hostname the cert covers, or use option A.

## 3. Configure and run

```bash
uv sync
cp .env.example .env      # then fill in host, user, password, sandbox, and the cert option
uv run examples/01_discover_and_login.py
uv run examples/02_browse.py
uv run examples/03_file_lifecycle.py --keep   # inspect poc-run-* in File Station, then:
uv run examples/03_file_lifecycle.py
```

## Code layout

- `src/synology_poc/client.py`: `SynologyClient`. It handles discovery and versioning, login/logout, the generic `call()`, `upload()`, `download()`, and `wait_task()` for async jobs.
- `src/synology_poc/errors.py`: `SynologyError` and the error code tables from the guide.
- `src/synology_poc/config.py`: loads `.env` and connects with readable failures (TLS, network, login).
