"""An exclusive lock on the data directory.

The server holds it while running, so a second server can't use the same data, and offline
maintenance (master key rotation) can tell that the server is still running. The OS releases
the lock when the process exits, even after a crash, so it never goes stale.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

LOCK_FILE = "via.lock"


class DataDirLocked(Exception):
    def __init__(self, data_dir: Path) -> None:
        super().__init__(f"{data_dir} is in use by a running Via server")
        self.data_dir = data_dir


def _lock(fh: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fh: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class DataDirLock:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._fh: BinaryIO | None = None

    def acquire(self) -> None:
        """Take the lock or raise :class:`DataDirLocked` right away (never waits)."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fh = (self.data_dir / LOCK_FILE).open("a+b")
        try:
            _lock(fh)
        except OSError as e:
            fh.close()
            raise DataDirLocked(self.data_dir) from e
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            _unlock(self._fh)
            self._fh.close()
            self._fh = None

    def __enter__(self) -> DataDirLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
