# Security policy

## Supported versions

Security fixes are provided for the latest release on the default branch.

## Report a vulnerability privately

Do not open a public issue, discussion, or pull request for a suspected vulnerability.

Use GitHub's **Report a vulnerability** form in the repository Security tab. Include:

- the affected version or commit,
- a concrete reproduction or exploit path,
- the expected and actual security boundary,
- potential impact,
- any suggested mitigation.

Do not include real API keys, tokens, cookies, private source code, session exports, or personal data. Use synthetic values and the smallest possible reproduction.

## Response process

The maintainers will acknowledge a report through the private advisory, validate the impact, prepare a fix, and coordinate disclosure. No response-time guarantee is offered for this community project.

## Scope

In scope:

- project authorization bypass,
- write execution without a valid confirmation token,
- arbitrary shell execution through MCP arguments,
- credential or private-session disclosure,
- unsafe Tunnel or Desktop notification defaults,
- denial of service that bypasses configured limits.

Generally out of scope:

- vulnerabilities in OpenAI services, Codex, ChatGPT, or `tunnel-client`, which should be reported to their owners,
- attacks that already require control of the local operating-system account,
- unsupported modifications to the proprietary Codex Desktop application,
- social engineering without a technical boundary failure.

## Secret exposure

If a real credential is accidentally committed, revoke it immediately, rotate it, remove it from Git history, audit the exposure window, and review provider logs for abuse. Deleting only the current file is not sufficient.
