# OpenClaw 2026.10.3 compatibility

Hotfix `v0.6.3` accepts both observed nested Gateway JSON run-stat layouts emitted by
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

- `result.meta.agentMeta.terminalReceipt` and
  `result.meta.agentMeta.codeModeEngaged`;
- `result.meta.systemPromptReport.tools`; and
- `result.meta.agentMeta.bridgeCalls` and `result.meta.toolSummary`, when they
  are emitted.

Their values must meet all of these conditions:

- when `bridgeCalls` is reported, it has exactly `search`, `describe`, and
  `call`, all integer zero; when it is absent, the remaining required
  tool-free attestations must still all be present and clean;
- `terminalReceipt.successfulToolNames` is an empty list, and any reported
  attempted or failed tool-name lists are also empty;
- `systemPromptReport.tools.entries` is an empty list, with `toolSearch: false`
  when that field is reported;
- `codeModeEngaged` is exactly `false`; and
- `toolSummary`, when emitted, reports zero calls and no tool names.

An absent `bridgeCalls` is never interpreted as zero: it is accepted only as the
separate, observed tool-free form after all other attestations validate. Any other
absent required 2026.10.3 attestation is not interpreted as zero. A nonempty
`systemPromptReport.tools.entries` (for example `tool_search`,
`tool_describe`, or `tool_call`) means the effective policy is not tool-free and
Discovery rejects the response with `gateway_tools_exposed`, even if no call was
made. Correct the isolated configuration before retrying; do not weaken the
adapter or change the shared OpenClaw installation.

## Upgrade

From an isolated Discovery checkout, preserve the existing SQLite and private
Bridge state, then replace only the application checkout/image with `v0.6.3`.
Do not ACK, dismiss, reset, or migrate the protected pending delivery
`dbf6d7a2-395e-40c6-8e91-7866aab89e53`. Keep publication disabled for the
preflight. Roll back by stopping the new container and restoring the prior
application image against the preserved schema-4 volume; do not delete volumes
or rewrite Bridge state.

## Editorial-decision output check

The Gateway editor treats the model reply as untrusted data. The next compatible
Discovery patch adds a trusted, schema-guided output contract to every request;
it does not relax `editorial-decision-v1` or the editorial policy.

For a synthetic candidate whose verification status is `PARTIAL`, the only safe
success result is `NEEDS_VERIFICATION`. It must include a nonempty
`factual_summary`, `unverified_claims`, and `open_questions`; it must not claim
`READY_FOR_REVIEW`, add a date, evidence identifier, or URL not supplied in the
request, or use a rejection reason.

Run the existing isolated `GatewayEditor.evaluate()` smoke with that synthetic
request after installing the patch. Record only the outcome code and decision:

- pass: `NEEDS_VERIFICATION` with no error;
- schema failure: `editorial_schema_invalid:<path>:<validator>:<expected>`;
- any other failure: retain the existing fail-closed result and do not retry by
  changing the model output manually.

The schema diagnostic is capped and contains only JSON paths, field names,
validator names, and expected types. It never includes the model reply,
candidate text, evidence text, URLs, or credentials. Do not record the raw
Gateway response. This smoke is operator-owned and does not authorize Bridge
ACKs, publication, scheduler changes, or a change to the shared OpenClaw
configuration.

## One-shot live editorial smoke

On `claw2`, from the checked-out release candidate, run this single command as
the isolated Discovery service user. Substitute only the two absolute paths;
the configuration must be the isolated `waagd` OpenClaw configuration, never
Shmul or the Telegram agent configuration.

```sh
OPENCLAW_CONFIG_PATH=/absolute/private/claw2-openclaw.json uv run --frozen python scripts/gateway-editorial-live-smoke.py --gateway-executable /absolute/path/to/openclaw
```

It calls `GatewayEditor.evaluate()` once with an in-memory synthetic candidate
and `verification_status=PARTIAL`. It creates no SQLite, Bridge, export, or
publication state. A passing invocation prints exactly:

```json
{"decision":"NEEDS_VERIFICATION","schema_version":1,"status":"ok"}
```

Any other result is fail-closed. For a schema failure, retain only the safe
`editorial_schema_invalid:<path>:<validator>:<expected>` code and do not retain
the raw Gateway reply. The command does not authorize a retry with changed
policy, an ACK, an automatic publication, or a scheduler change.

Each smoke invocation supplies OpenClaw a new random `editorial-smoke-*` session
key. The normal editorial controller retains its deterministic session key, so
this isolation does not alter its idempotent recovery behavior.

The smoke command accepts `--retry-format-once`. It makes at most two calls and
only repeats a completed, attested formatting failure (`empty_response`,
`model_json_invalid`, or `schema_invalid:*`) in a new session. Its default total
limit is 120 seconds and USD 0.10, divided across attempts; uncertain Gateway
outcomes are never retried. Use it for the final operator check:

```sh
OPENCLAW_CONFIG_PATH=/absolute/private/claw2-openclaw.json uv run --frozen python scripts/gateway-editorial-live-smoke.py --gateway-executable /absolute/path/to/openclaw --retry-format-once
```

Failure output contains only one of `model_json_invalid`, `empty_response`,
`envelope_invalid`, or `schema_invalid:<path>:<validator>:<expected>` where
applicable, plus the attested Gateway status and terminal disposition when
reported. It never includes model text, prompt data, URLs, or credentials.

The observed `minimax/minimax-m3.1-flash:free` endpoint supports JSON output
but not JSON Schema enforcement, and the two live results have shown both an
overlong field and non-JSON text. It is therefore not reliable enough for an
unattended structured editorial workflow. After separate cost approval, use `openai/gpt-4.1-mini` through OpenRouter:
its model page advertises JSON Schema structured outputs in `response_format`.
Configure it only in the isolated `claw2` editor and require the structured
parameter to be honored by the selected provider. Do not enable response repair:
the WAAGD adapter must validate the original response and fail closed.
