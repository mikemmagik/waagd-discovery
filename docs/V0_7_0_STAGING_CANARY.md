# v0.7.0 staging canary packet

Use only an isolated checkout at the immutable `v0.7.0` tag and a new empty
private Editorial Store. Keep the existing v0.4.1 scheduler, SQLite volume,
Bridge ledger, and protected pending delivery untouched.

1. Verify the tag commit against the published release and build the container
   from that checkout only. Do not reuse an existing image tag or private state
   directory.
2. Keep scheduler and automatic publication disabled. Keep the Site ingress and
   Publisher disconnected for this canary.
3. Provide the existing isolated, tool-free Gateway configuration with the
   configured `openrouter/free` route. Do not change the shared OpenClaw or
   Telegram configuration.
4. Exercise only synthetic fixtures: a Trusted Publisher candidate that reaches
   editorial review, a fidelity rejection, and a simulated ambiguous fidelity
   outcome. Confirm that failures are `operator_required`, emit no ACK, and do
   not replay after restarting the isolated process.
5. Preserve the canary logs and private Store for review. Roll back by stopping
   the canary and discarding only its newly created private state; do not alter
   any pre-existing volume or Bridge record.

This packet does not authorize RSS, LLM, Site, Cloudflare, Telegram, or
publication activity.
