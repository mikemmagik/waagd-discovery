# WAAGD Discovery v0.7.0 release

## Scope

`v0.7.0` is the stable release of the Trusted Publishers editorial route and
its durable Gateway recovery work. It is a software release only. It does not
authorize deployment, scheduler activation, automatic publication, or mutation
of an existing Bridge ledger.

## Included capabilities

- Trusted Publisher identification for the configured Guardian and Ars Technica
  RSS routes, retaining a strict distinction from independent verification.
- Bounded AI selection of zero to five eligible candidates per UTC day; the
  Site/D1 remains authoritative for its atomic publication quota.
- PublicationIntent v2 for the Trusted Publisher route, alongside the retained
  independently VERIFIED PublicationIntent v1 route.
- A tool-free OpenRouter free-route Editorial Gateway that creates only bounded
  creative fields; trusted code owns provenance and policy data.
- Deterministic and model-assisted source-fidelity gates. They permit editorial
  interpretation, irony and proportionate satire, while failing closed on
  unsupported material factual claims.
- Durable attempt history and restart recovery. Ambiguous or failed Gateway and
  fidelity turns without a saved decision become operator-required and are never
  replayed automatically or ACKed.

## Compatibility

- Discovery data schema remains 4; the private Editorial Store remains schema 3.
- PublicationIntent v1 is unchanged. Trusted Publisher output uses the tested
  v2 Site contract.
- The isolated Gateway stays on the configured `openrouter/free` route; no paid
  model is selected or enabled.

## Validation

The release candidate commit passed the Python 3.13/3.14, secret-scan,
regression/security, and Docker AMD64/ARM64 CI jobs. Independent QA validated
fault injection for fidelity failure and recovery boundaries.
