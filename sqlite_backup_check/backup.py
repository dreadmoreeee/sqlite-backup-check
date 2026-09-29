"""Create, describe and list backups."""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import crypto

MANIFEST_VERSION = 1
MANIFEST_SUFFIX = ".manifest.json"
_STAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"
_NAME_FMT = "%Y%m%dT%H%M%SZ"


class BackupError(Exception):
    """Usage or I/O problem (CLI exit code 2)."""


def zstd_available() -> bool:
    try:
        import zstandard  # noqa: F401
    except ImportError:
        return False
    return True


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def table_row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {n: conn.execute(f'SELECT COUNT(*) FROM "{n.replace(chr(34), chr(34) * 2)}"').fetchone()[0]
            for n in names}


def compress_file(src: Path, dst: Path, method: str) -> None:
    if method == "gzip":
        with open(src, "rb") as fi, gzip.open(dst, "wb", compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo, 1 << 20)
    elif method == "zstd":
        if not zstd_available():
            raise BackupError("zstd needs the 'zstandard' package (pip install zstandard)")
        import zstandard

        with open(src, "rb") as fi, open(dst, "wb") as fo:
            with zstandard.ZstdCompressor(level=6).stream_writer(fo, closefd=False) as w:
                shutil.copyfileobj(fi, w, 1 << 20)
    elif method == "none":
        shutil.copyfile(src, dst)
    else:
        raise BackupError(f"unknown compression: {method}")


def decompress_file(src: Path, dst: Path, method: str) -> None:
    if method == "gzip":
        with gzip.open(src, "rb") as fi, open(dst, "wb") as fo:
            shutil.copyfileobj(fi, fo, 1 << 20)
    elif method == "zstd":
        if not zstd_available():
            raise BackupError("zstd needs the 'zstandard' package (pip install zstandard)")
        import zstandard

        with open(src, "rb") as fi, open(dst, "wb") as fo:
            with zstandard.ZstdDecompressor().stream_reader(fi) as r:
                shutil.copyfileobj(r, fo, 1 << 20)
    elif method == "none":
        shutil.copyfile(src, dst)
    else:
        raise BackupError(f"unknown compression: {method}")


def _open_source(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn
    except sqlite3.OperationalError:
        # A WAL database on a read-only location may need a writable connection
        # to create the -shm file; fall back to a normal connection (no writes made).
        return sqlite3.connect(str(path), timeout=30)


def snapshot(source, dest: Path, vacuum_into: bool = False) -> dict[str, int]:
    """Write a consistent snapshot of `source` to plain file `dest`.

    Returns the table row counts read from the snapshot itself.
    """
    source = Path(source)
    if not source.is_file():
        raise BackupError(f"source database not found: {source}")
    src = _open_source(source)
    try:
        if vacuum_into:
            quoted = str(dest).replace("'", "''")
            src.execute(f"VACUUM INTO '{quoted}'")
        else:
            dst = sqlite3.connect(str(dest))
            try:
                src.backup(dst)
                dst.commit()
            finally:
                dst.close()
    except sqlite3.Error as exc:
        raise BackupError(f"sqlite backup failed: {exc}") from exc
    finally:
        src.close()
    snap = sqlite3.connect(str(dest))
    try:
        # Make the copy a single self-contained file (a WAL flag is copied by backup()).
        snap.execute("PRAGMA journal_mode=DELETE").fetchone()
        return table_row_counts(snap)
    finally:
        snap.close()


@dataclass
class BackupEntry:
    path: Path
    manifest_path: Path
    created: datetime
    manifest: dict

    @property
    def name(self) -> str:
        return self.path.name


def backup(
    source,
    dest_dir,
    *,
    name: str | None = None,
    compression: str = "gzip",
    vacuum_into: bool = False,
    passphrase: str | None = None,
    now: datetime | None = None,
) -> BackupEntry:
    """Back up `source` into `dest_dir`; returns the new entry (file + manifest)."""
    source = Path(source)
    dest_dir = Path(dest_dir)
    if compression not in ("gzip", "zstd", "none"):
        raise BackupError(f"unknown compression: {compression}")
    if compression == "zstd" and not zstd_available():
        raise BackupError("zstd needs the 'zstandard' package (pip install zstandard)")
    if passphrase is not None and not crypto.available():
        raise BackupError("encryption needs the 'cryptography' package")
    if not source.is_file():
        raise BackupError(f"source database not found: {source}")
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0, tzinfo=None)
    base = name or source.stem
    ext = ".sqlite" + {"gzip": ".gz", "zstd": ".zst", "none": ""}[compression]
    if passphrase is not None:
        ext += ".enc"
    final = dest_dir / f"{base}-{now.strftime(_NAME_FMT)}{ext}"
    manifest_path = Path(str(final) + MANIFEST_SUFFIX)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if final.exists():
            raise BackupError(f"backup already exists: {final}")
        with tempfile.TemporaryDirectory(dir=dest_dir, prefix=".sbc-") as tmp:
            tmpd = Path(tmp)
            raw = tmpd / "snapshot.sqlite"
            counts = snapshot(source, raw, vacuum_into)
            raw_size = raw.stat().st_size
            stage = raw
            if compression != "none":
                comp = tmpd / "snapshot.cmp"
                compress_file(raw, comp, compression)
                stage = comp
            if passphrase is not None:
                enc = tmpd / "snapshot.enc"
                crypto.encrypt_file(stage, enc, passphrase)
                stage = enc
            digest = sha256_file(stage)
            size = stage.stat().st_size
            shutil.move(str(stage), str(final))
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "created": now.strftime(_STAMP_FMT),
            "source": source.name,
            "file": final.name,
            "sha256": digest,
            "size": size,
            "snapshot_size": raw_size,
            "compression": compression,
            "encrypted": passphrase is not None,
            "method": "vacuum_into" if vacuum_into else "backup_api",
            "row_counts": counts,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        raise BackupError(f"I/O error: {exc}") from exc
    return BackupEntry(final, manifest_path, now, manifest)


def load_entry(backup_path) -> BackupEntry:
    p = Path(backup_path)
    mp = Path(str(p) + MANIFEST_SUFFIX)
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
        created = datetime.strptime(manifest["created"], _STAMP_FMT)
    except (OSError, ValueError, KeyError) as exc:
        raise BackupError(f"cannot read manifest {mp}: {exc}") from exc
    return BackupEntry(p, mp, created, manifest)


def list_backups(directory) -> list[BackupEntry]:
    """All backups in `directory` that have a readable manifest, oldest first."""
    d = Path(directory)
    if not d.is_dir():
        raise BackupError(f"not a directory: {d}")
    out = []
    for mp in sorted(d.glob("*" + MANIFEST_SUFFIX)):
        target = mp.with_name(mp.name[: -len(MANIFEST_SUFFIX)])
        try:
            out.append(load_entry(target))
        except BackupError:
            continue
    out.sort(key=lambda e: (e.created, e.name))
    return out
