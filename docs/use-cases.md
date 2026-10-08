# File Station use cases: catalog and implementation

This catalog lists every use case this project has proven against a real NAS. For each one it gives the API calls, parameter encoding, NAS quirks, the library method that implements it, and the MCP tool that exposes it. It's the reference for extending the library or the [MCP server](mcp-server.md).

- **Verified on:** DSM 7.x, NAS `maida` (nas.example.lan), non-admin user `poc-user`, 2026-10-07.
- **API reference:** [synology/file-station-api.md](synology/file-station-api.md). Its "Errata" section (§7) lists every place where DSM 7 differs from the official guide.
- **Implementation:** [`src/synology_poc/filestation.py`](../src/synology_poc/filestation.py) (`FileStation`). The examples in [`examples/`](../examples) and the tests in [`tests/`](../tests) exercise every method.

| ID | Use case | Library method | MCP tool | Safety |
| --- | --- | --- | --- | --- |
| UC-01 | Connect and authenticate | `SynologyClient.discover/login/logout` | (implicit) | — |
| UC-02 | NAS / File Station info | `info()` | `nas_info` | read |
| UC-03 | List shared folders | `list_shares()` | `list_shares` | read |
| UC-04 | List a folder | `list_folder()` | `list_folder` | read |
| UC-05 | File / folder details | `get_info()` | `get_file_info` | read |
| UC-06 | Create a folder | `create_folder()` | `create_folder` | write |
| UC-07 | Upload a file | `upload()` | `upload_file` | write |
| UC-08 | Download a file | `download()` | `download_file` | read |
| UC-09 | Rename | `rename()` | `rename` | write |
| UC-10 | Copy / move | `copy_move()` | `copy_move` | write |
| UC-11 | Delete | `delete()` | `delete` | destructive |
| UC-12 | Folder size | `dir_size()` | `folder_size` | read |
| UC-13 | MD5 checksum | `md5()` | `file_md5` | read |
| UC-14 | Search | `search()` | `search_files` | read |
| UC-15 | Compress to zip | `compress()` | `compress` | write |
| UC-16 | List archive contents | `list_archive()` | `list_archive` | read |
| UC-17 | Extract an archive | `extract()` | `extract` | write |
| UC-18 | Background tasks | `background_tasks()`, `clear_finished_tasks()` | `list_background_tasks` | read |
| UC-19 | Thumbnail | `thumbnail()` | `get_thumbnail` | read |
| UC-20 | Share links | `create_link/get_link/edit_link/list_links/delete_links()` | `create_share_link`, `edit_share_link`, `delete_share_link`, `list_share_links` | external exposure |

---

## Cross-cutting rules

These apply to every use case. Each was learned on the real NAS, and the library handles them all.

1. **Discover first.** `SYNO.API.Info` (`query.cgi`, v1, `query=all`) is the only fixed endpoint. Resolve every other API's CGI path and version from it. On DSM 7 every File Station API lives on `entry.cgi`.
2. **Log in with Auth v7.** The guide documents v3, but on DSM 7 a non-admin session created with Auth **v3 or v6** gets **105** from `Compress`. Session name, cookie vs `_sid`, SynoToken, and Compress version made no difference. `client.DOC_VERSIONS` asks for v7, clamped to what the NAS offers.
3. **Pin TLS.** The client refuses to run without `SYNO_CERT_SHA256` (fingerprint pin, works by IP), `SYNO_CERT_HOSTNAME` (connect by IP, verify the certificate against that name with public CAs; survives renewals) or `SYNO_CA_CERT` (CA file, hostname must match unless `SYNO_CERT_HOSTNAME` is set).
4. **Parameter encoding.**
   - Lists are JSON arrays: `path=["/a","/b"]`.
   - Booleans are `true`/`false`.
   - Plain strings are sent raw: `folder_path=/poc-sandbox`.
   - Task IDs are JSON-quoted: `taskid="FileStation_…"`.
   - Compress and Extract need **every** string JSON-quoted (see UC-15).
   - Send everything as a form-encoded POST so passwords never appear in URLs.
5. **Async pattern.** `start` → poll (`status`, or `list` for Search) until `finished` → result. A poll can report `finished=true` immediately. A 599 means the task was already reaped. Search must always be `stop`ped and `clean`ed.
6. **Errors.**
   - JSON envelope `{"success": false, "error": {"code", "errors": [...]}}`. `errors.describe()` maps codes, with API-specific tables first.
   - Auth 400–404 mean something different from File Station 400–404.
   - Binary endpoints (Thumb, Download) can fail with an HTTP status instead.
7. **Session lifetime.** On 106/107/119 the client logs in again once and retries, which matters for long-running processes. `logout()` forgets the credentials.
8. **System folders.** Shares contain `#recycle`, and sometimes `#snapshot` or `@eaDir`. Listings hide them by default. Deletes usually land in `#recycle`, which non-admins can't read.

---

## UC-01: Connect and authenticate

