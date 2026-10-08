# Synology File Station MCP server

> New to MCP? Read [mcp-explained.md](mcp-explained.md) first: it walks through how this server works, message by message.

An MCP server that gives an AI client such as Claude Code or Claude Desktop safe access to a Synology NAS through the File Station API. It wraps the use cases in [use-cases.md](use-cases.md). It runs over stdio, started by the client on your computer, or [as a container on the NAS](#running-in-container-manager), over HTTP.

- **Code:** [`src/synology_mcp/server.py`](../src/synology_mcp/server.py)
- **SDK:** the official Python `mcp` package, **2.3** (`MCPServer`; in 2.x `FastMCP` was renamed)
- **Command:** `synology-mcp` (a `[project.scripts]` entry)

## Architecture

```text
MCP client (Claude Code / Desktop)
   │  stdio, or HTTP + bearer token (JSON-RPC)
   ▼
synology_mcp.server     tools, policy checks, error → ToolError, one lock around NAS calls
   │
synology_poc.FileStation   one method per use case; all DSM 7 wire-format knowledge
   │
synology_poc.SynologyClient discovery, Auth v7 login, re-login on 106/107/119, TLS pinning
   │  HTTPS 5001
   ▼
DSM 7 (entry.cgi)
```

- **One NAS session per server process.** It's created lazily on the first tool call (starting the server doesn't touch the NAS) and logged out on shutdown. Over HTTP, all connected clients share it.
- **Calls are serialized.** The SDK runs sync tools in worker threads, and a `requests.Session` isn't safe to share between them.
- **stdout belongs to the protocol.** Nothing in the server or library prints.

## Setup

1. Complete the PoC setup in the [README](../README.md): test user, sandbox share, `.env`, and TLS pin. The server reads the **same `.env`**, so credentials never go into the MCP client config.
2. Optionally, add server settings to `.env`:

   | Variable | Default | Meaning |
   |----------|---------|---------|
   | `SYNO_MCP_ROOTS` | `SYNO_SANDBOX` | Comma-separated NAS folders that write and share tools may touch |
   | `SYNO_MCP_ALLOW_WRITES` | `false` | Register the write tools (create, upload, rename, copy/move, compress, extract, delete) |
   | `SYNO_MCP_ALLOW_SHARING` | `false` | Register the share-link tools. Links may be public on the internet (gofile.me) |
   | `SYNO_MCP_LOCAL_DIR` | (unset) | The only local folder that `upload_file` reads from and `download_file` saves to |

3. Register it with Claude Code from the repo root:

   ```bash
   claude mcp add synology -- uv run --directory "$PWD" synology-mcp
   ```

   For Claude Desktop, add this to `claude_desktop_config.json`:

   ```json
   {
     "mcpServers": {
       "synology": {
         "command": "uv",
         "args": ["run", "--directory", "/absolute/path/to/maida-synology-api-poc", "synology-mcp"]
       }
     }
   }
   ```

4. Check it: ask the client "what's in /poc-sandbox?" or "what does the synology server allow?" (`nas_info` reports the active policy).

## Running in Container Manager

Instead of the client starting the server on your computer, the server can run as a container on the NAS, and clients connect to it over HTTP. The image is built from the repo's [`Dockerfile`](../Dockerfile) for x86_64 NAS models.

```text
MCP client ──HTTPS :8443──▶ DSM reverse proxy ──HTTP 127.0.0.1:8765──▶ container :8000 /mcp ──HTTPS :5001──▶ File Station API
```

What changes compared with stdio:

- **Every request needs a token.** Anyone who can reach the port could use the NAS as the server's DSM user, so the server checks `Authorization: Bearer <SYNO_MCP_TOKEN>` on every request and refuses to start in HTTP mode without a token of at least 32 characters. `GET /healthz` is the only open path, for the container health check.
- **TLS comes from DSM.** The container publishes plain HTTP on `127.0.0.1` only, and DSM's reverse proxy serves it over HTTPS with the NAS certificate. Don't forward the port on your router.
- **The token lives in the client config.** That's the MCP token, not the NAS password, which stays in `.env` on the NAS. To rotate it, change `.env` and recreate the container.
- **`SYNO_MCP_LOCAL_DIR` is inside the container**, not on your computer. Leave it unset unless you mount a volume (see [`docker-compose.yml`](../docker-compose.yml)).
- **Responses are plain JSON**, not SSE streams, because the reverse proxy buffers streams.

