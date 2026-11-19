"""The artifact file store (design M4 section 9.2). The only module that reads or writes artifact files.

Content-addressed: <root>/<sha256[0:2]>/<sha256>. Files are written to <root>/tmp/<id> with mode 0600,
hashed while written, fsynced, then renamed into place. A file with the same hash is reused.
"""

import hashlib
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from ..ids import new_id
from .models import HubError


@dataclass
class TempFile:
    path: Path
    sha256: str
    size: int


class TooLarge(HubError):
    def __init__(self, limit: int, hint: str = ""):
        super().__init__("too_large", f"the file is larger than {limit} bytes{hint}", 413)


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        (self.root / "tmp").mkdir(exist_ok=True, mode=0o700)

    def path(self, sha256: str) -> Path:
        if len(sha256) != 64 or not all(c in "0123456789abcdef" for c in sha256):
            raise ValueError("not a sha256 hex digest")
        return self.root / sha256[:2] / sha256

    def _open_temp(self) -> tuple[Path, int]:
        self.ensure()
        path = self.root / "tmp" / new_id()
        return path, os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    def write_bytes(self, data: bytes, max_bytes: int, hint: str = "") -> TempFile:
        if len(data) > max_bytes:
            raise TooLarge(max_bytes, hint)
        path, fd = self._open_temp()
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        return TempFile(path, hashlib.sha256(data).hexdigest(), len(data))

    async def write_stream(self, chunks: AsyncIterator[bytes], max_bytes: int) -> TempFile:
        """Stops reading at max_bytes + 1 and deletes the partial file, so an oversized upload never fills
        the disk."""
        path, fd = self._open_temp()
        digest, size = hashlib.sha256(), 0
        try:
            async for chunk in chunks:
                size += len(chunk)
                if size > max_bytes:
                    raise TooLarge(max_bytes)
                digest.update(chunk)
                os.write(fd, chunk)
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            path.unlink(missing_ok=True)
            raise
        os.close(fd)
        return TempFile(path, digest.hexdigest(), size)

    def read_temp(self, temp: TempFile) -> bytes:
        return temp.path.read_bytes()

    def commit(self, temp: TempFile) -> Path:
        final = self.path(temp.sha256)
        final.parent.mkdir(exist_ok=True, mode=0o700)
        if final.exists():
            temp.path.unlink(missing_ok=True)                 # same content is stored once
        else:
            os.replace(temp.path, final)
        return final

    def discard(self, temp: TempFile) -> None:
        temp.path.unlink(missing_ok=True)

    def read(self, sha256: str) -> bytes:
        return self.path(sha256).read_bytes()

    def gc(self, live: set[str], temp_age: float = 3600) -> int:
        """Remove stored files no live artifact row references, and temp files older than `temp_age` seconds
        (a younger one may belong to an upload in progress). Returns the number of files removed."""
        if not self.root.is_dir():
            return 0
        removed = 0
        for folder in self.root.iterdir():
            if folder.name == "tmp" or not folder.is_dir():
                continue
            for file in folder.iterdir():
                if file.name not in live:
                    file.unlink(missing_ok=True)
                    removed += 1
        cutoff = time.time() - temp_age
        for file in (self.root / "tmp").glob("*"):
            if file.stat().st_mtime < cutoff:
                file.unlink(missing_ok=True)
        return removed
