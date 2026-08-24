# Secure MCP Tunnel setup

This guide covers the Bridge-specific path. The official [Secure MCP Tunnel documentation](https://github.com/openai/tunnel-client) is authoritative when commands or permissions change.

## 1. Prepare the Bridge

```bash
git clone https://github.com/AaAndrew233/chatgpt-codex-bridge.git
cd chatgpt-codex-bridge
./scripts/bootstrap.sh
.venv/bin/python -m unittest discover -s tests -v
```

Review `config.json`. Keep `model` as `null` to use the existing Codex model configuration.

## 2. Install the official client

```bash
brew install openai/tools/tunnel-client
tunnel-client --version
tunnel-client help quickstart
```

## 3. Obtain the required values

- Create or select a Tunnel in [OpenAI Platform Tunnel settings](https://platform.openai.com/settings/organization/tunnels).
- Create a Runtime API Key in [OpenAI Platform API keys](https://platform.openai.com/settings/organization/api-keys).
- Ensure the runtime principal has Tunnels Read and Use permissions.

An Admin Key is only for administrative tunnel creation and management. Do not use it for the daemon.

## 4. Store the Runtime Key safely

Write the key to a file outside the repository using your normal secret-management process. Then restrict access:

```bash
chmod 600 /ABSOLUTE/PATH/TO/runtime-key
```

Never paste the real value into documentation, shell history, issue reports, logs, or repository files.

## 5. Create the managed runtime

```bash
tunnel-client runtimes connect \
  --alias codex-bridge \
  --profile codex-bridge \
  --tunnel-id '<YOUR_TUNNEL_ID>' \
  --runtime-api-key 'file:/ABSOLUTE/PATH/TO/runtime-key' \
  --mcp-command '/ABSOLUTE/PATH/TO/chatgpt-codex-bridge/scripts/run_server.sh'
```

Managed runtime supervision is preferred over `nohup`, `disown`, or keeping a Terminal window open.

## 6. Verify before connecting ChatGPT

```bash
tunnel-client runtimes status codex-bridge --json
```

Only continue when `process_running`, `healthy`, and `ready` are true. If setup fails, run the official diagnostics:

```bash
tunnel-client doctor --profile codex-bridge --explain
```

## 7. Configure ChatGPT

Open [ChatGPT connector settings](https://chatgpt.com/#settings/Connectors), create or refresh the connector for the Tunnel, and enable it in a new conversation. Tool discovery may be cached; after a Bridge upgrade, disconnect and reconnect the connector, then start a new ChatGPT conversation.

## 8. Acceptance test

Ask ChatGPT to call `codex_status` and return only health, tool names, and registered project names. Then submit a read-only `codex_analyze` task and require ChatGPT to poll the job and retrieve every output page.

Do not test the write flow until the read-only path is healthy and the project list contains only intended directories.