- **Calls:** `SYNO.API.Info.query` (v1) → `SYNO.API.Auth.login` (**v7**) → … → `SYNO.API.Auth.logout`.
- **Params:** `account`, `passwd`, `session=FileStation`, `format=sid`. The SID is then sent as `_sid` on every call (on uploads, in the query string).
- **Returns:** `{sid}`. The SID lasts 7 days by default.
- **Quirks:**
  - Auth v3/v6 sessions are refused for Compress (cross-cutting rule 2).
  - A wrong password returns Auth **400**.
  - Repeated failures trigger DSM Auto Block on the client IP.
- **Implementation:** `SynologyClient` in [`client.py`](../src/synology_poc/client.py). `config.connect()` turns TLS, network, and login failures into readable exits.
- **Example:** [`01_discover_and_login.py`](../examples/01_discover_and_login.py).

## UC-02: NAS / File Station info

- **Call:** `SYNO.FileStation.Info.get` (v2).
- **Returns:** `hostname`, `is_manager` (false for a non-admin user), `support_sharing`, and `support_virtual_protocol`. On DSM 7 the last one is a **list**, not the comma-separated string the guide describes.

## UC-03: List shared folders

- **Call:** `SYNO.FileStation.List.list_share` (v2), `additional=["real_path","owner","time","perm","volume_status"]`.
- **Returns:** `shares[]`, each with `path` and `additional.volume_status{freespace,totalspace,readonly}`.
- **Note:** this shows exactly what the account can reach. It's the quickest way to audit a service account's permissions; the PoC used it to find that the test user could still see `/docker` and `/video`.

## UC-04: List a folder

- **Call:** `SYNO.FileStation.List.list` (v2).
- **Params:**
  - `folder_path` (raw string)
  - `offset`, `limit` (0 = all)
  - `sort_by` (`name|size|user|group|mtime|atime|ctime|crtime|posix|type`) and `sort_direction`
  - `pattern` (comma-separated globs; no wildcard means `*x*`)
  - `filetype` (`file|dir|all`)
  - `additional`
- **Returns:** `{total, offset, files[{path,name,isdir,additional}]}`.
- **Quirks:**
  - `total` counts system folders; the library returns `hidden_system`.
  - A path the account can't read returns **407**; a missing one returns **408**.

## UC-05: File / folder details

- **Call:** `SYNO.FileStation.List.getinfo` (v2), `path=[…]` (JSON list), `additional=["real_path","size","owner","time","perm","type"]`.
- **Returns:** `files[]`. `perm.acl` shows what the account may do with each item.

## UC-06: Create a folder

- **Call:** `SYNO.FileStation.CreateFolder.create` (v2), `folder_path=[parent]`, `name=[name]` (paired lists), `force_parent`.
- **Returns:** `folders[0]`. Errors: 1100 (details in `errors[]`), 1101.

## UC-07: Upload a file

- **Call:** `SYNO.FileStation.Upload.upload` (**v3**), a `multipart/form-data` POST.
  - `api`, `version`, `method` and `_sid` go in the query string.
  - `path`, `create_parents` and `overwrite` are form fields.
  - The **file part must come last**.
- **overwrite (v3):** `overwrite` | `skip` | omitted. With `skip`, the response is `{blSkip: true}`.
- **Quirk:** on DSM 7 a conflict with `overwrite` omitted returns **414** ("File already exists"), not 1805 as the guide says.
- **Implementation:** `upload(dest, bytes | Path, filename=…)`.

## UC-08: Download a file

- **Call:** `SYNO.FileStation.Download.download` (v2) as a GET, `path=["…"]`, `mode=download`.
- **Returns:** raw bytes. Errors come back as a JSON envelope instead, so check `Content-Type`.
- **Example check:** SHA-256 of the upload equals SHA-256 of the download ([example 03](../examples/03_file_lifecycle.py)).

## UC-09: Rename

- **Call:** `SYNO.FileStation.Rename.rename` (v2), `path=[…]`, `name=[…]` (paired lists).
- **Returns:** `files[0]` with the new path. Error: 1200.

## UC-10: Copy / move

- **Calls:** `SYNO.FileStation.CopyMove.start` (v3) → `status` until `finished`.
- **Params:** `path=[…]`, `dest_folder_path` (raw), `overwrite` (true/false; omitting it makes a conflict fail with 1003), `remove_src` (true = move).
- **Status:** `{processed_size, total (-1 while counting), progress, finished}`.

## UC-11: Delete

- **Calls:** `SYNO.FileStation.Delete.start` (v2) → `status`. Params: `path=[…]`, `recursive=true`, `accurate_progress=true`.
- **Notes:**
  - With the share's recycle bin enabled, items move to `#recycle`.
  - The library never deletes on its own; the examples and the MCP server guard every path to stay inside the allowed roots.

## UC-12: Folder size

- **Calls:** `SYNO.FileStation.DirSize.start` (v2), `path=[…]` → `status` → `{num_file, num_dir, total_size}`.
- **Quirk:** `num_dir` doesn't count the queried folder itself (verified: 1 subfolder → `num_dir=1`).

## UC-13: MD5 checksum

- **Calls:** `SYNO.FileStation.MD5.start` (v2), `file_path` (raw) → `status` → `{md5}`.
- **Note:** this verifies NAS-side content without downloading it. The PoC used it to prove a file survived compress → extract unchanged.