### 1. Prepare the project folder

1. Install **Container Manager** from Package Center.
2. Copy the repo to a folder on the NAS, e.g. `/volume1/docker/synology-mcp`: download the ZIP from GitHub and extract it with File Station, or `git clone` over SSH.
3. Create `.env` in that folder from [`.env.example`](../.env.example), with these differences from your local one:
   - `SYNO_HOST`: the NAS's LAN IP. Inside the container, `localhost` is the container itself.
   - `SYNO_CERT_SHA256` instead of `SYNO_CA_CERT`. The fingerprint works with an IP address, and there's no certificate file in the image. Get it with the `openssl` command in the [README](../README.md).
   - `SYNO_MCP_TOKEN`: the output of `openssl rand -hex 32`.
   - Keep `SYNO_USER`, `SYNO_PASS`, `SYNO_SANDBOX` and the `SYNO_MCP_*` flags as for stdio. `docker-compose.yml` sets the transport, host and port itself.

   The `.dockerignore` keeps `.env` and `certs/` out of the image; Compose passes the settings to the container at start.

### 2. Create the project

1. Container Manager → **Project** → **Create**. Name it `synology-mcp`, set the path to the folder above, and use the existing `docker-compose.yml`.
2. Container Manager builds the image and starts the container. Over SSH, `sudo docker compose up -d --build` in the folder does the same.
3. Check it over SSH: `curl http://127.0.0.1:8765/healthz` prints `ok`. The container's log is under Container Manager → **Container** → `synology-mcp` → **Log**.

### 3. Add HTTPS with the reverse proxy

1. Control Panel → **Login Portal** → **Advanced** → **Reverse Proxy** → **Create**:

   | | Protocol | Hostname | Port |
   |---|---|---|---|
   | Source | HTTPS | your NAS hostname, e.g. `fakenas.local` | 8443 |
   | Destination | HTTP | `127.0.0.1` | 8765 |

2. In the rule's **Advanced Settings**, raise the proxy read timeout (e.g. 600 s). `compress`, `extract` and `folder_size` wait for the NAS job inside one request, and the default is 60 s.
3. Control Panel → **Security** → **Certificate** → **Settings**: pick the certificate for the new entry.
4. If the DSM firewall is on, allow port 8443 from your LAN only.

### 4. Connect a client

Claude Code:

```bash
claude mcp add --transport http synology https://fakenas.local:8443/mcp \
  --header "Authorization: Bearer <SYNO_MCP_TOKEN>"
```

Claude Desktop, through the `mcp-remote` bridge (needs Node.js):

```json
{
  "mcpServers": {
    "synology": {
      "command": "npx",
      "args": ["mcp-remote", "https://fakenas.local:8443/mcp", "--header", "Authorization:${AUTH_HEADER}"],
      "env": { "AUTH_HEADER": "Bearer <SYNO_MCP_TOKEN>" }
    }
  }
}
```

If the NAS certificate comes from your own CA or is self-signed, Node.js won't trust it. Set `NODE_EXTRA_CA_CERTS` to the exported CA file (`certs/synology-ca.pem`) for Claude Code, or in the `env` block for `mcp-remote`.

**Updating:** replace the files in the project folder, then rebuild: `sudo docker compose up -d --build`, or stop the project and build it again in Container Manager.

## Safety model

