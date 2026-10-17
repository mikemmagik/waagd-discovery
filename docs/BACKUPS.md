# Managed backups and retention

Managed backups extend the existing SQLite online-backup API. They are separate
from manual backups and pre-restore safety copies. Nothing schedules backups or
deletes working databases automatically.

The default policy in `config/backup.yaml` is:

```yaml
backup:
  keep_last: 7
  max_age_days: 30
```

Both settings must be integers of at least one; booleans, floats, unknown keys,
duplicate keys, malformed/deeply nested YAML and oversized configuration fail
closed. `backup: {}` selects the defaults. The retention set is the **union** of
the newest `keep_last` verified backups and every verified backup whose filename
timestamp is within `max_age_days` of the reference time. The age boundary is
inclusive. Future-dated valid backups are retained conservatively. File mtimes
do not determine age. A single surviving valid backup is never removed, however
old it is.

Use one dedicated, locally mounted directory writable by runtime UID 10001, such
as `/backups/managed`. Keep separate operator-controlled copies outside the
MacBook as well: retention is not protection against losing the machine.

## Commands

Create a verified backup and then apply its configured retention policy:

```sh
docker compose run --rm discovery backup-managed --directory /backups/managed
```

Preview a rotation without creating, deleting or migrating backup files:

```sh
docker compose run --rm discovery backup-rotate --directory /backups/managed
```

After reviewing the preview, explicitly apply rotation:

```sh
docker compose run --rm discovery backup-rotate --directory /backups/managed --apply
```

The global `--backup-policy PATH` option selects a different trusted policy file.
The default path is `config/backup.yaml` (`/app/config/backup.yaml` in the image).
Keep policy files outside the backup directory. The preview requires an existing
directory; managed creation can create it. No automatic schedule is enabled.

## Which files may be deleted

Only exact names of this form are managed:

```text
waagd-backup-YYYYMMDDTHHMMSSffffffZ-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx.sqlite3
```

The timestamp is valid UTC with six microsecond digits; the UUID uses lowercase
canonical hex and hyphens. Names are never interpreted as paths. Files with other
names, invalid dates, symlinks, hardlinks, subdirectories, devices, FIFOs and files
with SQLite WAL/SHM/journal sidecars are preserved. No recursive deletion occurs.
Manual and pre-restore files are outside managed retention.

Every candidate is opened without following a symlink. Its inode and change
metadata are checked before and after reading. Verification opens a pinned file
descriptor with SQLite's read-only immutable option, using the existing exact
schema validator and in-memory snapshot/migration path, plus SQLite integrity and
foreign-key checks. The immutable option is used only for closed standalone managed
backups without sidecars, never the working WAL database. It prevents even
read-only SQLite WAL bookkeeping from appearing beside originals. No complete
temporary disk copy is made, so a backup larger than the container's 32 MiB
temporary filesystem can still be verified within the container's memory limit.
SQLite documents the [immutable URI option](https://www.sqlite.org/uri.html) as
an assurance that the underlying file cannot change. The dedicated directory
lock and before/after inode, change-metadata and sidecar checks enforce that
assumption for cooperating backup operations; use ordinary snapshots for a live
working database.
Recognized older schemas are accepted if their in-memory migration and full
verification succeed; backup bytes are never migrated. Corrupt, unversioned,
unrecognized and newer schemas are reported and preserved, and do not count as
survivors.

Cooperating backup operations take a nonblocking advisory lock on the dedicated
directory inode, separate from the working-database lock. No lock file is created,
including during preview. This assumes a local filesystem supporting `flock`;
network shares are unsupported. The operator must not replace or edit files in
that directory concurrently with a rotation.

Before deletion, rotation revalidates a retained backup, synchronizes its file
and directory, checks the obsolete file again, and checks both identities and the
directory immediately before unlinking. If every planned survivor changed or
became invalid, deletion stops. Identity changes, I/O errors or interruption can
leave extra files. A successful deletion is recorded before the directory is
synchronized; a later sync error stops further deletions while retaining a
previously verified, synchronized survivor.

Managed creation first calls `Repository.backup`, which exclusively creates the
new file through SQLite's online-backup API. It verifies the result, synchronizes
the new file and directory, and only then considers any old deletion. A failed
verification, creation or durability step cannot start rotation. An interrupted
operation may leave an additional file for operator inspection. Never remove a
corrupt-looking file merely to make the retained count smaller.

## API and JSON results

`RetentionPolicy(keep_last=7, max_age_days=30)` is frozen.
`load_retention_policy(Path)` returns a validated policy.

`rotate_backups(directory, policy, *, apply=False, as_of=None)` uses one aware UTC
reference time. A provided aware timestamp is converted to UTC; a naive timestamp
is rejected. Its result always has these keys:

```json
{
  "status": "preview",
  "apply": false,
  "reference_time": "2026-10-17T12:00:00+00:00",
  "policy": {"keep_last": 7, "max_age_days": 30},
  "valid": 8,
  "keep": ["<managed basename>"],
  "would_delete": ["<managed basename>"],
  "deleted": [],
  "preserved": [{"name": "manual.sqlite3", "reason": "unmanaged_name"}],
  "reason": null
}
```

The example arrays are abbreviated. `valid` is the number verified during the
initial scan. `keep` and `would_delete` describe that initial retention plan;
rechecks can preserve more files. Only `deleted` asserts actual deletion.
`status` is `preview`, `rotated`, or `blocked`. A blocked rotation reports a fixed
top-level `reason`: `no_valid_survivor`, `directory_changed`,
`survivor_verification_failed`, `survivor_changed`, or `rotation_io_failed`.
Files rejected individually appear in `preserved` with fixed reasons such as
`schema_newer`, `schema_invalid`, `corrupt_database`, `integrity_failed`,
`sqlite_sidecars_present`, `symlink`, `hardlink`, `nonregular_file`,
`unreadable_file`, `file_changed`, or `unmanaged_name`. Unexpected filename
characters are replaced with `[unrecognized-name]` in reports. No absolute paths,
SQLite exception text or file contents enter result metadata.

`create_managed_backup(repository, directory, policy)` returns:

```json
{
  "status": "created",
  "created": "<new managed basename>",
  "rotation": {"status": "rotated", "...": "the complete rotation result above"}
}
```

A successful creation can have a blocked nested rotation if subsequent filesystem
rechecks fail. Inspect `rotation.status` as well as the outer status. Errors before
creation/verification/durability completes raise a fixed `DiscoveryError` code;
they do not report a successful creation.
