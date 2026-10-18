"""Write/verify engine.

Pure byte-level logic operating on already-open file descriptors. It has no
knowledge of GTK or D-Bus: the caller obtains the descriptors (e.g. from
udisks2 ``OpenForRestore`` / ``OpenForBackup``) and runs these functions in a
worker thread.
"""

from __future__ import annotations

import enum
import errno as errno_mod
import hashlib
import os
import threading
import time
from dataclasses import dataclass, field
from typing import BinaryIO, Callable

from .archives import READ_ERRORS

CHUNK_SIZE = 4 * 1024 * 1024


class Phase(enum.Enum):
    WRITING = "writing"
    VERIFYING = "verifying"


class Status(enum.Enum):
    SUCCESS = "success"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass(frozen=True)
class Progress:
    phase: Phase
    bytes_done: int
    total: int
    speed: float  # bytes/second, averaged over the phase so far
    eta: float | None  # seconds remaining, None if unknown

    @property
    def indeterminate(self) -> bool:
        """The total is not known (compressed image of unknown size)."""
        return self.total == 0

    @property
    def fraction(self) -> float:
        return self.bytes_done / self.total if self.total else 1.0


@dataclass
class Result:
    status: Status
    phase: Phase
    bytes_done: int
    total: int
    message: str | None = None
    error_offset: int | None = None  # byte offset where the failure occurred
    errno: int | None = None


@dataclass
class WriteResult(Result):
    sha256: str | None = None  # hex digest of the source image (on success)
    # Per-chunk digests of the source, used by verify() to locate mismatches.
    chunk_digests: list[bytes] = field(default_factory=list, repr=False)
    chunk_size: int = CHUNK_SIZE


@dataclass
class VerifyResult(Result):
    sha256: str | None = None  # hex digest of the data read back


ProgressCallback = Callable[[Progress], None]


def format_eta(seconds: float | None) -> str:
    """``mm:ss`` (or ``h:mm:ss``) for an ETA; ``--:--`` if unknown."""
    if seconds is None:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _chunk_digest(data) -> bytes:
    return hashlib.blake2b(data, digest_size=16).digest()


class _ProgressTracker:
    def __init__(self, phase: Phase, total: int, callback: ProgressCallback | None):
        self.phase = phase
        self.total = total
        self.callback = callback
        self.start = time.monotonic()

    def report(self, done: int) -> None:
        if self.callback is None:
            return
        elapsed = time.monotonic() - self.start
        speed = done / elapsed if elapsed > 0 else 0.0
        eta = (self.total - done) / speed if speed > 0 and self.total else None
        self.callback(Progress(self.phase, done, self.total, speed, eta))


def _read_full(src: BinaryIO, buf: memoryview) -> int:
    """Read from ``src`` until ``buf`` is full or EOF."""
    got = 0
    while got < len(buf):
        n = src.readinto(buf[got:])
        if n == 0:
            break
        got += n
    return got


def _pread_full(fd: int, buf: memoryview, offset: int) -> int:
    """Read at ``offset`` until ``buf`` is full or EOF."""
    got = 0
    while got < len(buf):
        n = os.preadv(fd, [buf[got:]], offset + got)
        if n == 0:
            break
        got += n
    return got


def _pwrite_full(fd: int, data: memoryview, offset: int) -> None:
    """Write all of ``data`` at ``offset``, retrying short writes."""
    written = 0
    while written < len(data):
        n = os.pwrite(fd, data[written:], offset + written)
        if n == 0:
            raise OSError(errno_mod.EIO, "write returned 0 bytes")
        written += n


def write_image(
    src: BinaryIO | int,
    dst_fd: int,
    size: int | None,
    *,
    target_size: int | None = None,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    chunk_size: int = CHUNK_SIZE,
) -> WriteResult:
    """Copy ``size`` bytes from ``src`` (read sequentially) to the start of
    ``dst_fd`` and fsync it.

    ``src`` is a file object with ``readinto`` (e.g. a decompressing reader)
    or a file descriptor. If ``size`` is None the source is copied until EOF,
    and the copy fails once it would exceed ``target_size``.

    The source is hashed while streaming. Never raises for I/O errors; they
    are reported in the returned result.
    """
    if size is not None and size <= 0:
        raise ValueError("size must be positive")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    def result(status: Status, done: int, **kw) -> WriteResult:
        kw.setdefault("total", size or 0)
        total = kw.pop("total")
        return WriteResult(status, Phase.WRITING, done, total, chunk_size=chunk_size, **kw)

    if size is not None and target_size is not None and size > target_size:
        return result(
            Status.ERROR,
            0,
            message=f"Image ({size} bytes) is larger than the target ({target_size} bytes)",
        )

    if isinstance(src, int):
        with open(src, "rb", buffering=0, closefd=False) as reader:
            return _copy(reader, dst_fd, size, target_size, progress, cancel, chunk_size, result)
    return _copy(src, dst_fd, size, target_size, progress, cancel, chunk_size, result)


