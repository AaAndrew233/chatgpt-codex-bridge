# Codex Desktop sidebar behavior

Persistent sessions created through `codex_create_desktop_session` are written through the Codex app-server protocol and remain available to Codex after the Bridge process exits.

The Codex Desktop sidebar also maintains process-local project assignment and task caches. Updating those caches while the App is already running requires an unsupported local extension. That extension is not part of this repository because it modifies proprietary application code and can be overwritten by an App update.

## Without the extension

- Persistent session creation and continuation still work.
- `codex_list_sessions` and `codex_read_session` can discover persisted state.
- A newly created session may appear only after Codex Desktop restarts.
- The Bridge reports `sidebar_sync.state=desktop-unavailable`; this is a degraded display state, not necessarily a failed session creation.

## With a compatible local extension

`desktop_assignment.py` can notify a Unix socket at `~/.codex/codex-bridge/assignment.sock`. The client requires a token file owned by the current user with no group or other permissions and sends only a thread ID plus local project assignment.

This interface is experimental. It is intentionally fail-closed for invalid token ownership or permissions and fail-soft for Bridge session creation: notification failure is reported but does not delete persistent Codex state.

## Upgrade warning

Codex Desktop updates can remove or break an injected extension. Reinstall the official App to return to a supported state. Do not distribute patched application binaries through this repository, and do not edit Codex global state files directly to force a sidebar refresh.
