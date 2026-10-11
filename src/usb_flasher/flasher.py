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
from typing import Callable

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
        eta = (self.total - done) / speed if speed > 0 else None
        self.callback(Progress(self.phase, done, self.total, speed, eta))


def _read_full(fd: int, buf: memoryview) -> int:
    """Read from the current position of ``fd`` until ``buf`` is full or EOF."""
    got = 0
    while got < len(buf):
        n = os.readv(fd, [buf[got:]])
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
    src_fd: int,
    dst_fd: int,
    size: int,
    *,
    target_size: int | None = None,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    chunk_size: int = CHUNK_SIZE,
) -> WriteResult:
    """Copy ``size`` bytes from ``src_fd`` (read sequentially) to the start
    of ``dst_fd`` and fsync it.

    The source is hashed while streaming. Never raises for I/O errors; they
    are reported in the returned result.
    """
    if size <= 0:
        raise ValueError("size must be positive")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    def result(status: Status, done: int, **kw) -> WriteResult:
        return WriteResult(status, Phase.WRITING, done, size, chunk_size=chunk_size, **kw)

    if target_size is not None and size > target_size:
        return result(
            Status.ERROR,
            0,
            message=f"Image ({size} bytes) is larger than the target ({target_size} bytes)",
        )

    tracker = _ProgressTracker(Phase.WRITING, size, progress)
    sha = hashlib.sha256()
    digests: list[bytes] = []
    buf = memoryview(bytearray(chunk_size))
    done = 0
    tracker.report(0)

    while done < size:
        if cancel is not None and cancel.is_set():
            return result(Status.CANCELLED, done, message="Cancelled", chunk_digests=digests)

        want = min(chunk_size, size - done)
        chunk = buf[:want]
        try:
            got = _read_full(src_fd, chunk)
        except OSError as e:
            return result(
                Status.ERROR, done, message=f"Error reading image: {e.strerror}",
                error_offset=done, errno=e.errno,
            )
        if got < want:
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
        done += want
        tracker.report(done)

    try:
        os.fsync(dst_fd)
    except OSError as e:
        return result(
            Status.ERROR, done, message=f"Error flushing data to drive: {e.strerror}",
            errno=e.errno,
        )

    return result(Status.SUCCESS, done, sha256=sha.hexdigest(), chunk_digests=digests)


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
