# Sprint 6 staging operator guide

This guide is for an isolated staging host. It does not authorize a production
deployment, DNS change, paid model use, or modification of the existing `main`
agent. Keep all paths below outside source checkouts and owned by the service user.

## Trust boundary

Discovery writes only `publication-intent.json` to a private `0700` export
directory. It contains at most 100 independently verified, review-ready decisions.
`waagd-publication-bridge` checks that exact v1 envelope and atomically copies it
into the website automation's private `WAAGD_STATE_DIR`. It cannot open SQLite,
call Gateway, or publish. The website's `publish-import` command parses the
envelope again, checks every content digest and identity, then uses its separate
bearer-token journal. The journal is default-off and reconciles uncertain requests
before a retry.

The Gateway process receives a bounded editorial request and its explicitly
allowlisted Gateway configuration only. It does not inherit site write credentials,
the Telegram token, Docker access, or a shell. Configure the existing `waagd`
agent to be tool-free before any staging run; a zero tool count in a response is an
additional check, not a replacement for that policy.

## Staging paths and commands

Create four dedicated `0700` roots: editorial store, publication export, website
automation state, and Bridge state. Do not use the Discovery SQLite volume as any
of them. The existing Discovery/Bridge cycle prepares the inbox. Then run the
following one-shot stages under the same unprivileged service account:

```sh
waagd-editorial --config /srv/waagd/private/editorial.yaml \
  --inbox /srv/waagd/workspace/waagd-discovery/inbox.json \
  --store /srv/waagd/editorial/editorial.sqlite3 \
  --export /srv/waagd/discovery-export/candidates.json \
  --workspace-root /srv/waagd/workspace \
  --state-dir /srv/waagd/bridge-state \
  --no-simulation --allow-gateway --allow-ack \
  --gateway-executable /usr/local/bin/openclaw \
  --gateway-max-candidates 1 --gateway-max-cost-usd 0.10 \
  --corroboration-policy /srv/waagd/private/corroboration.yaml \
  gateway-process

waagd-publication-export \
  --store /srv/waagd/editorial/editorial.sqlite3 \
  --output /srv/waagd/publication-export/publication-intent.json --limit 100 \
  --publication-policy /srv/waagd/private/publication.yaml

waagd-publication-bridge \
  --source /srv/waagd/publication-export/publication-intent.json \
  --state-directory /srv/waagd/site-state

node --env-file=/srv/waagd/private/site.env automation/cli.ts publish-import
```

`editorial.yaml` must set all of `editorial.enabled`, `network.enabled`, and
`llm.enabled` to `true` for the deliberately named `gateway-process` command.
The command otherwise fails before opening external input. The site environment
also requires `WAAGD_SPRINT6_PUBLISHING_ENABLED=1`; absence or `0` is the kill
switch and records no new Worker write. Keep it at `0` for all non-staging runs.

`corroboration.yaml` is a service-user-owned `0700` directory with a `0600` file;
it is an independent allowlist, never a downloaded source artifact. It must name
every candidate which may become `VERIFIED`:

```yaml
corroboration:
  policies:
    - candidate_key: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
      trusted_independent_hosts: [confirm.example]
      claims:
        - claim: The independently checked factual claim has at least four words.
          independent_url: https://confirm.example/independent-report
```

For unattended staging, a separately governed verification service may instead
be pre-authorized with a fixed HTTPS template. The candidate URL is percent-encoded
only into the configured query placeholder; it cannot select a host or path. The
service must return exactly `canonical_url` and a 20–300-character `claim`, and the
claim must occur verbatim in both its response and the retrieved article. A failed,
redirected, malformed, or mismatched response stays `PARTIAL`.

```yaml
corroboration:
  automatic:
    endpoint_template: https://verify.example/check?candidate_url={candidate_url}
```

Publication export is deny-all unless a private service-user-owned `0700`
directory contains a `0600` policy file. Keep it disabled until a genuine,
governed verification evidence source has passed staging acceptance; an HTTP 200
or a model assertion is never enough.

```yaml
publication:
  automatic_enabled: false
  version: publication-policy-v1
  maximum_age_hours: 168
```

Set `automatic_enabled: true` only for the isolated staging run after the
operator records independent-source evidence approval. The site publisher still
has its separate default-off kill switch and trusted authorizer.

The Gateway command rejects a batch larger than `--gateway-max-candidates` (1–10)
before it can start a model turn. `--gateway-max-cost-usd` is mandatory and capped
at one USD per candidate; the batch ceiling is their product. Use a smaller
explicit amount for staging. A candidate without a matching policy remains
`PARTIAL` and cannot reach publication.

Do not put the site environment file in the Gateway process. The Gateway executable
must be an absolute, executable regular file without group/world write permission.

## Owner Telegram workflow

Set `WAAGD_SPRINT6_TELEGRAM_WORKFLOW_ENABLED=1` only after the tool-free `waagd`
Gateway policy has been checked. The existing owner-ID allowlist remains authoritative.
An owner message is first saved privately, receives a bounded preview, and only its
exact `/publish <id>` command may call the journal. Replayed updates and restarts
reuse the stable `telegram-<id>` identity. Binary Telegram uploads remain rejected;
send a public URL or text. The publisher kill switch applies here too.

For text, the editor may return only a URL-less `article` with no references. For a
URL, it must retain that exact URL as canonical URL and may cite only that URL. A
tool call, malformed reply, unavailable Gateway, uncertain write, or tombstoned
duplicate becomes a hold rather than a publication.

## Update and recovery

Back up the Discovery volume and retain the independent editorial/site-state roots
before upgrading. Build and validate both repositories, replace the containers or
local package, then run the commands above with the publication kill switch still
off. Check `publish-import` reports parsed items without a post. Enable the switch
for one isolated staging run only after checking the local D1 migration and Gateway
agent configuration. Never reset either SQLite database or delete publication journal
files during recovery: replaying the same batch is how duplicate prevention works.

The automated acceptance uses fake Gateway, Telegram and Worker/D1 implementations.
It does not establish a live Gateway, bot, Cloudflare, paid-model, or production pass.

From the Discovery checkout, one offline release-acceptance command verifies both
repositories and the two fake end-to-end journeys. It builds disposable images and
uses only temporary data:

```sh
python scripts/sprint6-acceptance.py --site-root /srv/waagd/weareallgonnadie
```

Release acceptance is pinned to Site RC
`ca112b2ab89a7ec0d82b85b06df07bb86a35b41c`. The supplied site checkout must be
a clean detached checkout at that exact revision. The acceptance first confirms
that the exporter rejects any other output filename, then runs the site's actual
`tests/backend.integration.test.ts` E2E. That test creates a real Python intent
and checks the TypeScript validator, Discovery ingress, Publisher, local
Worker/D1, replay and conflict handling, and frontend parser.

Use `--skip-docker` only while iterating locally; it is not release evidence.
