import gzip
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from sqlite_backup_check import BackupError, backup, list_backups, verify_backup
from sqlite_backup_check.backup import decompress_file


def make_db(path: Path, wal: bool = False, rows: int = 200) -> None:
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), total REAL);
        """
    )
    conn.executemany("INSERT INTO customers(name) VALUES (?)", [(f"Person {i}",) for i in range(rows)])
    conn.executemany(
        "INSERT INTO orders(customer_id, total) VALUES (?, ?)",
        [(i + 1, i * 1.5) for i in range(rows)],
    )
    conn.commit()
    conn.close()


def restore(entry, tmp_path: Path) -> sqlite3.Connection:
    out = tmp_path / "restored.sqlite"
    decompress_file(entry.path, out, entry.manifest["compression"])
    return sqlite3.connect(out)


def test_gzip_round_trip(tmp_path):
    src = tmp_path / "shop.sqlite"
    make_db(src)
    entry = backup(src, tmp_path / "bk", compression="gzip")
    assert entry.path.name.endswith(".sqlite.gz")
    assert entry.manifest["row_counts"] == {"customers": 200, "orders": 200}
    with gzip.open(entry.path) as f:
        assert f.read(16) == b"SQLite format 3\x00"
    rep = verify_backup(entry.path)
    assert rep.ok, rep.format()
    conn = restore(entry, tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 200
    conn.close()
    assert len(list_backups(tmp_path / "bk")) == 1


def test_uncompressed_and_vacuum_into(tmp_path):
    src = tmp_path / "shop.sqlite"
    make_db(src)
    e1 = backup(src, tmp_path / "bk", compression="none")
    assert verify_backup(e1.path).ok
    e2 = backup(src, tmp_path / "bk", compression="gzip", vacuum_into=True, name="vac")
    assert e2.manifest["method"] == "vacuum_into"
    assert verify_backup(e2.path).ok


def test_wal_source_with_uncheckpointed_data(tmp_path):
    src = tmp_path / "wal.sqlite"
    make_db(src, wal=True)
    hold = sqlite3.connect(src)  # keeps the WAL alive, data may sit only in -wal
    hold.execute("PRAGMA wal_autocheckpoint=0")
    hold.execute("INSERT INTO customers(name) VALUES ('Late Person')")
    hold.commit()
    entry = backup(src, tmp_path / "bk")
    hold.close()
    assert entry.manifest["row_counts"]["customers"] == 201
    assert verify_backup(entry.path).ok


def test_concurrent_writer_gives_consistent_backup(tmp_path):
    src = tmp_path / "live.sqlite"
    conn = sqlite3.connect(src)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE a(id INTEGER PRIMARY KEY, v INTEGER)")
    conn.execute("CREATE TABLE b(id INTEGER PRIMARY KEY, v INTEGER)")
    conn.executemany("INSERT INTO a(v) VALUES (?)", [(i,) for i in range(20000)])
    conn.executemany("INSERT INTO b(v) VALUES (?)", [(i,) for i in range(20000)])
    conn.commit()
    conn.close()

    stop = threading.Event()
    started = threading.Event()
    errors = []

    def writer():
        c = sqlite3.connect(src, timeout=30)
        try:
            n = 0
            while not stop.is_set():
                with c:  # a and b always grow together in one transaction
                    c.execute("INSERT INTO a(v) VALUES (?)", (n,))
                    c.execute("INSERT INTO b(v) VALUES (?)", (n,))
                n += 1
                if n == 5:
                    started.set()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            c.close()

    t = threading.Thread(target=writer)
    t.start()
    assert started.wait(10)
    try:
        entries = [backup(src, tmp_path / "bk", name=f"live{i}") for i in range(3)]
    finally:
        stop.set()
        t.join()
    assert not errors
    for e in entries:
        counts = e.manifest["row_counts"]
        assert counts["a"] == counts["b"] >= 20005
        assert verify_backup(e.path).ok


def test_corrupted_header_fails_even_with_fixed_checksum(tmp_path):
    src = tmp_path / "shop.sqlite"
    make_db(src)
    entry = backup(src, tmp_path / "bk", compression="none")
    data = bytearray(entry.path.read_bytes())
    data[0:16] = b"NotASQLiteFile!\x00"
    entry.path.write_bytes(bytes(data))
    # Make the checksum agree, so only the database checks can catch it.
    import hashlib

    m = json.loads(entry.manifest_path.read_text())
    m["sha256"] = hashlib.sha256(bytes(data)).hexdigest()
    entry.manifest_path.write_text(json.dumps(m))
    rep = verify_backup(entry.path)
    assert not rep.ok
    assert any(c.name == "integrity_check" and not c.ok for c in rep.checks)


def test_corrupted_pages_fail_even_with_fixed_checksum(tmp_path):
    src = tmp_path / "shop.sqlite"
    make_db(src, rows=2000)
    entry = backup(src, tmp_path / "bk", compression="none")
    data = bytearray(entry.path.read_bytes())
    mid = 4096 * 3
    data[mid : mid + 8192] = b"\xff" * 8192
    entry.path.write_bytes(bytes(data))
    import hashlib

    m = json.loads(entry.manifest_path.read_text())
    m["sha256"] = hashlib.sha256(bytes(data)).hexdigest()
    entry.manifest_path.write_text(json.dumps(m))
    assert not verify_backup(entry.path).ok


def test_flipped_bytes_in_gzip_fail_checksum(tmp_path):
    src = tmp_path / "shop.sqlite"
    make_db(src)
    entry = backup(src, tmp_path / "bk")
    data = bytearray(entry.path.read_bytes())
    for i in range(len(data) // 2, len(data) // 2 + 16):
        data[i] ^= 0xFF
    entry.path.write_bytes(bytes(data))
    rep = verify_backup(entry.path)
    assert not rep.ok
    assert [c.name for c in rep.checks if not c.ok] == ["checksum"]


def test_checksum_mismatch_detected_via_manifest(tmp_path):
    src = tmp_path / "shop.sqlite"
    make_db(src)
    entry = backup(src, tmp_path / "bk")
    m = json.loads(entry.manifest_path.read_text())
    m["sha256"] = "0" * 64
    entry.manifest_path.write_text(json.dumps(m))
    rep = verify_backup(entry.path)
    assert not rep.ok and "mismatch" in rep.checks[-1].detail


def test_row_count_mismatch_detected(tmp_path):
    src = tmp_path / "shop.sqlite"
    make_db(src)
    entry = backup(src, tmp_path / "bk")
    m = json.loads(entry.manifest_path.read_text())
    m["row_counts"]["orders"] += 1
    entry.manifest_path.write_text(json.dumps(m))
    rep = verify_backup(entry.path)
    assert not rep.ok
    assert [c.name for c in rep.checks if not c.ok] == ["row_counts"]


def test_foreign_key_violation_reported(tmp_path):
    src = tmp_path / "fk.sqlite"
    make_db(src)
    c = sqlite3.connect(src)
    c.execute("INSERT INTO orders(customer_id, total) VALUES (99999, 1.0)")  # FK off by default
    c.commit()
    c.close()
    rep = verify_backup(backup(src, tmp_path / "bk").path)
    assert not rep.ok
    assert [x.name for x in rep.checks if not x.ok] == ["foreign_key_check"]


def test_missing_source_and_manifest(tmp_path):
    with pytest.raises(BackupError):
        backup(tmp_path / "nope.sqlite", tmp_path / "bk")
    f = tmp_path / "x.sqlite.gz"
    f.write_bytes(b"x")
    assert not verify_backup(f).ok


def test_zstd_when_installed(tmp_path):
    pytest.importorskip("zstandard")
    src = tmp_path / "shop.sqlite"
    make_db(src)
    e = backup(src, tmp_path / "bk", compression="zstd")
    assert verify_backup(e.path).ok


def test_zstd_missing_is_clear_error(tmp_path, monkeypatch):
    import sys

    mod = sys.modules["sqlite_backup_check.backup"]
    monkeypatch.setattr(mod, "zstd_available", lambda: False)
    src = tmp_path / "shop.sqlite"
    make_db(src)
    with pytest.raises(BackupError, match="zstandard"):
        backup(src, tmp_path / "bk", compression="zstd")
