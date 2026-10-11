"""Source image validation."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_EXTENSIONS = (".img", ".iso", ".raw")
COMPRESSED_EXTENSIONS = (".gz", ".xz", ".zst", ".bz2", ".zip", ".7z")

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
    size: int
    is_iso9660: bool
    # None if not an ISO 9660 image; otherwise whether it has an MBR boot
    # signature and is therefore likely bootable when written to USB.
    is_hybrid: bool | None
    warnings: list[str] = field(default_factory=list)


def _read_at(f, offset: int, length: int) -> bytes:
    f.seek(offset)
    return f.read(length)


def inspect_image(path: str | os.PathLike) -> ImageInfo:
    """Validate ``path`` as a source image. Raises :class:`ImageError`."""
    path = Path(path)
    name = path.name.lower()

    if name.endswith(COMPRESSED_EXTENSIONS):
        raise ImageError(f"Compressed images are not supported yet: {path.name}")
    if not name.endswith(SUPPORTED_EXTENSIONS):
        raise ImageError(
            f"Unsupported file type: {path.name} "
            f"(supported: {', '.join(SUPPORTED_EXTENSIONS)})"
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

    try:
        with path.open("rb") as f:
            mbr_sig = _read_at(f, _MBR_SIG_OFFSET, len(_MBR_SIG))
            iso_magic = _read_at(f, _ISO9660_MAGIC_OFFSET, len(_ISO9660_MAGIC))
    except OSError as e:
        raise ImageError(f"Cannot read {path}: {e.strerror}") from None

    is_iso = iso_magic == _ISO9660_MAGIC
    info = ImageInfo(
        path=path,
        size=st.st_size,
        is_iso9660=is_iso,
        is_hybrid=(mbr_sig == _MBR_SIG) if is_iso else None,
    )
    if is_iso and not info.is_hybrid:
        info.warnings.append(
            "This ISO image does not appear to be a hybrid image and may not "
            "boot when written to a USB drive."
        )
    return info


def check_fits(image_size: int, target_size: int) -> None:
    """Raise :class:`ImageError` if the image does not fit on the target."""
    if image_size > target_size:
        raise ImageError(
            f"Image ({image_size} bytes) is larger than the target drive "
            f"({target_size} bytes)"
        )
