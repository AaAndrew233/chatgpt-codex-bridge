# Architecture

## Trust boundaries

The bridge separates four trust zones:

1. ChatGPT is a remote MCP client and all tool arguments are untrusted input.
2. `tunnel-client` owns the authenticated outbound connection to OpenAI Secure MCP Tunnel.
3. The bridge runs locally, validates project scope, and converts tool calls into bounded operations.
4. Codex CLI and Codex Desktop own model execution, sandbox enforcement, and persistent local session state.

The bridge never asks ChatGPT for a local credential and never exposes an arbitrary command-execution tool.

## Modules

| Module | Responsibility |
| --- | --- |
| `server.py` | MCP tools, request validation, orchestration, and tool annotations |
| `bridge_core.py` | Configuration, project authorization, confirmation tokens, CLI execution, and background jobs |
| `conversation_catalog.py` | Read-only discovery, pagination, filtering, and redaction of visible Codex sessions |
| `project_context.py` | Streaming, bounded project-history collection and coverage reporting |
| `desktop_sessions.py` | Persistent Codex app-server thread creation, resume, and turn lifecycle |
| `desktop_assignment.py` | Optional local Unix-socket notification for an unsupported Desktop extension |

## Request lifecycle

### Read-only analysis

1. ChatGPT calls `codex_analyze` with an authorized project and request.
2. The bridge validates the path and request size.
3. `JobStore` returns a job ID immediately.
4. A bounded worker starts `codex exec` with `--sandbox read-only`.
5. ChatGPT polls `codex_job_status` and reads all `codex_job_result` pages.

### Confirmed write

1. ChatGPT calls `codex_prepare_apply` for one exact project and request.
2. The user reviews the plan and explicitly approves it.
3. ChatGPT calls `codex_apply` with the unchanged request and one-time token.
4. The bridge consumes the token and starts `codex exec --sandbox workspace-write`.

Tokens are stored only in process memory, expire by default after five minutes, and cannot be replayed.

### Project-history context

`ProjectContextCollector` streams visible session files and maintains bounded memory. It reports source-scan completeness separately from output completeness:

- `scan_complete=false` means a configured source limit prevented a full scan.
- `context_complete=false` means the scan completed, but not all text fit in the output budget.

This distinction prevents a bounded summary from being represented as a complete export.

## State and failure behavior

- Local configuration is read at process startup. Codex project records are re-read on each relevant call.
- Background jobs are process-local and intentionally disappear after a bridge restart.
- Persistent Codex Desktop sessions remain in Codex-owned storage.
- Optional sidebar notification failure does not roll back a successfully created persistent session.
- External service failures are returned as sanitized bridge errors without raw credential-bearing responses.

## Non-goals

- Reading ChatGPT browser cookies, private APIs, or conversations by URL or conversation ID.
- Granting remote callers unrestricted access to the local filesystem or shell.
- Replacing Codex sandboxing, authentication, billing, or model-provider configuration.
- Shipping or modifying the proprietary Codex Desktop application.
