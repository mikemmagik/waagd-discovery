# WAAGD Discovery v0.6.0 stable handoff

## Release record

- Stable release: `v0.6.0`.
- Stable release commit on `main`: `ffb5c1284fc21b8032da0e85af158514a97a611f`.
- Consolidation pull request: [#12](https://github.com/mikemmagik/waagd-discovery/pull/12).
- Main CI: [run 39308869053](https://github.com/mikemmagik/waagd-discovery/actions/runs/39308869053), passed for Python 3.13, Python 3.14, Docker AMD64 and Docker ARM64.
- Independent QA: PASS for the final release delta and the preceding cross-repository acceptance.
- Compatible Site revision: `mikemmagik/weareallgonnadie@ca112b2ab89a7ec0d82b85b06df07bb86a35b41c`.

The release source is immutable at the tag above. This handoff is operational
documentation added after the tagged consolidation commit; it does not alter the
released runtime.

## Retained components

`v0.6.0` retains the Discovery Engine, persistent SQLite schema 4, opt-in
Scheduler, Docker runtime, Bridge v1 and opt-in v2, selected-candidate export,
backup and recovery behavior, Source Verifier, Editorial Controller and Store,
bounded OpenClaw Gateway Adapter, PublicationIntent v1, Publication Policy,
private data-only handoff, durable recovery and deduplication.

Publication stays default-deny. A publication intent requires trusted enabled
policy, VERIFIED evidence, an eligible editorial decision and the independent
site kill switch. The model does not receive SQLite, Docker, shell, Cloudflare,
Telegram or site credentials.

## Acceptance evidence

- Discovery security/regression suite: 988 passed, 90.86% coverage.
- Site validation suite: 97 passed; build smoke passed.
- Actual producer to Site local Worker/D1 E2E: 42 passed, 0 skipped.
- The checked E2E covers the real Python producer, TypeScript validator,
  Discovery ingress, Publisher, local Worker/D1, replay, changed-content
  conflict and frontend parser.
- CI secret scan, schema compatibility, Bridge recovery, Docker persistence,
  Bridge smoke and Editorial smoke are part of the successful CI matrix.

## Safe branch cleanup

The following fully merged branches were removed after the stable tag was
published. Their commits remain reachable from `main` or an existing release tag:

- Remote: `codex/sprint6-autonomous-publishing` and
  `sprint5/editorial-processing`.
- Local: `codex/sprint6-autonomous-publishing`,
  `codex/sprint6-independent-qa`, `feature/sprint2-backups`,
  `feature/sprint2-filtering`, `sprint5/editorial-processing`,
  `test/sprint2-independent-qa` and `test/sprint2-resilience`.

Remote-tracking references were pruned after this cleanup. Existing release tags,
including `v0.1.0` through `v0.6.0-rc.1`, were retained.

The following branches remain because they have commits not mechanically merged
into `main`, are attached to a worktree, or correspond to open dependency PRs:

- `codex/sprint6-verifier-gateway` is attached to an existing worktree.
- Historical specialist and QA branches for Sprints 3–5 remain for audit until
  their patch equivalence is reviewed individually.
- Open Dependabot branches and PRs remain separate dependency work; none was
  absorbed into this stable release.

## Upgrade from the target MacBook's v0.4.1

The target Intel MacBook is not changed by this release process. Before any
operator-controlled upgrade, preserve separate backups of the Discovery SQLite
volume, current export/Bridge state and Compose configuration. Keep the protected
pending delivery
`dbf6d7a2-395e-40c6-8e91-7866aab89e53` intact: do **not** ACK, dismiss, reset or
migrate Bridge around it as part of this upgrade.

Use a clean checkout of `v0.6.0`, review private paths and configuration ownership,
build the image, and run the documented isolated checks from
[`SPRINT_6_OPERATOR.md`](SPRINT_6_OPERATOR.md) with publication disabled. Do not
reuse a production credential in a Gateway process. Treat the installed Bridge
v0.4.1 delivery as a separate operator reconciliation decision before enabling any
Bridge v2 workflow.

## Rollback

Stop the new container, retain all volumes and private state, and restore the
previous v0.4.1 image/configuration against the preserved schema-4 Discovery
volume. Do not delete volumes, reset SQLite, overwrite Bridge state, or attempt a
Bridge v2 downgrade. A rollback after any separately approved Bridge-v2 migration
requires an explicit compatibility review and backup-based recovery plan.

## Runtime limitations

- No deployment, DNS change, Cloudflare mutation or automatic publication was
  performed by this release.
- The installed OpenClaw/MacBook acceptance is operator-owned and remains a
  separate staging procedure.
- Real independent evidence governance must be configured before VERIFIED can be
  assigned outside synthetic acceptance; HTTP success or a model assertion is not
  evidence.
- Automatic publication remains disabled until separately authorized staging
  policy and the site kill switch are both enabled.
- No pending Bridge delivery, OpenClaw agent, Telegram integration, staging or
  production system was modified during consolidation.
