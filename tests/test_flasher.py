import errno
import hashlib
import os
import threading

import pytest

from usb_flasher import flasher
from usb_flasher.flasher import Phase, Status, verify, write_image

CHUNK = 1000
SIZE = 10_500  # deliberately not a multiple of CHUNK


@pytest.fixture
def data():
    return os.urandom(SIZE)


@pytest.fixture
def fds(tmp_path, data):
    """(src_fd, dst_fd, dst_path); dst is pre-filled with zeros like a drive."""
    src = tmp_path / "image.img"
    dst = tmp_path / "drive"
    src.write_bytes(data)
    dst.write_bytes(b"\0" * (SIZE * 2))
    src_fd = os.open(src, os.O_RDONLY)
    dst_fd = os.open(dst, os.O_RDWR)
    yield src_fd, dst_fd, dst
    os.close(src_fd)
    os.close(dst_fd)


def test_write_and_verify_success(fds, data):
    src_fd, dst_fd, dst = fds
    res = write_image(src_fd, dst_fd, SIZE, chunk_size=CHUNK)

    assert res.status is Status.SUCCESS
    assert res.bytes_done == SIZE
    assert res.sha256 == hashlib.sha256(data).hexdigest()
    assert len(res.chunk_digests) == 11
    written = dst.read_bytes()
    assert written[:SIZE] == data
    assert written[SIZE:] == b"\0" * SIZE  # nothing past the image touched

    v = verify(dst_fd, res)
    assert v.status is Status.SUCCESS
    assert v.phase is Phase.VERIFYING
    assert v.sha256 == res.sha256


def test_default_chunk_size_with_small_image(fds, data):
    src_fd, dst_fd, dst = fds
    res = write_image(src_fd, dst_fd, SIZE)
    assert res.status is Status.SUCCESS
    assert dst.read_bytes()[:SIZE] == data
    assert verify(dst_fd, res).status is Status.SUCCESS


def test_progress_reports(fds):
    src_fd, dst_fd, _ = fds
    events = []
    res = write_image(src_fd, dst_fd, SIZE, chunk_size=CHUNK, progress=events.append)
    assert res.status is Status.SUCCESS

    done = [p.bytes_done for p in events]
    assert done[0] == 0
    assert done[-1] == SIZE
    assert done == sorted(done)
    assert all(p.phase is Phase.WRITING and p.total == SIZE for p in events)
    assert events[-1].fraction == 1.0

    vevents = []
    verify(dst_fd, res, progress=vevents.append)
    assert vevents[-1].bytes_done == SIZE
    assert all(p.phase is Phase.VERIFYING for p in vevents)


def test_short_writes_are_retried(fds, data, monkeypatch):
    src_fd, dst_fd, dst = fds
    real_pwrite = os.pwrite

    def short_pwrite(fd, buf, offset):
        return real_pwrite(fd, bytes(buf[:7]), offset)

    monkeypatch.setattr(flasher.os, "pwrite", short_pwrite)
    res = write_image(src_fd, dst_fd, SIZE, chunk_size=CHUNK)
    assert res.status is Status.SUCCESS
    assert dst.read_bytes()[:SIZE] == data


def test_short_reads_are_retried(fds, data, monkeypatch):
    src_fd, dst_fd, dst = fds
    real_readv = os.readv

    def short_readv(fd, buffers):
        return real_readv(fd, [buffers[0][:13]])

    monkeypatch.setattr(flasher.os, "readv", short_readv)
    res = write_image(src_fd, dst_fd, SIZE, chunk_size=CHUNK)
    assert res.status is Status.SUCCESS
    assert dst.read_bytes()[:SIZE] == data


def test_cancel_during_write(fds):
    src_fd, dst_fd, _ = fds
    cancel = threading.Event()

    def on_progress(p):
        if p.bytes_done >= 3 * CHUNK:
            cancel.set()

    res = write_image(
        src_fd, dst_fd, SIZE, chunk_size=CHUNK, progress=on_progress, cancel=cancel
    )
    assert res.status is Status.CANCELLED
    assert res.bytes_done == 3 * CHUNK
    assert res.sha256 is None


