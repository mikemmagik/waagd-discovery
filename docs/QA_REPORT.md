# Sprint 0–1 verification evidence

Verified 2026-10-17. Scope: Discovery Core, manual operation only.
Four specialists contributed collector, storage, infrastructure and independent
QA work in isolated Git worktrees. The lead reviewed and integrated all changes,
then ran the combined checks. See QA_REVIEW.md for the independent findings.

## Local results

| Check | Actual result |
| --- | --- |
| Offline unit/integration/security tests | 222 PASS, no warnings |
| Statement coverage | 95.77% (1,020/1,065), required minimum 85% |
| Ruff without cache / format | PASS |
| Strict mypy | PASS, all 15 application source files |
| Frozen dependency lock | PASS |
| Tracked-file credential/runtime-data scan | PASS |
| Credential-pattern scan of all local Git history | PASS, no matches |
| Compose configuration | PASS |
| Docker linux/arm64 native build + offline smoke | PASS |
| Docker linux/amd64 emulated build + offline smoke | PASS |
| Offline container persistence / repeat ingestion | PASS, one article across two recreated containers |
| Offline container export / backup / explicit restore | PASS, integrity verified |
| Runtime isolation assertions | PASS: UID10001, readonly root, zero capabilities, no-new-privileges, no socket |
| Real container SIGTERM | PASS, exit130, run interrupted, database integrity preserved |
| Compose down/up cycle | PASS, before/after counts identical |

The images use Python 3.13.16; local development tests ran on Python 3.14.8.
Runtime image size observed: approximately 233 MB arm64 and 208 MB amd64
(Docker image size, not compressed transfer size). Compose bounds are 512 MiB,
one CPU and 64 processes. No sustained resource/soak test has been claimed.

## Live source evidence

Explicit manual Docker collection at 2026-10-17T14:34:38Z checked all three
configured feeds and completed successfully in 2.919 seconds:

| Source | Received | Added | Existing | Failed/skipped |
| --- | ---: | ---: | ---: | ---: |
| Guardian World | 45 | 45 | 0 | 0 |
| Guardian Technology | 31 | 29 | 2 | 0 |
| Ars Technica | 20 | 20 | 0 | 0 |
| Total | 96 | 94 | 2 | 0 |

Run ID: b8e38aca-e5e3-4b8a-9ba9-fa95454a1305.
Immediate repeat run be2d8092-bf07-4f03-9627-829ca92836b5 added zero articles.
World and Ars returned 304; Technology yielded 31 existing entries.
The database retained 94 articles, 3 sources, 2 runs and 127 observations after
Compose down/up. A bounded export of 50 discoveries and a verified backup were
created locally outside Git. External availability is evidence at this timestamp,
not a future guarantee; CI remains offline.

## GitHub and release gate

The repository was verified private, empty initially, with existing authorized
ADMIN access. No credentials were written to repository files or CLI configuration.
CI checks Python 3.13/3.14 and builds/runs offline smoke for amd64/arm64. Consult
the GitHub Actions results for the commit referenced by the release tag; local
checks alone are not a claim of remote CI success. A release tag is created only
after its commit's CI is green. Final commit, tag and CI URL are recorded in the
operator handoff report.

## Boundaries and limitations

No OpenClaw, Shmul, Telegram, Cloudflare, DNS, email or existing website files were
accessed or modified. No AI request, automated schedule or publication was added.
There are no public container ports or Docker socket mount.

Discovery export is unfiltered and unverified. Deterministic preliminary filtering
is Sprint 2 work. Relative article links are skipped; no full article crawler,
HTML source adapter, semantic event clustering, scheduler, health daemon or agent
handoff is implemented. Supported response encodings: identity/gzip/deflate.
The registry is trusted operator input; arbitrary external URLs are not an API.
Local-volume advisory locks do not coordinate external direct SQLite tools.
An abrupt SIGKILL can leave an unfinished run record; already committed source
transactions remain. Backup retention/off-device transfer is operator-managed.
Mac sleep/power-off pauses work; no continuous-availability claim is made.

Schema validation deliberately refuses custom/unrecognized schema objects. Restore
of a corrupt working database may need an operator-controlled separate target;
the tool refuses to discard a target it cannot verify and back up.
