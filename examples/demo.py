"""Demo: back up a busy synthetic database, verify it, then show a corrupted copy fail.

Run from the tool folder:  python examples/demo.py
Everything happens in a temporary directory with invented data.
"""

import contextlib
import io
import shutil
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlite_backup_check.cli import main  # noqa: E402


def build(path: Path) -> None:
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(
        """
        CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT, email TEXT);
        CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), total REAL);
        """
    )
    c.executemany(
        "INSERT INTO customers(name, email) VALUES (?, ?)",
        [(f"Customer {i}", f"customer{i}@example.com") for i in range(30000)],
    )
    c.executemany(
        "INSERT INTO orders(customer_id, total) VALUES (?, ?)",
        [(i % 30000 + 1, round(i * 0.37, 2)) for i in range(60000)],
    )
    c.commit()
    c.close()


class _Rel(io.TextIOBase):
    """Print text with the demo temp dir prefix removed (keeps the README free of local paths)."""

    def __init__(self, prefix: str):
        self.prefixes = [prefix + "\\", prefix + "/", prefix]

    def write(self, text: str) -> int:
        for p in self.prefixes:
            text = text.replace(p, "")
        for d in ("backups", "corrupted"):
            text = text.replace(d + "\\", d + "/")
        sys.__stdout__.write(text)
        return len(text)


def main_demo() -> int:
    with tempfile.TemporaryDirectory(prefix="sbc-demo-") as tmp, contextlib.redirect_stdout(_Rel(tmp)):
        tmpd = Path(tmp)
        db = tmpd / "shop.sqlite"
        out = tmpd / "backups"
        build(db)

        stop = threading.Event()
        written = [0]

        def writer():
            c = sqlite3.connect(db, timeout=30)
            while not stop.is_set():
                with c:
                    cur = c.execute("INSERT INTO customers(name, email) VALUES ('New', 'new@example.com')")
                    c.execute("INSERT INTO orders(customer_id, total) VALUES (?, 9.99)", (cur.lastrowid,))
                written[0] += 1
            c.close()

        t = threading.Thread(target=writer)
        t.start()
        try:
            print("== backup while a writer thread is inserting rows ==")
            rc = main(["backup", str(db), str(out), "--verify"])
        finally:
            stop.set()
            t.join()
        print(f"(writer inserted {written[0]} customer+order pairs meanwhile)\n")

        print("== verify a corrupted copy (bytes flipped after the backup) ==")
        good = next(out.glob("*.sqlite.gz"))
        bad_dir = tmpd / "corrupted"
        bad_dir.mkdir()
        bad = bad_dir / good.name
        shutil.copy(good, bad)
        shutil.copy(str(good) + ".manifest.json", str(bad) + ".manifest.json")
        data = bytearray(bad.read_bytes())
        mid = len(data) // 2
        for i in range(mid, mid + 16):
            data[i] ^= 0xFF
        bad.write_bytes(bytes(data))
        rc_bad = main(["verify", str(bad)])
        print(f"\nexit codes: good backup={rc}, corrupted copy={rc_bad} (expected 0 and 1)")
        return 0 if (rc == 0 and rc_bad == 1) else 2


if __name__ == "__main__":
    sys.exit(main_demo())
