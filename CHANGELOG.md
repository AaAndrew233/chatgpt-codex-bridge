# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0.0] - 2026-08-24

### Added

- Initial public release of the local-first ChatGPT to Codex MCP bridge.
- Project discovery, bounded session history, background jobs, and paginated output.
- Read-only analysis and planning with confirmation-gated workspace writes.
- Persistent Codex Desktop session creation, continuation, and explicit ChatGPT context handoff.
- Secure MCP Tunnel setup documentation, public-release checks, and continuous integration.

### Fixed

- Restricted session listing and reading to authorized project roots.
- Prevented external requests from being interpreted as internal job envelopes.
- Reserved background-job capacity before creating persistent Desktop sessions.
- Removed session content from the default health-status response.