def _copy(src, dst_fd, size, target_size, progress, cancel, chunk_size, result) -> WriteResult:
    tracker = _ProgressTracker(Phase.WRITING, size or 0, progress)
    sha = hashlib.sha256()
    digests: list[bytes] = []
    buf = memoryview(bytearray(chunk_size))
    done = 0
    tracker.report(0)

    while size is None or done < size:
        if cancel is not None and cancel.is_set():
            return result(Status.CANCELLED, done, message="Cancelled", chunk_digests=digests)

        want = chunk_size if size is None else min(chunk_size, size - done)
        chunk = buf[:want]
        try:
            got = _read_full(src, chunk)
        except READ_ERRORS as e:
            reason = getattr(e, "strerror", None) or "the archive is corrupt or truncated"
            return result(
                Status.ERROR, done, message=f"Error reading image: {reason}",
                error_offset=done, errno=getattr(e, "errno", None),
            )
        if size is None:
            if got == 0:
                break
            if target_size is not None and done + got > target_size:
                return result(
                    Status.ERROR, done,
                    message=f"Image is larger than the target ({target_size} bytes)",
                    error_offset=done, chunk_digests=digests,
                )
            chunk = buf[:got]
        elif got < want:
            return result(
                Status.ERROR, done + got,
                message=f"Image ended unexpectedly at byte {done + got} (expected {size})",
                error_offset=done + got,
            )

        try:
            _pwrite_full(dst_fd, chunk, done)
        except OSError as e:
            return result(
                Status.ERROR, done, message=f"Error writing to drive: {e.strerror}",
                error_offset=done, errno=e.errno,
            )

        sha.update(chunk)
        digests.append(_chunk_digest(chunk))
        done += len(chunk)
        tracker.report(done)

    if size is not None:
        # A reader that yields more than it announced would verify a prefix.
        try:
            extra = src.read(1)
        except READ_ERRORS:
            extra = b""
        if extra:
            return result(
                Status.ERROR, done,
                message=f"Image is larger than expected ({size} bytes)",
                error_offset=done,
            )
    elif done == 0:
        return result(Status.ERROR, 0, message="Image is empty")

    try:
        os.fsync(dst_fd)
    except OSError as e:
        return result(
            Status.ERROR, done, message=f"Error flushing data to drive: {e.strerror}",
            errno=e.errno,
        )

    return result(
        Status.SUCCESS, done, total=done, sha256=sha.hexdigest(), chunk_digests=digests,
    )


def verify(
    dst_fd: int,
    written: WriteResult,
    *,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> VerifyResult:
    """Read back the first ``written.total`` bytes of ``dst_fd`` and compare
    them against the digests recorded by :func:`write_image`."""
    if written.status is not Status.SUCCESS:
        raise ValueError("can only verify a successful write")

    size = written.total
    chunk_size = written.chunk_size

    def result(status: Status, done: int, **kw) -> VerifyResult:
        return VerifyResult(status, Phase.VERIFYING, done, size, **kw)

    # Drop cached pages so we read what is actually on the device.
    try:
        os.posix_fadvise(dst_fd, 0, 0, os.POSIX_FADV_DONTNEED)
    except OSError:
        pass  # advisory only

    tracker = _ProgressTracker(Phase.VERIFYING, size, progress)
    sha = hashlib.sha256()
    buf = memoryview(bytearray(chunk_size))
    done = 0
    index = 0
    tracker.report(0)

    while done < size:
        if cancel is not None and cancel.is_set():
            return result(Status.CANCELLED, done, message="Cancelled")

        want = min(chunk_size, size - done)
        chunk = buf[:want]
        try:
            got = _pread_full(dst_fd, chunk, done)
        except OSError as e:
            return result(
                Status.ERROR, done, message=f"Error reading back from drive: {e.strerror}",
                error_offset=done, errno=e.errno,
            )
        if got < want:
            return result(
                Status.ERROR, done + got,
                message=f"Drive ended unexpectedly at byte {done + got} (expected {size})",
                error_offset=done + got,
            )

        if _chunk_digest(chunk) != written.chunk_digests[index]:
            return result(
                Status.ERROR, done,
                message=f"Verification failed: data mismatch in chunk at byte {done}",
                error_offset=done,
            )

        sha.update(chunk)
        done += want
        index += 1
        tracker.report(done)

    digest = sha.hexdigest()
    if digest != written.sha256:  # defensive; chunk digests already matched
        return result(Status.ERROR, done, message="Verification failed: checksum mismatch")
    return result(Status.SUCCESS, done, sha256=digest)
