# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.3.1] - 2026-09-09

### Fixed

- Made the stdio MCP smoke test deterministic across CI operating systems by running test subprocesses unbuffered and reporting captured diagnostics on failure.

## [0.3.0] - 2026-09-09

### Added

- Repositioned the product as **Codex with ChatGPT**, while keeping the existing repository and Runtime aliases compatible.
- Added explicit execution protocol states to background jobs so ChatGPT can distinguish queued, running, completed, failed, and cancelled work.
- Added bridge version, tool schema version, and connector maintenance guidance to `codex_status`.
- Added an update and maintenance guide covering Runtime restarts, connector metadata refresh, and new-conversation boundaries.

### Security

- Kept project, session, and exact-request write authorization unchanged while exposing only maintenance metadata in status responses.

## [0.2.0] - 2026-08-24

### Added

- Session-scoped, time-limited authorization for projectless Codex Desktop conversations in the Recent section.
- Metadata-only discovery for unassigned sessions and a confirmation-gated `codex_prepare_session_access` flow.

### Security

- Bound session grants to one sidebar-visible session, its canonical working directory, access mode, and expiration time.
- Kept exact-request write confirmation mandatory in addition to session-level workspace access.
- Enforced the selected working directory, approval policy, and sandbox policy on every resumed app-server turn.

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
