# maida-synology-api-poc

Python library, examples and MCP server for the Synology DSM 7 File Station Web API, verified against the user's real NAS.

## Commands

- `uv sync`: install (Python ≥ 3.11, deps in `pyproject.toml` / `uv.lock`)
- `uv run pytest`: the whole suite, against the fake NAS. Must pass before any commit. CI runs it on Python 3.11–3.13.
- `uv run examples/0N_*.py`: real-NAS runs. They need `.env` and are run by the user. Only 03–05 write, and only inside `SYNO_SANDBOX`.
- `uv run synology-mcp`: the MCP server (stdio). It reads the same `.env`; `SYNO_MCP_*` flags enable writes and sharing.
- `uv run python scripts/capture_mcp_wire.py`: regenerates `docs/mcp-wire-sample.md`.

## Code

- `src/synology_poc/`
  - `client.py`: `SynologyClient`
  - `filestation.py`: `FileStation`, one method per use case. All DSM 7 wire-format knowledge lives here.
  - `policy.py`: `PathPolicy`
  - `errors.py`: error code tables
  - `config.py`: `.env` loading
  - `sandbox.py`: helpers for the examples
- `src/synology_mcp/server.py`: the MCP server: read tools always on, write and share tools only when enabled, plus 3 prompts.
- `examples/01–05`: discovery/login, browsing, file lifecycle, async jobs, sharing + thumbnails.
- `tests/`: `fake_nas.py` reproduces the observed DSM 7 quirks. When you learn a new one, add it to the fake and add a test.
- `scripts/capture_mcp_wire.py`: records real MCP traffic (server + fake NAS) for the docs.

## Rules that aren't obvious from the code

- **All changes go through a pull request**, never a direct push to `main`. CI (`.github/workflows/ci.yml`) must be green. Workflows must not use secrets, `.env` or a real NAS.
- **DSM 7 differs from the PDF guide.** When they disagree, trust `docs/synology/file-station-api.md` §7 (errata) and `docs/use-cases.md`. If an operation fails, capture the DSM web UI's request (browser DevTools) rather than guessing: that's how the Compress parameters were found.
- **Keep Auth v7** (`client.DOC_VERSIONS`). On this NAS, sessions from Auth v3/v6 get 105 on Compress.
- **The fake NAS accepts raw and JSON-quoted strings alike**, so passing tests can't prove the parameter encoding. New NAS calls need a real-NAS run (an example script inside the sandbox) before they're exposed as MCP tools.
- **Order for new NAS operations:** `FileStation` method + fake + test → real-NAS example → MCP tool → docs (`use-cases.md`, `mcp-server.md`).
- **Nothing in `synology_mcp` or the library may print to stdout**; with stdio, stdout is the protocol channel.
- **`mcp` is 2.x:** `FastMCP` is now `mcp.server.mcpserver.MCPServer`, and annotations are `mcp_types.ToolAnnotations` (snake_case). Don't follow 1.x examples.
- **Never commit `.env`, `certs/` or `out/`** (all gitignored). The repo is public: never commit the proprietary Synology PDF or anything NAS-specific.
- **Don't put the real NAS's IP, hostname, SIDs or passwords in docs or samples**; use the fake NAS (`fakenas`).

## Repo workflow

- The repo is public (`nsierraj/maida-synology-api-poc`, default branch `main`).
- **Branch protection on `main`:** changes need a PR and green checks `test (py3.11)`, `test (py3.12)`, `test (py3.13)`. Force-push and deletion are blocked. Admins are not enforced, so don't push to `main` directly anyway.
- Work on a branch, open a PR, wait for CI (`gh pr checks --watch`), then squash-merge with `gh pr merge --squash --delete-branch`.
- **CI** (`.github/workflows/ci.yml`): `uv sync --locked` then `uv run pytest`, on pull requests and pushes to `main`. Pin actions to a full version tag; some, like `astral-sh/setup-uv`, have no floating major tag. If `uv.lock` is out of date, CI fails; run `uv lock`.
- **Dependabot** (`.github/dependabot.yml`) opens weekly PRs for GitHub Actions and `uv` dependencies. Review them like any other PR.

## Releases

- SemVer; the version lives in `pyproject.toml`. Record changes under `[Unreleased]` in `CHANGELOG.md` in the same PR as the change.
- To release: a PR moves `[Unreleased]` to a dated `[X.Y.Z]` section, bumps `pyproject.toml` and runs `uv lock`. After it merges, tag `main` (`git tag -a vX.Y.Z -m vX.Y.Z && git push origin vX.Y.Z`) and run `gh release create vX.Y.Z` with that section as the notes.
- Nothing is published to PyPI.

## Docs

- `docs/mcp-explained.md`: plain-language MCP explainer for REST/web developers. Its JSON excerpts were copied by hand from `docs/mcp-wire-sample.md`. After changing tools or prompts, regenerate the sample, then update the excerpts and tool counts. A shareable web version is the private artifact <https://claude.ai/artifact/SRiQJJYYa8783aErNyu2Rn>, built from the same content; republish it alongside.
- `docs/use-cases.md`: every verified use case (UC-01…UC-20): calls, encoding, quirks, library method, MCP tool.
- `docs/mcp-server.md`: MCP server setup, tools, prompts, safety model, troubleshooting, how to add a tool.

## API reference

- `docs/synology/file-station-api.md`: condensed reference (methods, params, error codes) plus the DSM 7 errata in §7. Read this first.
- The official guide (rev. 2023.03) is not stored in the repo (proprietary). Download it from <https://global.download.synology.com/download/Document/Software/DeveloperGuide/Package/FileStation/All/enu/Synology_File_Station_API_Guide.pdf>. It's the base spec, but the errata override it for DSM 7.
