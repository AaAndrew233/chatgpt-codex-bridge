# Security model

## Intended deployment

The bridge is intended for a single developer workstation or a controlled team workstation. It runs as a local stdio MCP server behind the official OpenAI Secure MCP Tunnel client. Do not bind it directly to a public network interface.

## Assets protected

- Local source code and project files
- Codex project and session metadata
- Codex and OpenAI credentials already present on the workstation
- Runtime API keys and Tunnel configuration
- The user's authority to modify local projects

## Main controls

### Project authorization

Project paths must resolve under either:

- a validated project registered in Codex Desktop, or
- a narrow directory explicitly listed in `allowed_roots`.

Automatic discovery rejects the filesystem root, the user's home directory, and common credential/configuration directories. Symlinks are resolved before authorization decisions.

### Execution boundary

The bridge constructs argument arrays and uses `asyncio.create_subprocess_exec`; it does not interpolate caller input into a shell command. Read operations use the Codex `read-only` sandbox. Writes use `workspace-write` only after token validation.

### Write confirmation

A confirmation token is:

- generated with a cryptographically secure random source,
- stored only in memory,
- valid for one use,
- short-lived,
- bound to the resolved project path and exact request body.

The token proves only that the bridge issued it. The MCP client must still show the plan to the user and obtain explicit approval before calling a write tool.

### Data minimization

- Session APIs return filtered user-visible messages rather than raw JSONL records.
- Sensitive strings are redacted before results leave the bridge.
- Project-history scanning is streamed and bounded by count, bytes, and output size.
- Job results expire from process memory.
- Logs contain operation metadata, not request bodies or credentials.

### Prompt injection handling

Session history, project files, and ChatGPT-provided context are untrusted data. The bridge labels handed-off context as untrusted reference text. This reduces instruction confusion but is not a complete defense against malicious repository content. Users should review write plans carefully.

## Known residual risks

- A user-approved Codex write can still make harmful changes inside the authorized workspace.
- Read-only analysis can expose source code to the configured model provider according to the user's existing Codex configuration.
- Redaction uses high-signal patterns and may not detect every proprietary secret format.
- A compromised workstation account can access local files independently of this bridge.
- The optional Desktop sidebar extension is unsupported and may break after Codex updates.
- Tunnel availability, organization permissions, and connector behavior are controlled by external OpenAI services.

## Deployment checklist

- Keep Runtime Keys outside the repository in files with mode `0600`.
- Use a Runtime Key for the daemon, never an Admin Key.
- Keep `allowed_roots` narrow and review `codex_list_projects` before team use.
- Leave `model` as `null` unless the team intentionally pins a supported model.
- Keep the bridge and `tunnel-client` updated from verified sources.
- Run `./scripts/check_public_release.py` before every public release.
- Review the exact request before approving a write token.
- Stop or disconnect the managed Tunnel runtime when remote access is no longer needed.

## Reporting vulnerabilities

Do not open a public issue for a suspected vulnerability. Follow the private reporting process in [SECURITY.md](../SECURITY.md).
