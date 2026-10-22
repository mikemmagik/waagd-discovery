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

## Editorial smoke checks

Use the two commands in the zero-cost policy section below. The PARTIAL command
is deterministic and never invokes OpenClaw. The VERIFIED command invokes only
the isolated `waagd` Gateway with a synthetic fixture; it is a format and
attestation check, not independent verification of news. Neither command opens
Discovery SQLite or Bridge state, publishes, ACKs a delivery, enables the
scheduler, or changes a shared Gateway configuration.

## Zero-cost editorial policy

The Gateway is no longer asked to create factual or provenance fields. The
trusted host validates the request and constructs `candidate_key`, canonical
source, permitted verification date, evidence IDs, checked evidence URLs,
`created_at`, and model identity. Candidate feed URLs are metadata only and are
not promoted into `checked_sources` merely because they were supplied.

`PARTIAL` never starts OpenClaw: it deterministically returns a valid
`NEEDS_VERIFICATION` decision. `FAILED` deterministically returns `REJECTED`
with `UNVERIFIABLE`, so neither status can produce a publication intent.
`VERIFIED` is the only path that invokes the isolated model. The model may return
only `satirical_angle`, `headline`, `summary`, and `article`; the host rejects
extra fields, overlong strings, URLs, explicit dates, and quotation characters,
then builds and validates the complete existing `editorial-decision-v1` result.
The final `READY_FOR_REVIEW` gate still requires validated independent evidence.
Publication remains default-deny.

The isolated Gateway continues to use the configured OpenRouter free route; Discovery
does not pin a particular free model. GatewayEditor permits one fresh-session correction
only after a completed, fully attested response fails JSON, creative-schema, field-length,
or word-count validation. The correction receives only a bounded validation code, never
the previous model text. Gateway ambiguity, timeout, transport failure, tool use, budget
failure, or creative-safety failure is final and fail-closed: it never triggers an automatic
second call. Safe per-invocation metrics are exposed as route, verification status, outcome,
and safe error code; no model response, prompt, source text, or credential is recorded.

Run these two checks from the isolated checkout. They use no Discovery, Bridge,
or publication state.

```sh
uv run --frozen python scripts/gateway-editorial-live-smoke.py
```

The first command must print `NEEDS_VERIFICATION` and must not contact OpenClaw.
The second is only a model-format smoke: its VERIFIED evidence is synthetic test
fixture data, not independent verification of a real news item.

```sh
OPENCLAW_CONFIG_PATH=/absolute/private/claw2-openclaw.json uv run --frozen python scripts/gateway-editorial-live-smoke.py --verified --gateway-executable /absolute/path/to/openclaw --retry-format-once
```

A passing second command prints `READY_FOR_REVIEW` with
`fixture: synthetic_verified`. It does not authorize publication, ACK, scheduler
activation, or a change to the shared Gateway.
