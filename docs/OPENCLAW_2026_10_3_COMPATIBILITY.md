# OpenClaw 2026.10.3 compatibility

Hotfix `v0.6.2` accepts the nested Gateway JSON run-stat layout emitted by
`openclaw agent --json` 2026.10.3. It remains fail-closed: a model reply is
usable only when its editorial JSON is valid and the Gateway attests that no
tool was available or called.

## Isolated-editor preflight

Do not edit an existing shared Gateway or the `main` agent for this check. Use
the isolated `waagd` configuration selected by `OPENCLAW_CONFIG_PATH`. Its tool
policy must contain both settings:

```json5
{
  tools: {
    toolSearch: false,
    deny: ["*"],
  },
}
```

`tools.toolSearch: false` prevents the Tool Search control surface from being
added. `tools.deny: ["*"]` is the authoritative deny rule; deny takes
precedence over profile and agent-level allow rules. Keep this configuration in
the isolated editor's config path, never in a shared Gateway configuration.

Before allowing a Discovery editorial invocation, run one synthetic,
non-publishing `openclaw agent --agent waagd --json` check and inspect the
returned data without logging its prompt or credentials. Discovery accepts the
2026.10.3 form only when the following nested fields are present and clean:

- `result.meta.agentMeta.bridgeCalls`,
  `result.meta.agentMeta.terminalReceipt`, and
  `result.meta.agentMeta.codeModeEngaged`;
- `result.meta.systemPromptReport.tools`; and
- `result.meta.toolSummary`, when it is emitted.

Their values must meet all of these conditions:

- `bridgeCalls.search`, `bridgeCalls.describe`, and `bridgeCalls.call` are all
  integer zero;
- `terminalReceipt.successfulToolNames` is an empty list, and any reported
  attempted or failed tool-name lists are also empty;
- `systemPromptReport.tools.entries` is an empty list, with `toolSearch: false`
  when that field is reported;
- `codeModeEngaged` is exactly `false`; and
- `toolSummary`, when emitted, reports zero calls and no tool names.

An absent required 2026.10.3 attestation is not interpreted as zero. A nonempty
`systemPromptReport.tools.entries` (for example `tool_search`,
`tool_describe`, or `tool_call`) means the effective policy is not tool-free and
Discovery rejects the response with `gateway_tools_exposed`, even if no call was
made. Correct the isolated configuration before retrying; do not weaken the
adapter or change the shared OpenClaw installation.

## Upgrade

From an isolated Discovery checkout, preserve the existing SQLite and private
Bridge state, then replace only the application checkout/image with `v0.6.2`.
Do not ACK, dismiss, reset, or migrate the protected pending delivery
`dbf6d7a2-395e-40c6-8e91-7866aab89e53`. Keep publication disabled for the
preflight. Roll back by stopping the new container and restoring the prior
application image against the preserved schema-4 volume; do not delete volumes
or rewrite Bridge state.