def test_cancel_during_verify(fds):
    src_fd, dst_fd, _ = fds
    res = write_image(src_fd, dst_fd, SIZE, chunk_size=CHUNK)
    cancel = threading.Event()
    cancel.set()
    v = verify(dst_fd, res, cancel=cancel)
    assert v.status is Status.CANCELLED
    assert v.bytes_done == 0


def test_verify_detects_mismatch(fds):
    src_fd, dst_fd, _ = fds
    res = write_image(src_fd, dst_fd, SIZE, chunk_size=CHUNK)
    assert res.status is Status.SUCCESS

    # Corrupt one byte in the 6th chunk.
    bad = 5 * CHUNK + 123
    (orig,) = os.pread(dst_fd, 1, bad)
    os.pwrite(dst_fd, bytes([orig ^ 0xFF]), bad)

    v = verify(dst_fd, res)
    assert v.status is Status.ERROR
    assert v.error_offset == 5 * CHUNK
    assert "mismatch" in v.message


def test_verify_detects_truncated_target(tmp_path, fds):
    src_fd, dst_fd, dst = fds
    res = write_image(src_fd, dst_fd, SIZE, chunk_size=CHUNK)
    os.ftruncate(dst_fd, SIZE - 50)
    v = verify(dst_fd, res)
    assert v.status is Status.ERROR
    assert v.error_offset == SIZE - 50


def test_image_larger_than_target(fds, data):
    src_fd, dst_fd, dst = fds
    before = dst.read_bytes()
    res = write_image(src_fd, dst_fd, SIZE, target_size=SIZE - 1, chunk_size=CHUNK)
    assert res.status is Status.ERROR
    assert res.bytes_done == 0
    assert "larger than the target" in res.message
    assert dst.read_bytes() == before


def test_image_exactly_target_size(fds):
    src_fd, dst_fd, _ = fds
    res = write_image(src_fd, dst_fd, SIZE, target_size=SIZE, chunk_size=CHUNK)
    assert res.status is Status.SUCCESS


def test_source_shorter_than_declared_size(fds):
    src_fd, dst_fd, _ = fds
    res = write_image(src_fd, dst_fd, SIZE + 100, chunk_size=CHUNK)
    assert res.status is Status.ERROR
    assert res.error_offset == SIZE
    assert "ended unexpectedly" in res.message


def test_write_error_reports_offset_and_errno(fds, monkeypatch):
    src_fd, dst_fd, _ = fds
    real_pwrite = os.pwrite

    def failing_pwrite(fd, buf, offset):
        if offset >= 4 * CHUNK:
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        return real_pwrite(fd, buf, offset)

    monkeypatch.setattr(flasher.os, "pwrite", failing_pwrite)
    res = write_image(src_fd, dst_fd, SIZE, chunk_size=CHUNK)
    assert res.status is Status.ERROR
    assert res.errno == errno.ENOSPC
    assert res.error_offset == 4 * CHUNK
    assert res.bytes_done == 4 * CHUNK


def test_fsync_error_is_reported(fds, monkeypatch):
    src_fd, dst_fd, _ = fds

    def failing_fsync(fd):
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(flasher.os, "fsync", failing_fsync)
    res = write_image(src_fd, dst_fd, SIZE, chunk_size=CHUNK)
    assert res.status is Status.ERROR
    assert res.errno == errno.EIO
    assert res.sha256 is None


def test_verify_requires_successful_write(fds):
    src_fd, dst_fd, _ = fds
    res = write_image(src_fd, dst_fd, SIZE, target_size=1)
    with pytest.raises(ValueError):
        verify(dst_fd, res)


@pytest.mark.parametrize("size", [0, -1])
def test_invalid_size(fds, size):
    src_fd, dst_fd, _ = fds
    with pytest.raises(ValueError):
        write_image(src_fd, dst_fd, size)
