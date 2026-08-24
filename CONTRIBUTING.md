# Contributing

Contributions are welcome when they preserve the bridge's narrow security boundary and remain testable without real credentials or private Codex data.

## Before opening a change

- Search existing issues and pull requests.
- Open an issue first for new tools, authorization changes, public network listeners, new persistent storage, or protocol changes.
- Keep changes focused. Avoid unrelated refactors or dependency additions.

## Local setup

```bash
git clone https://github.com/AaAndrew233/chatgpt-codex-bridge.git
cd chatgpt-codex-bridge
./scripts/bootstrap.sh
```

## Required checks

```bash
./scripts/check_public_release.py
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q \
  bridge_core.py conversation_catalog.py desktop_assignment.py \
  desktop_sessions.py project_context.py server.py
sh -n scripts/bootstrap.sh
sh -n scripts/run_server.sh
```

Tests must use temporary directories and synthetic credentials. Never attach real logs, configurations, session exports, project paths, tunnel profiles, or keys to an issue or pull request.

## Pull requests

A pull request should include:

- the user-visible problem and outcome,
- the security and compatibility impact,
- tests for success, failure, and boundary conditions,
- documentation updates when behavior or configuration changes,
- confirmation that the public-release check passes.

New dependencies require a reason, a license check, and a supply-chain review. External commands must use argument arrays rather than a shell. New read or write capabilities must be bounded by project authorization and explicit limits.

By contributing, you agree that your contribution is licensed under Apache License 2.0.
