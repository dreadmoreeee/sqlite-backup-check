# sqlite-backup-check

Safe backups for SQLite-backed apps, with proof that they restore.

A copy of a live `.sqlite` file is not a backup. This tool takes a consistent online backup, records a checksum and the table row counts, rotates old copies, and can restore each backup into a temp file to check it really opens and matches what was saved. Stdlib only at its core; encryption and zstd are optional extras. Python 3.10+.

## Install

```
pip install .                    # core (gzip)
pip install ".[encrypt]"         # + AES-256-GCM encryption (cryptography)
pip install ".[zstd]"            # + zstd compression (zstandard)
```

Or run it straight from a checkout: `python -m sqlite_backup_check ...`.

## Use

```
sqlite-backup-check backup /srv/app/app.sqlite /var/backups/app --verify
sqlite-backup-check backup app.sqlite backups --compress none --vacuum-into
sqlite-backup-check verify /var/backups/app            # every backup in the folder
sqlite-backup-check verify /var/backups/app --latest
sqlite-backup-check rotate /var/backups/app --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --dry-run
sqlite-backup-check list /var/backups/app --json
```

Python API:

```python
from sqlite_backup_check import backup, verify_backup, rotate

entry = backup("app.sqlite", "backups", compression="gzip")
report = verify_backup(entry.path)
assert report.ok, report.format()
rotate("backups", daily=7, weekly=4, monthly=6, dry_run=True)
```

## What it does

- **Backup**: `sqlite3.Connection.backup` from a read-only connection, so it runs while the app writes and sees WAL content. `--vacuum-into` uses `VACUUM INTO` instead (smaller, defragmented file). The copy is stored as a single self-contained file (no `-wal`).
- **Row counts** are read from the snapshot itself, not from the live database, so they can never drift from the saved data.
- **Compression**: gzip, or zstd if `zstandard` is installed. `--compress none` is also available.
- **Manifest**: `<backup>.manifest.json` with sha256, sizes, compression, encryption flag, method, timestamp and row counts.
- **Rotation** (grandfather-father-son): keeps the newest backup of each of the last N days, N ISO weeks and N months that have backups. A backup kept by any tier is kept. Times are UTC. `--dry-run` deletes nothing.
- **Verify**: checksum, decrypt, decompress into a temp file, then `PRAGMA integrity_check`, `PRAGMA foreign_key_check`, and a comparison of row counts against the manifest. Temp files are removed afterwards.

Backup files are named `<name>-YYYYMMDDTHHMMSSZ.sqlite[.gz|.zst][.enc]`.

## Encryption and key handling

`--passphrase-env VAR` or `--passphrase-file PATH` enables AES-256-GCM. The key is derived with scrypt (n=2^15, r=8, p=1) from your passphrase and a random 16-byte salt; each file gets a random 12-byte nonce. The versioned header (`SBCE`, version 1, KDF parameters, salt, nonce) is authenticated as GCM associated data, so tampering with it is detected. Data is compressed, then encrypted.

- The passphrase is read from an environment variable or a file (trailing newline stripped). It is never accepted as a command-line argument, because arguments leak through `ps` and shell history.
- Keep the file readable only by the backup user (`chmod 600`).
- **A lost passphrase means lost data.** There is no recovery path or backdoor. Store the passphrase separately from the backups, in a password manager or a sealed copy.
- The whole file is processed in memory during encryption and decryption; plan for RAM about the size of the compressed backup.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | OK |
| 1 | Verification failed (checksum, decrypt, integrity, foreign keys or row counts) |
| 2 | Usage or I/O error (bad arguments, missing file, missing optional package) |

For cron, chain with `&&`, or let systemd mark the unit failed. See `examples/` for a cron line, a systemd service and timer, a CI example, and `demo.py`.

## Tests

```
python -m pytest -q -p no:cacheprovider --import-mode=importlib
```

Covers a concurrent writer during backup, a WAL source, gzip and encryption round trips, a wrong passphrase, rotation with fixed dates, corrupted backups (with and without a rewritten checksum) and checksum mismatch. Encryption tests skip if `cryptography` is missing; the zstd test skips without `zstandard`.

## Limits

- One database per backup; attached databases are not included.
- The backup API holds a read snapshot for the duration of the copy. In WAL mode writers continue; in rollback-journal mode writers wait (up to 30 s) until it ends.
- Rotation only touches files that have a manifest in the directory you give it.

## Measured result

`python examples/demo.py` builds an invented database (30,000 customers, 60,000 orders, WAL mode), backs it up while a writer thread inserts rows, verifies it, then flips bytes in a copy:

```
== backup while a writer thread is inserting rows ==
backup written: backups/shop-20260929T135720Z.sqlite.gz
  738932 bytes, sha256 283f981ab0d901b3d70fbf5932ca10fd1ab0de7acc32888e1a928a4cc7688515
  2 table(s), 90000 rows recorded from the snapshot
OK   backups/shop-20260929T135720Z.sqlite.gz
  [ok  ] manifest: created 2026-09-29T13:57:20Z, method backup_api
  [ok  ] checksum: sha256 283f981ab0d901b3...
  [ok  ] decompress: gzip
  [ok  ] integrity_check: ok
  [ok  ] foreign_key_check: no violations
  [ok  ] row_counts: 2 table(s), 90000 rows match the manifest
(writer inserted 355 customer+order pairs meanwhile)

== verify a corrupted copy (bytes flipped after the backup) ==
FAIL corrupted/shop-20260929T135720Z.sqlite.gz
  [ok  ] manifest: created 2026-09-29T13:57:20Z, method backup_api
  [FAIL] checksum: sha256 mismatch (expected 283f981ab0d901b3d70fbf5932ca10fd1ab0de7acc32888e1a928a4cc7688515, got a0f621d51ff5416ee3c1df326d79fab30f758c476fd95249b93f4f901f238cde)
0/1 backup(s) verified

exit codes: good backup=0, corrupted copy=1 (expected 0 and 1)
```

Test suite (Python 3.12, Windows):

```
24 passed, 1 skipped in 2.25s
```

Marvin Palencia, founder of [DeMark Studio](https://demarkstudio.ca), Miramichi, New Brunswick, Canada. Portfolio: [marvin.demarkstudio.ca](https://marvin.demarkstudio.ca)

MIT License.
