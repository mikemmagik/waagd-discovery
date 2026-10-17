# Security boundaries

No production credentials, publishing capability, LLM clients, Docker socket,
OpenClaw paths, public ports, privilege escalation or root runtime. Data and
backups are local volumes/directories; root filesystem is read-only. The process
uses a restrictive umask. Do not mount arbitrary host directories into the image.

The source registry is trusted operator configuration, not feed-supplied input.
HTTP(S) only; public destination checks and validation on redirects; bounded
response/XML/item/text size; finite retries and timeouts. XML DTD/entities are
rejected. Feed text is stripped of markup and serialized as JSON data. It is never
executed, followed as an instruction or used to trigger new URL fetches.

Export is an untrusted data interface. A future editorial agent must retain this
boundary and must not receive SQLite writes, shell execution, production tokens
or authority to publish from feed content. That integration is outside this release.

SQLite identities use constraints and parameterized SQL. Source transactions,
read-only snapshots and backups protect data. Supported CLI mutators use an
advisory lock. Tools directly opening SQLite must coordinate externally; local
Docker volume is required, network filesystems are unsupported.

Logs contain fixed error codes, source IDs and counts. They do not include
request/response bodies, URL credentials, cookies or authorization headers.
CI checks obvious credential patterns; this is a heuristic, not a guarantee.

Operational limits: this is a local single-operator service, not a public input
API. Feed subscriptions do not grant article/image reuse rights. No paywall
bypass or complete article crawling is implemented. Backups on the same device
do not protect against device loss. macOS sleep/power-off pauses collection.

Sprint 2 adds only deterministic technical checks. Eligibility never grants
publication authority. Filter runs/decisions are immutable, transactionally
complete and tied to exact inputs, policy and engine version. Export validates
current registry settings in its memory snapshot and rechecks age before ranking.
All original observations remain even when a story is deferred or excluded.
Managed retention handles only its dedicated filename namespace, rejects linked
or changing files and never counts a corrupt/newer-schema file as a survivor.
See BACKUPS.md for standalone immutable-read safeguards and off-device policy.

Sprint 3 retains these boundaries. Scheduling requires an explicit command/profile
and mounts no agent workspace. Canonical DB-side lock paths are protected against
export replacement, including when the database is reached through a symlink.
Read-only diagnostics do not recover or migrate an operator database. Development
restart/kill tests use disposable volumes, fixture networking and no production
credentials. A kernel lock is evidence only for cooperating local processes;
direct out-of-band SQLite writers remain unsupported. The service cannot collect
while the Mac is asleep/powered off or Docker Desktop is stopped.

Sprint 4 publishes a bounded file-only interface; it grants no Discovery database,
Docker, socket, backup or secret access. It does not modify the existing agent's
preexisting tools/permissions: actual read-only consumer access is an operator
review requirement. The host bridge is offline and does not import SQLite/Docker/
HTTP clients; only its explicit export file, fixed workspace child and private
state are used. Workspace/export/state roots cannot overlap; descriptor-relative
nofollow reads reject links, unsafe types, mutation and oversized files. Private
ledger initialization and checksums detect accidental missing/corrupt state;
checksums/policy hashes are not authentication against a privileged local writer.
Keep producer directories controlled and never expose the ledger to the consumer.

News/source strings remain hostile data. The bridge never executes commands,
fetches URLs, renders templates or promotes text into trusted instructions.
Encoding/control replacement and a static policy warning do not remove semantic
prompt injection. A future consumer must enforce expiry, data-only interpretation
and durable candidate-key deduplication itself. At-least-once publication is not
exactly-once external processing. ACK/dismiss are explicit trusted actions, never
accepted from article text or automatic workspace receipts. No real agent/LLM is
connected in this release; only mock workspaces and fictional news are tested.
