"""Streaming readers for compressed source images."""

from __future__ import annotations

import bz2
import contextlib
import gzip
import lzma
import zipfile
import zlib
from pathlib import Path
from typing import BinaryIO, Iterator

try:
    from compression import zstd  # Python 3.14+
except ImportError:  # pragma: no cover - depends on interpreter version
    zstd = None

# Extensions of the image inside the archive that we are willing to flash.
IMAGE_EXTENSIONS = (".img", ".iso", ".raw")
COMPRESSION_EXTENSIONS = (".gz", ".xz", ".zst", ".bz2", ".zip")

# Everything a decompressor may raise on truncated or corrupt input.
READ_ERRORS = (OSError, EOFError, lzma.LZMAError, zlib.error, zipfile.BadZipFile)


class ArchiveError(Exception):
    """The archive cannot be read or does not contain a usable image."""


def compression_of(path: Path) -> str | None:
    """The compression suffix of ``path`` (e.g. ``".xz"``), or None."""
    suffix = path.suffix.lower()
    return suffix if suffix in COMPRESSION_EXTENSIONS else None


def _zip_member(zf: zipfile.ZipFile, archive: Path) -> zipfile.ZipInfo:
    files = [i for i in zf.infolist() if not i.is_dir()]
    images = [i for i in files if i.filename.lower().endswith(IMAGE_EXTENSIONS)]
    if len(images) == 1:
        return images[0]
    if not images:
        raise ArchiveError(
            f"{archive.name} contains no disk image ({', '.join(IMAGE_EXTENSIONS)})"
        )
    raise ArchiveError(f"{archive.name} contains several disk images; extract one first")


def _xz_size(f: BinaryIO, file_size: int) -> int | None:
    """Sum the uncompressed sizes recorded in the xz stream indexes."""

    def vli(buf: bytes, pos: int) -> tuple[int, int]:
        value = 0
        for i in range(9):
            b = buf[pos + i]
            value |= (b & 0x7F) << (7 * i)
            if not b & 0x80:
                return value, pos + i + 1
        raise ValueError("bad variable-length integer")

    total = 0
    end = file_size
    try:
        while end > 0:
            while end >= 4:  # stream padding
                f.seek(end - 4)
                if f.read(4) != b"\0\0\0\0":
                    break
                end -= 4
            if end == 0:
                break
            if end < 24:
                return None
            f.seek(end - 12)
            footer = f.read(12)
            if footer[10:] != b"YZ":
                return None
            index_size = (int.from_bytes(footer[4:8], "little") + 1) * 4
            if index_size + 24 > end:
                return None
            f.seek(end - 12 - index_size)
            index = f.read(index_size)
            if index[0] != 0:
                return None
            count, pos = vli(index, 1)
            blocks = 0
            for _ in range(count):
                unpadded, pos = vli(index, pos)
                uncompressed, pos = vli(index, pos)
                total += uncompressed
                blocks += (unpadded + 3) // 4 * 4
            end -= 12 + index_size + blocks + 12
            if end < 0:
                return None
    except (OSError, IndexError, ValueError):
        return None
    return total


def _zstd_size(f: BinaryIO) -> int | None:
    if zstd is None:
        return None
    try:
        return zstd.get_frame_info(f.read(18)).decompressed_size
    except (zstd.ZstdError, ValueError):
        return None


def uncompressed_size(path: Path, kind: str) -> int | None:
    """Uncompressed size from the archive metadata, or None if it is not
    stored reliably (gzip, bz2, zstd without a size in the frame header)."""
    try:
        if kind == ".zip":
            with zipfile.ZipFile(path) as zf:
                return _zip_member(zf, path).file_size
        with path.open("rb") as f:
            if kind == ".xz":
                return _xz_size(f, path.stat().st_size)
            if kind == ".zst":
                return _zstd_size(f)
    except zipfile.BadZipFile:
        raise ArchiveError(f"Not a valid zip archive: {path.name}") from None
    return None


@contextlib.contextmanager
def open_stream(path: Path, kind: str) -> Iterator[BinaryIO]:
    """Open ``path`` and yield a file object over the decompressed data."""
    if kind == ".zip":
        try:
            zf = zipfile.ZipFile(path)
        except zipfile.BadZipFile:
            raise ArchiveError(f"Not a valid zip archive: {path.name}") from None
        with zf:
            with zf.open(_zip_member(zf, path)) as f:
                yield f
        return
    if kind == ".zst":
        if zstd is None:
            raise ArchiveError("Reading .zst images needs Python 3.14 or newer")
        opener = zstd.open
    else:
        opener = {".gz": gzip.open, ".xz": lzma.open, ".bz2": bz2.open}[kind]
    with opener(path, "rb") as f:
        yield f