## UC-14: Search

- **Calls:** `SYNO.FileStation.Search.start` (v2) → poll `list` (`taskid`, `limit=0`) until `finished` → `list` (`limit=-1` or N) → **`stop` + `clean` always**.
- **Params:**
  - `folder_path=[…]`, `recursive`
  - `pattern`: globs separated by **spaces** (unlike `List.list`, which uses commas)
  - `extension`: comma-separated
  - `filetype`, size/time ranges, `owner`, `group`
- **Quirk:** `list` defaults to `limit=0`, which returns nothing. Without `clean`, the temporary search database stays on the NAS.

## UC-15: Compress to zip

- **Calls:** `SYNO.FileStation.Compress.start` (v3) → `status` until `finished`.
- **Params, exactly as the DSM 7 web UI sends them:**
  - `path=[…]`
  - `dest_file_path="…"`, `level="normal"`, `mode="replace"`, `format="zip"`, `codepage="enu"`: each **JSON-quoted**
  - `password=null`
  - `level=normal` and `mode=replace` are **not** in the guide (which lists `moderate` and `add`).
- **Quirk:** needs a session created with **Auth v7** (cross-cutting rule 2). The fake NAS in `tests/` reproduces the 105.
- **Example:** [`04_async_tasks.py`](../examples/04_async_tasks.py), step 4.

## UC-16: List archive contents

- **Call:** `SYNO.FileStation.Extract.list` (v2), `file_path="…"` (JSON-quoted), `limit=-1`, `codepage="enu"`, `item_id` (omit for the root).
- **Quirks:**
  - Returns **one folder level per call**. To see a folder's contents, call again with that folder's `item_id`.
  - Inside a folder, the result contains a parent entry `{"name": "..", "item_id": -1, "path": "root"}` with no `size`; skip it.
  - Items aren't ordered.
  - `list_archive()` walks recursively and returns a flat list with `depth`.

## UC-17: Extract an archive

- **Calls:** `SYNO.FileStation.Extract.start` (v2) → `status`.
- **Params:** `file_path` and `dest_folder_path` (JSON-quoted; the destination must exist), `overwrite`, `keep_dir`, `create_subfolder`, `codepage="enu"`, and `password` (optional).
- **Verified:** the MD5 of the extracted file matches the original, and subfolders are preserved.

## UC-18: Background tasks

- **Calls:** `SYNO.FileStation.BackgroundTask.list` (v3), `api_filter=[…]` → `clear_finished`, `taskid=[…]`.
- **Quirk:** on DSM 7, finished tasks whose status was already read are dropped from the list (`total: 0`). Treat the list as "what's running now", not history. Clear only IDs you own.

## UC-19: Thumbnail

- **Call:** `SYNO.FileStation.Thumb.get` (v2) as a GET, `path` (raw), `size` (`small|medium|large|original`), `rotate` (`1` = 90°, `2` = 180°, …).
- **Returns:** image bytes in the **source's format** (PNG in, PNG out). Errors are HTTP status codes.
- **Verified sizes for a 640×480 source:** small 120×90, medium 360×270, large 640×480 (never upscaled). `rotate=1` gives 270×360.
- **Quirk:** a fresh file can briefly return 404 while DSM generates the thumbnail; the library retries for up to 5 s.

## UC-20: Share links

- **Calls:** `SYNO.FileStation.Sharing` (v3): `create` → `getinfo` → `edit` → `list` → `delete`.
- **Params:**
  - `create`: `path=[…]`, `password` (max 16 characters). It also returns `qrcode` (a base64 PNG `data:` URL).
  - `getinfo`: `id="…"` (JSON-quoted).
  - `edit`: `id=[…]`, `date_expired="YYYY-MM-DD"` (quoted). `password=""` removes the password; omitting it leaves it unchanged.
  - `delete`: `id=[…]`.
- **Quirks:**
  - `date_expired` reads `""` when unset and `"YYYY-MM-DD 23:59:59"` after an edit.
  - A deleted link gives **2000** on `getinfo`.
- **⚠ Exposure:** with QuickConnect enabled, the URL is `https://gofile.me/<qc-id>/<link-id>`, which is **reachable from the internet** via Synology's relay. The MCP server keeps these tools behind a separate flag and defaults new links to a 1-day expiry.
- **Example:** [`05_sharing_and_thumbs.py`](../examples/05_sharing_and_thumbs.py). It leaves the link active; `--cleanup` removes it.

---

## Adding a use case

1. Read the API section in [file-station-api.md](synology/file-station-api.md). If the DSM 7 web UI can do the operation, capture its request in DevTools first: on this NAS, the UI's parameters have been more reliable than the guide's.
2. Add a `FileStation` method that hides the wire format, and a test in `tests/test_filestation.py`. Teach `tests/fake_nas.py` any quirk you observe.
3. Prove it on the real NAS with an example script inside the sandbox.
4. Add an MCP tool (see [mcp-server.md](mcp-server.md#adding-a-tool)), and a row plus a section here.
