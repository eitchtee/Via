"""Filesystem store for encrypted file blobs, one file per push."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

log = logging.getLogger(__name__)

PARTIAL_SUFFIX = ".part"


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def path(self, push_id: str) -> Path:
        # Shard on the (random) last characters of the ULID.
        return self.root / push_id[-2:].lower() / push_id

    def partial_path(self, push_id: str) -> Path:
        return self.path(push_id).with_name(push_id + PARTIAL_SUFFIX)

    def open_partial(self, push_id: str) -> BinaryIO:
        path = self.partial_path(push_id)
        path.parent.mkdir(exist_ok=True)
        return path.open("xb")

    def commit_partial(self, push_id: str) -> None:
        os.replace(self.partial_path(push_id), self.path(push_id))

    def exists(self, push_id: str) -> bool:
        return self.path(push_id).is_file()

    def delete(self, push_id: str) -> None:
        for path in (self.path(push_id), self.partial_path(push_id)):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # e.g. still open for download on Windows; the janitor retries later.
                log.warning("could not delete blob %s", path, exc_info=True)

    def scan(self) -> Iterator[tuple[str, Path, bool, float]]:
        """Yield ``(push_id, path, is_partial, mtime)`` for every stored file."""
        for shard in self.root.iterdir():
            if not shard.is_dir():
                continue
            for path in shard.iterdir():
                partial = path.name.endswith(PARTIAL_SUFFIX)
                yield path.name.removesuffix(PARTIAL_SUFFIX), path, partial, path.stat().st_mtime