| Layer | What it enforces |
| --- | --- |
| DSM account | The hard boundary. Use a dedicated **non-admin** user with access to only the shares it needs. |
| Tool registration | Write and sharing tools **don't exist** unless their flag is on, so the model can't call them. |
| `PathPolicy` ([`policy.py`](../src/synology_poc/policy.py)) | Every write or share target is normalized (`..` resolved) and must lie inside `SYNO_MCP_ROOTS`. A root itself can't be deleted or renamed. Moves also check their sources. |
| Local files | `upload_file` / `download_file` only touch `SYNO_MCP_LOCAL_DIR`; paths that escape it are refused. |
| Share links | Separate flag. Expiry is required (1–30 days, default 1) and the result flags `public_internet`. |
| HTTP transport | Bearer token on every request (constant-time check), plain HTTP bound to `127.0.0.1`, TLS from DSM's reverse proxy. |
| Tool annotations | `read_only_hint` and `destructive_hint` let clients ask for confirmation before writes and deletes. |

Reads aren't restricted by `SYNO_MCP_ROOTS`; they're bounded by the DSM account. To keep the model out of a share, take the account's access away in DSM.

## Tools

**Always available (read-only)**

| Tool | Arguments | Returns |
| --- | --- | --- |
| `nas_info` | — | hostname, is_admin, sharing_supported, active server policy |
| `list_shares` | — | `[{path, free_bytes, total_bytes, read_only}]` |
| `list_folder` | `path`, `offset=0`, `limit=100`, `sort_by=name`, `sort_direction=asc`, `pattern`, `filetype=all`, `include_system=false` | `{total, offset, hidden_system, items[{name, path, is_dir, size, modified, type}]}` |
| `get_file_info` | `paths[]` | items with owner and real_path |
| `folder_size` | `paths[]` | `{num_file, num_dir, total_size}` |
| `file_md5` | `path` | `{path, md5}` |
| `search_files` | `folder`, `pattern` (space-separated globs), `extension` (comma-separated), `filetype`, `max_results=100` | `{total, items[]}` |
| `list_archive` | `path`, `recursive=true` | `[{path, is_dir, size, packed_size, depth}]` |
| `get_thumbnail` | `path`, `size=medium` | image content |
| `download_file` | `path`, `save=false` | `{text}` for UTF-8 files up to 256 KB, otherwise `{saved_to}` under `SYNO_MCP_LOCAL_DIR` |
| `list_background_tasks` | — | running tasks (finished ones may already be gone) |
| `list_share_links` | — | `[{id, url, path, status, has_password, date_expired}]` |

**`SYNO_MCP_ALLOW_WRITES=true`** (targets must be inside `SYNO_MCP_ROOTS`)

| Tool | Arguments | Notes |
| --- | --- | --- |
| `create_folder` | `parent`, `name`, `create_parents=false` | |
| `upload_file` | `dest_folder`, `local_file` *or* `text` + `filename`, `overwrite=false` | Existing files are skipped unless `overwrite` is true |
| `rename` | `path`, `new_name` | `new_name` can't contain `/` |
| `copy_move` | `paths[]`, `dest_folder`, `move=false`, `overwrite=false` | A copy may read from outside the roots; a move may not |
| `compress` | `paths[]`, `dest_file_path`, `password` | zip |
| `extract` | `archive`, `dest_folder`, `overwrite=false`, `password` | The destination folder must exist |
| `delete` | `paths[]` | **destructive**; usually goes to `#recycle` |

**`SYNO_MCP_ALLOW_SHARING=true`**

| Tool | Arguments | Notes |
| --- | --- | --- |
| `create_share_link` | `path`, `password`, `expires_in_days=1` (1–30) | Returns `public_internet: true` for gofile.me URLs |
| `edit_share_link` | `link_id`, `password` (`""` removes it), `expires_in_days` | |
| `delete_share_link` | `link_ids[]` | Only links whose target is inside the roots |

## Prompts

Prompts are ready-made tasks that tell the model which tools to combine. They only produce instructions, so every tool call still goes through the client's approvals and the server policy. In Claude Code they appear as slash commands, e.g. `/mcp__synology__audit_access`; in the Inspector, under **Prompts**.

