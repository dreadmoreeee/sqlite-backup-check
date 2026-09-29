"""Command line interface. Exit codes: 0 ok, 1 verification failed, 2 usage/IO error."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, crypto
from .backup import MANIFEST_SUFFIX, BackupError, backup, list_backups
from .rotate import rotate
from .verify import verify_backup

EXIT_OK = 0
EXIT_VERIFY_FAILED = 1
EXIT_ERROR = 2


def _add_pass_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("passphrase (never accepted on the command line)")
    g.add_argument("--passphrase-env", metavar="VAR", help="read the passphrase from this environment variable")
    g.add_argument("--passphrase-file", metavar="PATH", help="read the passphrase from this file")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="sqlite-backup-check",
        description="Safe SQLite backups: online backup, checksums, rotation, verification.",
        epilog="Exit codes: 0 ok, 1 verification failed, 2 usage/IO error.",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backup", help="take a backup")
    b.add_argument("source", help="SQLite database file")
    b.add_argument("dest_dir", help="directory receiving the backup and its manifest")
    b.add_argument("--name", help="file name prefix (default: source file stem)")
    b.add_argument("--compress", choices=["gzip", "zstd", "none"], default="gzip")
    b.add_argument("--vacuum-into", action="store_true", help="use VACUUM INTO instead of the backup API")
    b.add_argument("--verify", action="store_true", help="verify the new backup right away")
    _add_pass_args(b)

    v = sub.add_parser("verify", help="verify one backup file, or every backup in a directory")
    v.add_argument("target", help="backup file or directory")
    v.add_argument("--latest", action="store_true", help="with a directory: only the newest backup")
    _add_pass_args(v)

    r = sub.add_parser("rotate", help="grandfather-father-son retention")
    r.add_argument("directory")
    r.add_argument("--keep-daily", type=int, default=7)
    r.add_argument("--keep-weekly", type=int, default=4)
    r.add_argument("--keep-monthly", type=int, default=6)
    r.add_argument("--dry-run", action="store_true", help="show what would be removed, delete nothing")

    ls = sub.add_parser("list", help="list backups in a directory")
    ls.add_argument("directory")
    ls.add_argument("--json", action="store_true")
    return ap


def _passphrase(args) -> str | None:
    return crypto.load_passphrase(args.passphrase_env, args.passphrase_file)


def _cmd_backup(args) -> int:
    entry = backup(
        args.source,
        args.dest_dir,
        name=args.name,
        compression=args.compress,
        vacuum_into=args.vacuum_into,
        passphrase=_passphrase(args),
    )
    m = entry.manifest
    total = sum(m["row_counts"].values())
    print(f"backup written: {entry.path}")
    print(f"  {m['size']} bytes, sha256 {m['sha256']}")
    print(f"  {len(m['row_counts'])} table(s), {total} rows recorded from the snapshot")
    if args.verify:
        rep = verify_backup(entry.path, _passphrase(args))
        print(rep.format())
        return EXIT_OK if rep.ok else EXIT_VERIFY_FAILED
    return EXIT_OK


def _cmd_verify(args) -> int:
    target = Path(args.target)
    if target.is_dir():
        entries = list_backups(target)
        if not entries:
            raise BackupError(f"no backups found in {target}")
        if args.latest:
            entries = entries[-1:]
        paths = [e.path for e in entries]
    elif target.exists():
        paths = [target]
    else:
        raise BackupError(f"not found: {target}")
    pw = _passphrase(args)
    bad = 0
    for p in paths:
        rep = verify_backup(p, pw)
        print(rep.format())
        bad += 0 if rep.ok else 1
    print(f"{len(paths) - bad}/{len(paths)} backup(s) verified")
    return EXIT_OK if not bad else EXIT_VERIFY_FAILED


def _cmd_rotate(args) -> int:
    kept, removed = rotate(
        args.directory, args.keep_daily, args.keep_weekly, args.keep_monthly, args.dry_run
    )
    verb = "would remove" if args.dry_run else "removed"
    for e in removed:
        print(f"{verb}: {e.name}")
    print(f"kept {len(kept)}, {verb} {len(removed)}")
    return EXIT_OK


def _cmd_list(args) -> int:
    entries = list_backups(args.directory)
    if args.json:
        print(json.dumps([dict(e.manifest, file=e.name) for e in entries], indent=2))
        return EXIT_OK
    for e in entries:
        m = e.manifest
        enc = " enc" if m.get("encrypted") else ""
        print(f"{m['created']}  {m['size']:>10}  {m['compression']}{enc}  {e.name}")
    print(f"{len(entries)} backup(s)")
    return EXIT_OK


_COMMANDS = {"backup": _cmd_backup, "verify": _cmd_verify, "rotate": _cmd_rotate, "list": _cmd_list}


def main(argv=None) -> int:
    ap = build_parser()
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:  # argparse: 0 for --help/--version, 2 for usage errors
        return int(exc.code or 0)
    try:
        return _COMMANDS[args.cmd](args)
    except (BackupError, crypto.CryptoUnavailable, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
