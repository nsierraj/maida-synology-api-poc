# Changelog

All notable changes are recorded here, in the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.
The project follows [Semantic Versioning](https://semver.org/); while it is 0.x, minor releases may change behavior.

## [Unreleased]

## [0.1.0] - 2026-10-07

First release.

### Added
- `synology_poc` library: `SynologyClient` (discovery, Auth v7 login, re-login on expired sessions, uploads, downloads, async-job polling) and `FileStation`, with one method per verified use case (UC-01…UC-20).
- `PathPolicy`, which keeps writes and share links inside allowed roots.
- Examples `01`–`05`: discovery/login, browsing, file lifecycle, async jobs, sharing and thumbnails. Only `03`–`05` write, and only inside `SYNO_SANDBOX`.
- `synology_mcp` MCP server (`synology-mcp`): read tools always on, write and share tools behind `SYNO_MCP_*` flags, plus 3 prompts.
- Test suite against an in-memory fake NAS that reproduces the observed DSM 7 quirks.
- Documentation: a condensed File Station API reference with DSM 7 errata, verified use cases, MCP server guide and an MCP explainer for web developers.
- GitHub Actions CI (pytest on Python 3.11–3.13) and Dependabot for actions and `uv`.

[Unreleased]: https://github.com/nsierraj/maida-synology-api-poc/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/nsierraj/maida-synology-api-poc/releases/tag/v0.1.0
