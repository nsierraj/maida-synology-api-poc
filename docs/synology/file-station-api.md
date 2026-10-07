# Synology File Station API — Developer Reference

Condensed, searchable reference transcribed from the official
[Synology File Station API Guide](https://global.download.synology.com/download/Document/Software/DeveloperGuide/Package/FileStation/All/enu/Synology_File_Station_API_Guide.pdf) (revision 2023.03, covers DSM 6.0 / 7.x).
The PDF is the source of truth; page numbers below refer to the PDF's printed page numbers.

Download the guide from the link above; it is not stored in this repo.

> The guide is proprietary Synology documentation, so it is not redistributed here. This file is a condensed summary of it.

---

## 1. Workflow

1. **Discover APIs** — `GET /webapi/query.cgi?api=SYNO.API.Info&version=1&method=query&query=all`
   Returns, per API name: `path` (CGI path), `minVersion`, `maxVersion`. `query.cgi` is the only fixed location; resolve every other API's path from this response.
2. **Log in** — `SYNO.API.Auth` `login` with `session=FileStation`. Keep the returned `sid`.
3. **Call File Station APIs** — pass `_sid=<sid>` on every request (or send cookie `id=<sid>`).
4. **Log out** — `SYNO.API.Auth` `logout` with `session=FileStation`.

On DSM 6+/7 nearly every File Station API is served from `entry.cgi`, but always use the `path` from `SYNO.API.Info`.

## 2. Request format

```text
GET /webapi/<CGI_PATH>?api=<API_NAME>&version=<VERSION>&method=<METHOD>[&<PARAMS>][&_sid=<SID>]
```

- Default ports: HTTP `5000`, HTTPS `5001`. GET or POST (form-encoded) both work; upload is `multipart/form-data` POST.
- All params must be URL-encoded.
- **Multi-value params** (paths, names, `additional`, task IDs) are sent as a JSON array: `path=["/video/a","/video/b"]` → `path=%5B%22%2Fvideo%2Fa%22%2C%22%2Fvideo%2Fb%22%5D`.
  Single string values are often sent JSON-quoted as well (`path="/video"` → `path=%22%2Fvideo%22`); the guide's examples do this.
- Legacy comma-delimited escaping: in non-JSON lists `,` separates items, so a literal comma is written `\,` and a backslash `\\`. Passwords (`passwd`, `password`) are never escaped this way.

## 3. Response format

```json
{ "success": true,  "data":  { ... } }
{ "success": false, "error": { "code": 1100, "errors": [ { "code": 418, "path": "/test/:" } ] } }
```

- `error.code`: common code (below) or an API-specific code.
- `error.errors`: optional per-file details (`code` plus `path`/`name`).
- Binary endpoints (Thumb, Download) return raw bytes on success and an HTTP status (e.g. 404) on failure.

## 4. Common error codes

### All WebAPIs

| Code | Meaning |
| --- | --- |
| 100 | Unknown error |
| 101 | No parameter of API, method or version |
| 102 | Requested API does not exist |
| 103 | Requested method does not exist |
| 104 | Requested version does not support the functionality |
| 105 | Logged-in session does not have permission |
| 106 | Session timeout |
| 107 | Session interrupted by duplicate login |
| 119 | SID not found |

### All File Station APIs (file operations)

| Code | Meaning |
| --- | --- |
| 400 | Invalid parameter of file operation |
| 401 | Unknown error of file operation |
| 402 | System is too busy |
| 403 | Invalid user does this file operation |
| 404 | Invalid group does this file operation |
| 405 | Invalid user and group does this file operation |
| 406 | Can't get user/group information from the account server |
| 407 | Operation not permitted |
| 408 | No such file or directory |
| 409 | Non-supported file system |
| 410 | Failed to connect internet-based file system (e.g., CIFS) |
| 411 | Read-only file system |
| 412 | Filename too long in the non-encrypted file system |
| 413 | Filename too long in the encrypted file system |
| 414 | File already exists |
| 415 | Disk quota exceeded |
| 416 | No space left on device |
| 417 | Input/output error |
| 418 | Illegal name or path |
| 419 | Illegal file name |
| 420 | Illegal file name on FAT file system |
| 421 | Device or resource busy |
| 599 | No such task of the file operation |

Note: 400–404 overlap with `SYNO.API.Auth`-specific codes; interpret by which API returned them.

---

## 5. Base APIs

### SYNO.API.Info (v1, `query.cgi`) — p.16

| Method | Params | Returns |
| --- | --- | --- |
| `query` | `query` = comma-separated API names, or `all` (prefixes like `SYNO.FileStation` also work) | `{ "<API>": { path, minVersion, maxVersion } }` |

### SYNO.API.Auth (v1–3+, `auth.cgi`) — p.18

**login**

| Param | Notes | Ver |
| --- | --- | --- |
| `account` | Login account | 1+ |
| `passwd` | Password (not escaped) | 1+ |
| `session` | Session name — use `FileStation` | 1+ |
| `format` | `cookie` (default; sets `id` cookie) or `sid` (returns SID in JSON only) | 2+ |
| `otp_code` | 2-step verification code (documented as reserved) | 3+ |

Response: `{ "sid": "..." }`. SID expires after 7 days by default.

**logout** — `session` (name). Empty success response.

| Code | Auth-specific error |
| --- | --- |
| 400 | No such account or incorrect password |
| 401 | Account disabled |
| 402 | Permission denied |
| 403 | 2-step verification code required |
| 404 | Failed to authenticate 2-step verification code |

---

## 6. File Station APIs

All require a login with `session=FileStation`. Paths always start with a shared folder (e.g. `/video/sub/file.txt`).

### Shared object definitions

**`additional` options** (request) → fields under `additional` (response):

| Option | Response field |
| --- | --- |
| `real_path` | `real_path` — volume path, e.g. `/volume1/video/1` |
| `size` | `size` — bytes |
| `owner` | `owner` — `{ user, group, uid, gid }` |
| `time` | `time` — `{ atime, mtime, ctime, crtime }` (Unix seconds) |
| `perm` | `perm` — see below |
| `mount_point_type` | `mount_point_type` — virtual FS type of a mount point |
| `type` | `type` — file extension, uppercased (e.g. `TXT`) |
| `volume_status` | `volume_status` — `{ freespace, totalspace, readonly }` (shares/virtual folders only) |

**`<file>` object**: `path`, `name`, `isdir`, `children` (only with `goto_path`; `{ total, offset, files }`), `additional`.

**`<perm>` (files)**: `posix` (int, e.g. 777), `is_acl_mode` (bool), `acl` `{ append, del, exec, read, write }`.

**`<shared-folder perm>`**: `share_right` (`RW`/`RO`), `posix`, `adv_right` `{ disable_download, disable_list, disable_modify }`, `acl_enable`, `is_acl_mode`, `acl`.

**Sort fields** (`sort_by`): `name`, `size`, `user`, `group`, `mtime`, `atime`, `ctime`, `crtime`, `posix`, `type` (subset varies per method). `sort_direction`: `asc` (default) / `desc`.

### Non-blocking task pattern

Search, DirSize, MD5, CopyMove, Delete (`start`), Extract and Compress are asynchronous:
`start` → returns `taskid` → poll `status` (or `list` for Search) until `finished: true` → optionally `stop`.
Search additionally requires `clean` to remove its temp DB. Running/finished tasks are visible via `SYNO.FileStation.BackgroundTask`.

---

### SYNO.FileStation.Info (v2) — p.21

| Method | Params | Response |
| --- | --- | --- |
| `get` | — | `is_manager`, `support_virtual_protocol` (e.g. `cifs,nfs,iso`), `support_sharing`, `hostname` |

### SYNO.FileStation.List (v2) — p.23

**list_share** — list shared folders

| Param | Default | Notes |
| --- | --- | --- |
| `offset` | 0 | |
| `limit` | 0 | 0 = all |
| `sort_by` | `name` | name, user, group, mtime, atime, ctime, crtime, posix |
| `sort_direction` | `asc` | |
| `onlywritable` | false | true = only writable shares |
| `additional` | — | real_path, size, owner, time, perm, mount_point_type, sync_share, volume_status |

Response: `{ total, offset, shares: [<shared folder>] }`.

**list** — enumerate a folder

| Param | Default | Notes |
| --- | --- | --- |
| `folder_path` | **required** | |
| `offset` / `limit` | 0 / 0 | limit 0 = all |
| `sort_by` | `name` | adds `size`, `type` |
| `sort_direction` | `asc` | |
| `pattern` | — | Case-insensitive glob(s), comma-separated; no `?`/`*` ⇒ wrapped as `*pattern*` |
| `filetype` | `all` | `file`, `dir`, `all` |
| `goto_path` | — | Returns tree from `folder_path` down to `goto_path` (requires `additional` to include `real_path`) |
| `additional` | — | real_path, size, owner, time, perm, type, mount_point_type |

Response: `{ total, offset, files: [<file>] }`.

**getinfo** — `path` (one or more), `additional`. Response: `{ files: [<file>] }`.

### SYNO.FileStation.Search (v2) — p.39

**start** — all given criteria are ANDed

| Param | Notes |
| --- | --- |
| `folder_path` | **required**; one or more folders |
| `recursive` | default true |
| `pattern` | Glob(s) on name; space-separated for multiple |
| `extension` | Glob(s) on extension, comma-separated; excludes folders |
| `filetype` | `file`/`dir`/`all` (default all) |
| `size_from` / `size_to` | bytes |
| `mtime_from`/`mtime_to`, `crtime_from`/`crtime_to`, `atime_from`/`atime_to` | Unix seconds |
| `owner`, `group` | case-insensitive |

Response: `{ taskid }`.

**list** — `taskid` (required), `offset`, `limit` (default 0 = **nothing**; use `-1` for all), `sort_by`, `sort_direction`, `pattern`, `filetype`, `additional` (real_path, size, owner, time, perm, type).
Response: `{ total, offset, finished, files: [<file>] }`.

**stop** — `taskid` (one or more). Temp DB is kept; `list` still works.
**clean** — `taskid` (one or more). Deletes the temp DB. Always call when done.

### SYNO.FileStation.VirtualFolder (v2) — p.47

**list** — `type` (`nfs`, `cifs`, `iso`; required), `offset`, `limit`, `sort_by`, `sort_direction`, `additional` (real_path, owner, time, perm, mount_point_type, volume_status).
Response: `{ total, offset, folders: [{ path, name, additional }] }`.

### SYNO.FileStation.Favorite (v2) — p.51

| Method | Params | Notes |
| --- | --- | --- |
| `list` | `offset`, `limit`, `status_filter` (`valid`/`broken`/`all`), `additional` (real_path, owner, time, perm, mount_point_type) | → `{ total, offset, favorites: [{ path, name, status, additional }] }` |
| `add` | `path`, `name`, `index` (default -1 = append) | |
| `delete` | `path` | |
| `clear_broken` | — | Remove all broken favorites |
| `edit` | `path`, `name` | Rename favorite |
| `replace_all` | `path` (list), `name` (list) | Same length; replaces all favorites |

Errors: 800 path already a favorite · 801 name conflicts with existing favorite · 802 too many favorites.

### SYNO.FileStation.Thumb (v2) — p.57

**get** — `path` (required), `size` (`small` default, `medium`, `large`, `original`), `rotate` (0–4 = 0°/90°/180°/270°/360°).
Returns image bytes; errors are HTTP status codes (e.g. 404).
Images: jpg, jpeg, jpe, bmp, png, tif, tiff, gif, heic and many RAW formats. Video thumbnails only exist for indexed folders (`photo` share or user homes).

### SYNO.FileStation.DirSize (v2) — p.59

| Method | Params | Response |
| --- | --- | --- |
| `start` | `path` (one or more) | `{ taskid }` |
| `status` | `taskid` | `{ finished, num_dir, num_file, total_size }` |
| `stop` | `taskid` | empty |

### SYNO.FileStation.MD5 (v2) — p.62

| Method | Params | Response |
| --- | --- | --- |
| `start` | `file_path` | `{ taskid }` |
| `status` | `taskid` | `{ finished, md5 }` |
| `stop` | `taskid` | empty |

### SYNO.FileStation.CheckPermission (v3) — p.65

**write** — `path` (folder), `filename`, `overwrite` (bool; unset ⇒ error if exists), `create_only` (default true).
Success = permitted; error response = no write permission.

### SYNO.FileStation.Upload (v2/v3) — p.67

**upload** — `POST` `multipart/form-data` (RFC 1867). `api`, `version`, `method` and all params go in form parts; **the file part must be last**.

| Param | Notes |
| --- | --- |
| `path` | Destination folder |
| `create_parents` | Create missing parent folders |
| `overwrite` | v2: `true`/`false`; v3: `overwrite`/`skip`. Unset ⇒ error 1805 if file exists |
| `mtime`, `crtime`, `atime` | Unix **milliseconds** |
| `file` (with `filename`) | File content, last part |

Errors: 1800 Content-Length missing/mismatch · 1801 timeout waiting for data (3600 s) · 1802 no filename in last part · 1803 upload cancelled · 1804 oversized file on FAT · 1805 file exists and no `overwrite` given.

### SYNO.FileStation.Download (v2) — p.71

**download** — `path` (one or more; multiple ⇒ ZIP), `mode` (`open` default → MIME by extension; `download` → `application/octet-stream` + attachment).
Returns file bytes. With `mode=open`, errors come back as HTTP 404.

### SYNO.FileStation.Sharing (v3) — p.73

| Method | Params | Response |
| --- | --- | --- |
| `getinfo` | `id` | `<Sharing_Link>` |
| `list` | `offset`, `limit`, `sort_by` (id, name, isFolder, path, date_expired, date_available, status, has_password, url, link_owner), `sort_direction`, `force_clean` (bool; true = resync, slow) | `{ total, offset, links: [<Sharing_Link>] }` |
| `create` | `path` (one or more), `password` (max 16 chars), `date_expired`, `date_available` (`"YYYY-MM-DD"`, quoted; 0 = none) | `{ links: [{ path, url, id, qrcode (base64), error }] }` |
| `delete` | `id` (one or more) | empty, or error array of failed IDs |
| `clear_invalid` | — | Removes expired and broken links |
| `edit` | `id` (one or more), `password` (empty string removes), `date_expired`, `date_available` | empty |

`<Sharing_Link>`: `id`, `url`, `link_owner`, `path`, `isFolder`, `has_password`, `date_expired`, `date_available`, `status` (`valid`, `invalid` = not yet available, `expired`, `broken`). Dates are in the DiskStation's local time.

Errors: 2000 link does not exist · 2001 too many sharing links · 2002 failed to access sharing links.

### SYNO.FileStation.CreateFolder (v2) — p.80

**create** — `folder_path` (list), `name` (list, same length, paired by index), `force_parent` (default false; true = mkdir -p, no error if exists), `additional`.
Response: `{ folders: [<file>] }`.
Errors: 1100 failed to create (see `errors`) · 1101 too many folders in parent.

### SYNO.FileStation.Rename (v2) — p.83

**rename** — `path` (list), `name` (list, same length), `additional`, `search_taskid` (updates search results).
Response: `{ files: [<file>] }`. Error: 1200 failed to rename (see `errors`).

### SYNO.FileStation.CopyMove (v3) — p.86

**start**

| Param | Default | Notes |
| --- | --- | --- |
| `path` | required | One or more sources |
| `dest_folder_path` | required | |
| `overwrite` | (none) | true = overwrite, false = skip; unset ⇒ error 1003 on conflict |
| `remove_src` | false | true = move, false = copy |
| `accurate_progress` | true | Progress per file including subfolders |
| `search_taskid` | — | Update search results |

**status** — `taskid` → `{ processed_size, total (-1 while calculating), path, finished, progress (0–1), dest_folder_path }`.
**stop** — `taskid`.

Errors: 1000 copy failed · 1001 move failed · 1002 destination error · 1003 conflict and no `overwrite` · 1004 file/folder type conflict · 1006 special characters on FAT32 · 1007 file > 4 GB on FAT32.

### SYNO.FileStation.Delete (v2) — p.90

**start** (non-blocking) — `path` (one or more), `accurate_progress` (default true), `recursive` (default true; false fails on non-empty folders), `search_taskid`. → `{ taskid }`.
**status** — `taskid` → `{ processed_num, total (-1 while calculating), path, processing_path, finished, progress }`.
**stop** — `taskid`.
**delete** (blocking) — `path`, `recursive`, `search_taskid`. Returns when done.

Error: 900 failed to delete (see `errors`).

### SYNO.FileStation.Extract (v2) — p.95

Supported archives: zip, gz, tar, tgz, tbz, bz2, rar, 7z, iso.

**start**

| Param | Default | Notes |
| --- | --- | --- |
| `file_path` | required | Archive |
| `dest_folder_path` | required | |
| `overwrite` | false | |
| `keep_dir` | true | Keep the archive's folder structure |
| `create_subfolder` | false | Extract into a subfolder named after the archive |
| `codepage` | DSM setting | enu, cht, chs, krn, ger, fre, ita, spn, jpn, dan, nor, sve, nld, rus, plk, ptb, ptg, hun, trk, csy |
| `password` | — | |
| `item_id` | — | Extract only these items (IDs from `list`) |

**status** — `taskid` → `{ finished, progress, dest_folder_path }`.
**stop** — `taskid`.
**list** — `file_path`, `offset`, `limit` (default -1 = all), `sort_by` (name, size, pack_size, mtime), `sort_direction`, `codepage`, `password`, `item_id` (folder within archive; -1 = root).
→ `{ total, items: [{ item_id, name, size, pack_size, mtime ("YYYY-MM-DD HH:MM:SS"), path, is_dir }] }`.

Errors: 1400 extract failed · 1401 not an archive · 1402 read error · 1403 wrong password · 1404 failed to list archive · 1405 item ID not found.

### SYNO.FileStation.Compress (v3) — p.102

**start**

| Param | Default | Notes |
| --- | --- | --- |
| `path` | required | One or more sources |
| `dest_file_path` | required | Full archive path including name |
| `level` | `moderate` | `store`, `fastest`, `moderate`, `best` |
| `mode` | `add` | `add`, `update`, `refreshen`, `synchronize` |
| `format` | `zip` | `zip` or `7z` |
| `password` | — | |

**status** — `taskid` → `{ finished, dest_file_path }`.
**stop** — `taskid`.

Errors: 1300 compress failed · 1301 archive name too long.

### SYNO.FileStation.BackgroundTask (v3) — p.106

**list** — `offset`, `limit`, `sort_by` (`crtime` default, `finished`), `sort_direction`, `api_filter` (list of CopyMove/Delete/Extract/Compress API names).
→ `{ total, offset, tasks: [{ api, version, method, taskid, crtime, finished, params, path, processed_num, processed_size, processing_path, total (-1 if unsupported), progress (0 if unsupported) }] }`.

**clear_finished** — `taskid` (optional list; omitted = clear all finished).

---

## 7. Errata and gotchas in the PDF

The guide has a number of copy-paste errors. Trust the parameter tables over the example URLs:

- Examples on pp.8–9 use made-up CGI paths (`entryFilStation/info.cgi`, `FilStation/info.cgi`); several others use `/webapi/FileStation/entry.cgi`. The real path comes from `SYNO.API.Info` (normally `entry.cgi`).
- Several examples use `version=1` (Favorite list/replace_all, Search stop, CopyMove stop, Delete blocking `delete`, Compress stop), although those APIs are documented as v2/v3. Use the version from `SYNO.API.Info` (`maxVersion`).
- Response example on p.9 shows `support_virtual`; the field table (p.21) says `support_virtual_protocol`.
- DirSize `stop` lists the param as `tasked`; it is `taskid`.
- Extract `status` example uses `api=SYNO.FileStation.Compress`; it should be `SYNO.FileStation.Extract`.
- Extract `list` example uses `sortby`; the parameter is `sort_by`. The item field is `item_id` in the example but `itemid` in the table.
- Sharing `create` example is missing `=` after `date_expired`; the `edit` table types `id` as Integer although IDs are strings.
- Compress `start` example URL-encodes `=` and `&` (`method%3Dstart`, `%26format%3Dzip`); those must be literal.
- Rename example has a malformed encoding (`%5B%%222F...`).
- Search `pattern` uses **space** to separate globs, while List `pattern` and Search `extension` use **comma**.
- Search `list` default `limit=0` returns no rows; pass `limit=-1` (or a page size).
- `Favorite` `list` table shows the sort-field enum under `additional`; the valid `additional` values are the ones described in its text (real_path, owner, time, perm, mount_point_type).
- **Observed on DSM 7 (2026-10, this project's NAS):** Upload v3 with `overwrite` omitted and an existing target returns **414** (common "File already exists"), not 1805. Handle both.
- **Observed on DSM 7:** `FileStation.Info.get` returns `support_virtual_protocol` as a JSON array (e.g. `[]`), not a comma-separated string.
- **Observed on DSM 7:** `SYNO.FileStation.Compress` returns **105** for a non-admin user whose session came from an Auth **v3 or v6** login, with any session name, cookie or `_sid`, SynoToken or not, and any Compress version 1–3. Logging in with **Auth v7** fixes it. The DSM 7 web UI sends Compress with every string JSON-quoted, `level="normal"`, `mode="replace"` (neither is in the guide), `password=null` and `codepage="enu"`; the PoC mirrors that.
- **Observed on DSM 7:** `Extract.list` returns **one folder level per call**: root items, then pass a folder's `item_id` to list inside it. Items are not ordered by `item_id`. Inside a subfolder the result also contains a parent entry `{"name": "..", "item_id": -1, "path": "root", "is_dir": true}` with no `size`; skip it.
- **Observed on DSM 7:** finished Compress/Extract tasks may already be gone from `BackgroundTask.list` once their `status` has reported `finished` (`total: 0`). Don't rely on it for history.
- **Observed on DSM 7:** `Sharing` reports a link with no expiry as `date_expired: ""` (not `"0"`). After `edit` with `date_expired="YYYY-MM-DD"`, it reads back as `"YYYY-MM-DD 23:59:59"`.
- **Observed on DSM 7:** with QuickConnect enabled, `Sharing.create` returns URLs on `https://gofile.me/<qc-id>/<link-id>`, which are reachable from the **internet** via Synology's relay, not only the LAN.
- **Observed on DSM 7:** `Thumb.get` keeps the source format (PNG in, PNG out). Sizes for a 640×480 source: small 120×90, medium 360×270, large = original (not upscaled). `rotate=1` swaps width and height.
- Upload/Search/etc. timestamps differ: Upload uses **milliseconds**; Search filters and `time` objects use **seconds**.
