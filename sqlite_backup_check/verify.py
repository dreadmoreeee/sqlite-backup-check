"""Verify a backup: checksum, decrypt/decompress, integrity, FK check, row counts."""

from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import crypto
from .backup import (
    BackupError,
    decompress_file,
    load_entry,
    sha256_file,
    table_row_counts,
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class VerifyReport:
    path: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, ok, detail))
        return ok

    def format(self) -> str:
        lines = [f"{'OK  ' if self.ok else 'FAIL'} {self.path}"]
        for c in self.checks:
            mark = "ok  " if c.ok else "FAIL"
            lines.append(f"  [{mark}] {c.name}" + (f": {c.detail}" if c.detail else ""))
        return "\n".join(lines)


def verify_backup(backup_path, passphrase: str | None = None) -> VerifyReport:
    """Verify one backup file (its manifest must sit next to it). Never raises on
    a bad backup; problems are recorded as failed checks."""
    report = VerifyReport(str(backup_path))
    try:
        entry = load_entry(backup_path)
    except BackupError as exc:
        report.add("manifest", False, str(exc))
        return report
    m = entry.manifest
    report.add("manifest", True, f"created {m['created']}, method {m.get('method')}")
    path = entry.path
    if not path.is_file():
        report.add("checksum", False, "backup file is missing")
        return report
    actual = sha256_file(path)
    if actual != m.get("sha256"):
        report.add("checksum", False, f"sha256 mismatch (expected {m.get('sha256')}, got {actual})")
        return report
    report.add("checksum", True, f"sha256 {actual[:16]}...")

    with tempfile.TemporaryDirectory(prefix="sbc-verify-") as tmp:
        tmpd = Path(tmp)
        stage = path
        if m.get("encrypted"):
            if passphrase is None:
                report.add("decrypt", False, "backup is encrypted and no passphrase was given")
                return report
            out = tmpd / "dec"
            try:
                crypto.decrypt_file(stage, out, passphrase)
            except (crypto.DecryptError, crypto.CryptoUnavailable) as exc:
                report.add("decrypt", False, str(exc))
                return report
            report.add("decrypt", True, "AES-256-GCM tag valid")
            stage = out
        raw = tmpd / "restored.sqlite"
        try:
            decompress_file(stage, raw, m.get("compression", "none"))
        except Exception as exc:  # corrupt or truncated stream, missing codec
            report.add("decompress", False, f"{type(exc).__name__}: {exc}")
            return report
        report.add("decompress", True, m.get("compression", "none"))

        try:
            conn = sqlite3.connect(str(raw))
            try:
                integ = [r[0] for r in conn.execute("PRAGMA integrity_check")]
                report.add(
                    "integrity_check",
                    integ == ["ok"],
                    "ok" if integ == ["ok"] else "; ".join(integ[:5]),
                )
                fk = conn.execute("PRAGMA foreign_key_check").fetchall()
                report.add(
                    "foreign_key_check",
                    not fk,
                    "no violations" if not fk else f"{len(fk)} violation(s), first: {fk[0]}",
                )
                counts = table_row_counts(conn)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            report.add("integrity_check", False, f"{type(exc).__name__}: {exc}")
            return report

    expected = m.get("row_counts", {})
    if counts == expected:
        total = sum(counts.values())
        report.add("row_counts", True, f"{len(counts)} table(s), {total} rows match the manifest")
    else:
        diffs = [
            f"{t}: manifest={expected.get(t)} restored={counts.get(t)}"
            for t in sorted(set(expected) | set(counts))
            if expected.get(t) != counts.get(t)
        ]
        report.add("row_counts", False, "; ".join(diffs))
    return report
