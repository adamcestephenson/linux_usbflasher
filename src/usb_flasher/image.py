"""Source image validation."""

from __future__ import annotations

import contextlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator

from . import archives

SUPPORTED_EXTENSIONS = (".img", ".iso", ".raw")
COMPRESSED_EXTENSIONS = archives.COMPRESSION_EXTENSIONS
UNSUPPORTED_COMPRESSED_EXTENSIONS = (".7z",)

# ISO 9660 primary volume descriptor: sector 16 (offset 32768), "CD001" at +1.
_ISO9660_MAGIC_OFFSET = 32769
_ISO9660_MAGIC = b"CD001"
# MBR boot signature at bytes 510-511; present on hybrid ISOs.
_MBR_SIG_OFFSET = 510
_MBR_SIG = b"\x55\xaa"


class ImageError(Exception):
    """The file cannot be used as a source image."""


@dataclass
class ImageInfo:
    path: Path
    # Image size in bytes; for compressed files the uncompressed size, or
    # None when the archive does not record it reliably.
    size: int | None
    is_iso9660: bool
    # None if not an ISO 9660 image; otherwise whether it has an MBR boot
    # signature and is therefore likely bootable when written to USB.
    is_hybrid: bool | None
    warnings: list[str] = field(default_factory=list)
    compression: str | None = None  # e.g. ".xz"; None for raw images
    file_size: int = 0  # size of the file on disk

    def open(self):
        """Context manager yielding a file object over the (decompressed) image."""
        return open_image(self.path, self.compression)


def _read_exactly(f, length: int) -> bytes:
    data = b""
    while len(data) < length:
        chunk = f.read(length - len(data))
        if not chunk:
            break
        data += chunk
    return data


@contextlib.contextmanager
def open_image(path: Path, compression: str | None = None) -> Iterator[BinaryIO]:
    """Open the image for sequential reading, decompressing if needed."""
    if compression is None:
        with path.open("rb", buffering=0) as f:
            yield f
    else:
        with archives.open_stream(path, compression) as f:
            yield f


def inspect_image(path: str | os.PathLike) -> ImageInfo:
    """Validate ``path`` as a source image. Raises :class:`ImageError`."""
    path = Path(path)
    name = path.name.lower()

    if name.endswith(UNSUPPORTED_COMPRESSED_EXTENSIONS):
        raise ImageError(f"Unsupported archive format: {path.name}")
    compression = archives.compression_of(path)
    # The extension that identifies the image itself (before any compression).
    image_name = name[: -len(compression)] if compression and compression != ".zip" else name
    if compression != ".zip" and not image_name.endswith(SUPPORTED_EXTENSIONS):
        raise ImageError(
            f"Unsupported file type: {path.name} "
            f"(supported: {', '.join(SUPPORTED_EXTENSIONS)}, optionally compressed "
            f"with {', '.join(COMPRESSED_EXTENSIONS)})"
        )

    try:
        st = path.stat()
    except FileNotFoundError:
        raise ImageError(f"File not found: {path}") from None
    except OSError as e:
        raise ImageError(f"Cannot access {path}: {e.strerror}") from None
    if not stat.S_ISREG(st.st_mode):
        raise ImageError(f"Not a regular file: {path}")
    if st.st_size == 0:
        raise ImageError(f"Image is empty: {path}")

    size: int | None = st.st_size
    try:
        if compression:
            size = archives.uncompressed_size(path, compression)
            if size == 0:
                raise ImageError(f"Image is empty: {path}")
        with open_image(path, compression) as f:
            header = _read_exactly(f, _ISO9660_MAGIC_OFFSET + len(_ISO9660_MAGIC))
    except archives.ArchiveError as e:
        raise ImageError(str(e)) from None
    except archives.READ_ERRORS as e:
        if compression:
            raise ImageError(f"Cannot read {path.name}: the archive is corrupt or truncated") from None
        raise ImageError(f"Cannot read {path}: {getattr(e, 'strerror', None) or e}") from None
    if not header:
        raise ImageError(f"Image is empty: {path}")

    mbr_sig = header[_MBR_SIG_OFFSET:_MBR_SIG_OFFSET + len(_MBR_SIG)]
    iso_magic = header[_ISO9660_MAGIC_OFFSET:]
    is_iso = iso_magic == _ISO9660_MAGIC
    info = ImageInfo(
        path=path,
        size=size,
        is_iso9660=is_iso,
        is_hybrid=(mbr_sig == _MBR_SIG) if is_iso else None,
        compression=compression,
        file_size=st.st_size,
    )
    if is_iso and not info.is_hybrid:
        info.warnings.append(
            "This ISO image does not appear to be a hybrid image and may not "
            "boot when written to a USB drive."
        )
    return info


def check_fits(image_size: int | None, target_size: int) -> None:
    """Raise :class:`ImageError` if the image does not fit on the target.

    An unknown size passes; the write itself is capped at the drive size."""
    if image_size is not None and image_size > target_size:
        raise ImageError(
            f"Image ({image_size} bytes) is larger than the target drive "
            f"({target_size} bytes)"
        )
