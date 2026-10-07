# maida-synology-api-poc

Proof of concept for the Synology **File Station** Web API (DSM 7.x), in Python: a reusable
library, runnable examples, and an MCP server.

| Doc | Contents |
| --- | --- |
| [docs/use-cases.md](docs/use-cases.md) | The 20 use cases verified on a real NAS: API calls, encoding, DSM 7 quirks, implementation, MCP tool |
| [docs/mcp-server.md](docs/mcp-server.md) | The MCP server: setup, tools, safety model, troubleshooting |
| [docs/synology/file-station-api.md](docs/synology/file-station-api.md) | Condensed API reference plus errata against the official guide |

| Example | What it shows | Writes to NAS? |
| --- | --- | --- |
| `01_discover_and_login.py` | `SYNO.API.Info` discovery, login, `FileStation.Info`, logout | No |
| `02_browse.py` | List shares, list a folder, file details, error handling on a denied path | No |
| `03_file_lifecycle.py` | Create folder → upload → conflict handling → rename → async copy → download + SHA-256 check → async delete | Only inside `SYNO_SANDBOX/poc-run-*` |
| `04_async_tasks.py` | Background jobs (start → poll → result): folder size, MD5, search, compress, list archive, extract + MD5 check, task list | Only inside `SYNO_SANDBOX/poc-run-*` (self-cleaning) |
| `05_sharing_and_thumbs.py` | Thumbnails saved to `out/`, password-protected sharing link: create → getinfo → edit expiry → list | `SYNO_SANDBOX/poc-share-*`, **left active** until `--cleanup` |

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
uv run examples/04_async_tasks.py             # add --keep to inspect poc-run-* afterwards
uv run examples/05_sharing_and_thumbs.py      # prints the link URL + password; open it in a browser
uv run examples/05_sharing_and_thumbs.py --cleanup
```

Notes:

- If QuickConnect is enabled, `05`'s link is a `gofile.me` URL that is **reachable from the internet** (password-protected). Run `--cleanup` once you've tried it.
- `05` leaves the share link and its `poc-share-*` folder in place on purpose. The link expires the next day, but run `--cleanup` to remove it sooner. Deleting the folder by hand would leave a "broken" link behind.
- Creating links requires the test user to have the sharing permission (DSM File Station → Settings). If it's missing, `05` stops with a hint.
- Thumbnails, the QR code, and other local output go to `out/`, which is gitignored.

## MCP server

```bash
claude mcp add synology -- uv run --directory "$PWD" synology-mcp
```

It's read-only by default. Write tools need `SYNO_MCP_ALLOW_WRITES=true` and stay inside `SYNO_MCP_ROOTS`, and share links need `SYNO_MCP_ALLOW_SHARING=true`. See [docs/mcp-server.md](docs/mcp-server.md).

## Tests

```bash
uv run pytest
```

The tests run the library, the examples, and the MCP server against `tests/fake_nas.py`, an in-memory DSM 7 that reproduces the quirks seen on the real NAS. The fake is lenient about parameter quoting, so a real-NAS run remains the final check.

## Code layout

- `src/synology_poc/client.py`: `SynologyClient`. It handles discovery and versioning, Auth v7 login/logout, re-login on expired sessions, the generic `call()`, `upload()`, `fetch_binary()`/`download()`, and `wait_task()` for async jobs.
- `src/synology_poc/filestation.py`: `FileStation`, one method per use case. All DSM 7 wire-format knowledge lives here.
- `src/synology_poc/policy.py`: `PathPolicy`, which keeps writes and share links inside allowed roots.
- `src/synology_poc/errors.py`: `SynologyError` and the error code tables from the guide.
- `src/synology_poc/config.py`: loads `.env` and connects with readable failures (TLS, network, login).
- `src/synology_poc/sandbox.py`: helpers for the examples (sandbox guard, run folder names, async delete).
- `src/synology_mcp/server.py`: the MCP server (`synology-mcp`).
- `tests/`: the fake NAS and the test suites.
