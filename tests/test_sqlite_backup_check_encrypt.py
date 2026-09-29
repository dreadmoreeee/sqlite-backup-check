import sqlite3

import pytest

pytest.importorskip("cryptography")

from sqlite_backup_check import backup, verify_backup  # noqa: E402
from sqlite_backup_check.crypto import HEADER_SIZE, DecryptError, decrypt_file, load_passphrase  # noqa: E402


def make_db(path):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, body TEXT)")
    c.executemany("INSERT INTO notes(body) VALUES (?)", [(f"note {i}",) for i in range(100)])
    c.commit()
    c.close()


def test_encryption_round_trip(tmp_path):
    src = tmp_path / "n.sqlite"
    make_db(src)
    e = backup(src, tmp_path / "bk", passphrase="correct horse battery staple")
    assert e.path.name.endswith(".sqlite.gz.enc")
    blob = e.path.read_bytes()
    assert blob[:4] == b"SBCE" and blob[4] == 1
    assert b"SQLite format 3" not in blob
    rep = verify_backup(e.path, "correct horse battery staple")
    assert rep.ok, rep.format()


def test_wrong_passphrase_fails(tmp_path):
    src = tmp_path / "n.sqlite"
    make_db(src)
    e = backup(src, tmp_path / "bk", passphrase="right")
    rep = verify_backup(e.path, "wrong")
    assert not rep.ok
    assert any(c.name == "decrypt" and not c.ok for c in rep.checks)
    assert not verify_backup(e.path, None).ok
    with pytest.raises(DecryptError):
        decrypt_file(e.path, tmp_path / "out", "wrong")


def test_two_backups_use_different_salt_and_nonce(tmp_path):
    src = tmp_path / "n.sqlite"
    make_db(src)
    a = backup(src, tmp_path / "bk", name="a", passphrase="pw").path.read_bytes()
    b = backup(src, tmp_path / "bk", name="b", passphrase="pw").path.read_bytes()
    assert a[:HEADER_SIZE] != b[:HEADER_SIZE]


def test_tampered_header_rejected(tmp_path):
    src = tmp_path / "n.sqlite"
    make_db(src)
    e = backup(src, tmp_path / "bk", passphrase="pw")
    data = bytearray(e.path.read_bytes())
    data[6] ^= 0x01  # KDF parameter byte
    (tmp_path / "t.enc").write_bytes(bytes(data))
    with pytest.raises(DecryptError):
        decrypt_file(tmp_path / "t.enc", tmp_path / "o", "pw")


def test_passphrase_sources(tmp_path, monkeypatch):
    f = tmp_path / "pw.txt"
    f.write_text("s3cret\n", encoding="utf-8")
    assert load_passphrase(file=f) == "s3cret"
    monkeypatch.setenv("SBC_TEST_PW", "fromenv")
    assert load_passphrase(env="SBC_TEST_PW") == "fromenv"
    assert load_passphrase() is None
    with pytest.raises(ValueError):
        load_passphrase(env="SBC_UNSET_VAR_XYZ")
    with pytest.raises(ValueError):
        load_passphrase(env="SBC_TEST_PW", file=f)