| Prompt | Arguments | What it does |
| --- | --- | --- |
| `audit_access` | — | `nas_info` + `list_shares` + `list_share_links`: flags shares outside the write roots (other than `/home`), admin accounts, and active links. Ends with DSM steps to remove access that isn't needed |
| `find_large_files` | `folder` (default: first root), `top` (default 10) | `folder_size` + `search_files`, then the largest files as a table. Read-only |
| `clean_sandbox` | `folder` (default: first root) | Finds `poc-run-*` / `poc-share-*` / `poc-probe-*` leftovers and their links, **asks before deleting**. The wording adapts to whether writes and sharing are enabled |

## Errors

- **NAS errors** come back as tool errors with the mapped message, e.g. `SYNO.FileStation.List.list -> 407: Operation not permitted`. Codes are listed in [file-station-api.md §4](synology/file-station-api.md#4-common-error-codes).
- **Policy refusals** read `Refused by policy: '/video/x' is outside the allowed roots: /poc-sandbox`.
- **Login, TLS, and network problems** surface on the first tool call with a hint.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `TLS check against the NAS failed` | The certificate fingerprint changed (DSM renewed its certificate) or a CA file doesn't match the host. Re-run the `openssl` command in the README. |
| `NAS login failed: … 400` | Wrong user or password in `.env`. Repeated failures make DSM Auto Block ban the IP (Control Panel → Security → Protection). |
| `105` on compress | The session wasn't created with Auth v7. Check that `client.DOC_VERSIONS["SYNO.API.Auth"]` is 7 and the NAS advertises v7 (`uv run examples/01_discover_and_login.py`). |
| Share tools missing | `SYNO_MCP_ALLOW_SHARING` isn't `true`. If creation fails, the DSM user may lack the sharing permission (File Station → Settings). |
| Write tools missing | `SYNO_MCP_ALLOW_WRITES` isn't `true`. Restart the server after changing `.env`. |
| Client shows no tools / server exits | Run `uv run synology-mcp` by hand. A missing `.env` setting is reported on stderr. |
| Container keeps restarting, log mentions `SYNO_MCP_TOKEN` | The token is missing or shorter than 32 characters. |
| `401` / client says unauthorized | The client's token doesn't match `SYNO_MCP_TOKEN`. After changing `.env`, recreate the container (`sudo docker compose up -d`); a plain restart keeps the old settings. |
| `Cannot reach the NAS at localhost` (container) | Set `SYNO_HOST` to the NAS's LAN IP. |
| `SYNO_CA_CERT points to … which doesn't exist` (container) | Use `SYNO_CERT_SHA256` instead; the image has no certificate files. |
| `502` from the reverse proxy | The destination must be `127.0.0.1`, not `localhost`: the container only listens on IPv4 loopback. |
| Long tool calls fail over HTTPS but work on `127.0.0.1:8765` | The reverse proxy timed out. Raise its read timeout (step 3 above). |

## Development

- **Tests:** `uv run pytest`. They use [`tests/fake_nas.py`](../tests/fake_nas.py), an in-memory DSM 7 that reproduces the observed quirks. `tests/test_mcp_server.py` drives the server through the SDK's in-process `mcp.Client`; `tests/test_mcp_http.py` runs it under uvicorn and checks the token.
- **Inspector:** `npx @modelcontextprotocol/inspector uv run synology-mcp` gives a UI for calling the tools by hand.

### Adding a tool

1. Implement and test the operation in `FileStation` first (see [use-cases.md → Adding a use case](use-cases.md#adding-a-use-case)).
2. In `build_server()`, add a function under the right section and decorate it with `@tool(READ_ONLY | WRITE | DESTRUCTIVE | EXPOSING)`.
   - Its docstring becomes the tool description, and its type hints become the input schema.
   - Call the matching `policy.check_*` **before** touching the NAS.
   - Return compact JSON (use `_file()` / `_link()`).
3. Add it to the tool sets in `tests/test_mcp_server.py` and write a test, including a policy-escape case for any write.
4. Document it in the tool tables above and in the use-case catalog.
