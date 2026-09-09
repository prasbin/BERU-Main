# Backup & restore

BERU stores **everything** in a single SQLite database file (default
`./beru.db` — see `DATABASE_URL`). That includes:

| Area | Tables |
|---|---|
| Conversations | `conversations`, `messages` |
| Knowledge / scoping | `facts`, `projects` |
| Proactive engine | `scheduled_tasks`, `task_runs`, `monitor_triggers`, `trigger_fires` |
| Notifications | `notifications` |
| Reliability ledger | `activity_records`, `audit_records` |

Because the whole durable state is one file, **a single snapshot of that file is
a complete backup** — there is nothing else to mirror (no secondary store).

## Snapshot (backup)

Use the built-in CLI (`python -m backend.services.backup`), which drives the
**SQLite online backup API** (`sqlite3.Connection.backup`):

```sh
# Timestamped snapshot next to the live DB (default):
python -m backend.services.backup backup

# To an explicit destination:
python -m backend.services.backup backup --target /mnt/backups/beru-$(date +%F).db

# Against a specific database (otherwise settings DATABASE_URL is used):
python -m backend.services.backup backup --database sqlite+aiosqlite:///./beru.db --target /mnt/backups/beru.db

# Skip the integrity + row-count verification (fast, CI-style runs):
python -m backend.services.backup backup --no-verify
```

Why the online backup API rather than `VACUUM INTO` or a raw file copy:

- It is **safe while the app is running** — WAL mode is enforced on every
  connection, and `backup` captures a consistent point-in-time view without
  requiring the file to be quiescent (unlike `VACUUM INTO`) and without
  rewriting the live file.
- The destination is a **normal, self-contained SQLite file** (WAL journal is
  checkpointed into it), so it can be copied/restored anywhere.

Every backup is verified **automatically** before it is reported as written:

```sh
python -m backend.services.backup verify path/to/snapshot.db
```

`verify` runs `PRAGMA integrity_check` and, in the snapshot path, also compares
every user table's row count between the live file and the snapshot. A
mismatch or corrupt file raises instead of silently completing.

## Restore

Restoring is a file copy of a snapshot back over the live database path:

1. **Stop the application.** SQLite does not allow the file to be hot-swapped
   under a running WAL-mode process, and a partial overlap could corrupt both
   files.
2. Replace the live file with the snapshot:

   ```sh
   # From your own copy:
   cp /mnt/backups/beru-2026-09-08.db ./beru.db
   ```

   Or with the built-in helper (which always verifies the snapshot first and
   keeps a `beru.pre-restore.db` copy of the previous live file):

   ```python
   from backend.services.backup import restore_database
   restore_database("/mnt/backups/beru-2026-09-08.db")
   ```

3. **Verify the restored file:**

   ```sh
   python -m backend.services.backup verify ./beru.db
   ```

4. Start the application. Alembic migrations run on startup (`upgrade head`); a
   snapshot taken at the same schema revision as the running build is restored
   as-is. If you restore a snapshot from an older schema revision than the
   installed code, the normal migration path applies it forward (see
   `backend/database/migrations.py`).

## Frequency & hygiene

- **Schedule a snapshot** (e.g. systemd timer / cron) at least daily; the
  write is small and cheap for a single-user app.
- Keep snapshots **off the machine that runs BERU** where a backup matters
  (separate volume / NAS / object storage). A copy on a second disk survives
  drive failure; a sibling on the app disk only survives corruption.
- Pre-restore copies are kept alongside the live DB as
  `beru.pre-restore[-<timestamp>].db` — tidy them up after you confirm a
  restore worked.
- Snapshot files are **self-contained**: once `VACUUM`/backup completes, the
  `-wal` / `-shm` siblings of the source are irrelevant to restores.

## Scope & limitations

- **SQLite only.** The helpers raise `ValueError` for Postgres/MySQL URLs and
  for `:memory:` databases. The app's default and documented deployment is
  SQLite (`sqlite+aiosqlite:///./beru.db`).
- Backups capture **durable database state only**. In-memory state (the
  reliability ledger's live counters, the session-token store) is rebuilt on
  restart; nothing in those is needed to restore a working system.
- Restoring an old snapshot then running a *newer* code release will migrate
  forward, and is fully supported; the reverse (running older code against a
  newer schema) is **not** — stick to restoring to the same-or-newer code
  version.