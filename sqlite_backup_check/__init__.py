"""sqlite-backup-check: safe SQLite backups with verification and rotation."""

__version__ = "0.1.0"

from .backup import BackupEntry, BackupError, backup, list_backups  # noqa: E402
from .crypto import DecryptError, load_passphrase  # noqa: E402
from .rotate import rotate, select_keep  # noqa: E402
from .verify import VerifyReport, verify_backup  # noqa: E402

__all__ = [
    "BackupEntry",
    "BackupError",
    "DecryptError",
    "VerifyReport",
    "backup",
    "list_backups",
    "load_passphrase",
    "rotate",
    "select_keep",
    "verify_backup",
]
