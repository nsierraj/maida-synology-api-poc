# maida-synology-api-poc

Proof of concept against the Synology DSM File Station Web API.

## Code

- `README.md` — NAS setup, TLS pinning, how to run the examples.
- `src/synology_poc/` — `SynologyClient` (client.py), error tables (errors.py), `.env` loading (config.py), sandbox helpers (sandbox.py).
- `examples/01–05` — discovery/login, read-only browsing, sandboxed file lifecycle, async jobs, sharing + thumbnails. Run with `uv run`.

## API reference

- `docs/synology/file-station-api.md` — condensed, searchable reference (methods, params, error codes, and known errata in the official guide). Read this first.
- `docs/synology/Synology_File_Station_API_Guide.pdf` — official Synology guide (rev. 2023.03), the source of truth.
