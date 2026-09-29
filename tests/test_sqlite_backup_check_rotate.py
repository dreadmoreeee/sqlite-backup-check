import sqlite3
from datetime import datetime, timedelta

from sqlite_backup_check import backup, list_backups, rotate, select_keep
from sqlite_backup_check.cli import main


def d(y, m, dd, h=3):
    return datetime(y, m, dd, h, 0, 0)


def test_daily_keeps_newest_per_day():
    stamps = [d(2026, 3, 10, 1), d(2026, 3, 10, 22), d(2026, 3, 9, 5), d(2026, 3, 8, 5)]
    keep = select_keep(stamps, daily=2, weekly=0, monthly=0)
    assert keep == {d(2026, 3, 10, 22), d(2026, 3, 9, 5)}


def test_weekly_uses_iso_weeks():
    # 2026-03-09 is a Monday. Sun 03-08 belongs to the previous ISO week.
    stamps = [d(2026, 3, 11), d(2026, 3, 9), d(2026, 3, 8), d(2026, 3, 2), d(2026, 2, 23)]
    keep = select_keep(stamps, daily=0, weekly=2, monthly=0)
    assert keep == {d(2026, 3, 11), d(2026, 3, 8)}


def test_monthly_and_year_boundary():
    stamps = [d(2026, 1, 31), d(2026, 1, 2), d(2025, 12, 31), d(2025, 12, 1), d(2025, 11, 15)]
    keep = select_keep(stamps, daily=0, weekly=0, monthly=2)
    assert keep == {d(2026, 1, 31), d(2025, 12, 31)}


def test_tiers_combine_and_daily_counts_days_with_backups():
    stamps = [d(2026, 4, 1) - timedelta(days=i) for i in range(0, 60)]
    keep = select_keep(stamps, daily=7, weekly=4, monthly=3)
    assert set(stamps[:7]) <= keep
    assert len(keep) <= 7 + 4 + 3
    # gaps: only days that have a backup count
    sparse = [d(2026, 4, 1), d(2026, 3, 1), d(2026, 2, 1)]
    assert select_keep(sparse, daily=2, weekly=0, monthly=0) == {d(2026, 4, 1), d(2026, 3, 1)}


def test_zero_keep_and_empty():
    assert select_keep([], 7, 4, 6) == set()
    assert select_keep([d(2026, 1, 1)], 0, 0, 0) == set()


def _populate(tmp_path, n=10):
    src = tmp_path / "app.sqlite"
    c = sqlite3.connect(src)
    c.execute("CREATE TABLE t(x)")
    c.execute("INSERT INTO t VALUES (1)")
    c.commit()
    c.close()
    bk = tmp_path / "bk"
    for i in range(n):
        backup(src, bk, now=d(2026, 5, 20) - timedelta(days=i))
    return bk


def test_rotate_dry_run_then_real(tmp_path):
    bk = _populate(tmp_path)
    kept, removed = rotate(bk, 3, 0, 0, dry_run=True)
    assert len(kept) == 3 and len(removed) == 7
    assert len(list_backups(bk)) == 10
    assert len(list(bk.glob("*.gz"))) == 10
    rotate(bk, 3, 0, 0)
    left = list_backups(bk)
    assert [e.created for e in left] == [d(2026, 5, 18), d(2026, 5, 19), d(2026, 5, 20)]
    assert len(list(bk.glob("*.gz"))) == 3
    assert len(list(bk.glob("*.manifest.json"))) == 3


def test_cli_rotate_list_verify_exit_codes(tmp_path, capsys):
    bk = _populate(tmp_path, 5)
    assert main(["rotate", str(bk), "--keep-daily", "2", "--keep-weekly", "0",
                 "--keep-monthly", "0", "--dry-run"]) == 0
    assert "would remove" in capsys.readouterr().out
    assert main(["list", str(bk)]) == 0
    assert main(["verify", str(bk)]) == 0
    victim = next(bk.glob("*.gz"))
    victim.write_bytes(victim.read_bytes()[:-5])
    assert main(["verify", str(bk)]) == 1
    assert main(["verify", str(tmp_path / "missing")]) == 2
    assert main(["list", str(tmp_path / "missing")]) == 2
    assert main(["bogus"]) == 2
