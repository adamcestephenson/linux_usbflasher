import bz2
import gzip
import hashlib
import lzma
import os
import zipfile

import pytest

from usb_flasher import archives
from usb_flasher.flasher import Status
from usb_flasher.image import ImageError, inspect_image
from usb_flasher.job import FlashJob

from fakes import B, MB, TARGET_SIZE, FakeClient, stick_snapshot

zstd = archives.zstd

DATA = os.urandom(300_000) + bytes(200_000)


def _write_zip(path, data, name="disk.img"):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, data)


def _write(path, data):
    compress = {
        ".gz": gzip.compress, ".bz2": bz2.compress, ".xz": lzma.compress,
        ".zst": zstd.compress if zstd else None,
    }[path.suffix]
    path.write_bytes(compress(data))


FORMATS = [
    pytest.param(".xz", 500_000, id="xz"),
    pytest.param(".gz", None, id="gz"),
    pytest.param(".bz2", None, id="bz2"),
    pytest.param(".zst", 500_000, id="zst",
                 marks=pytest.mark.skipif(zstd is None, reason="needs Python 3.14")),
    pytest.param(".zip", 500_000, id="zip"),
]


def make(tmp_path, ext, data=DATA, name="disk.img"):
    p = tmp_path / (name + ext)
    if ext == ".zip":
        _write_zip(p, data)
    else:
        _write(p, data)
    return p


def flash(tmp_path, image_path, drive_size=TARGET_SIZE):
    drive = tmp_path / "drive.bin"
    drive.write_bytes(b"\xaa" * drive_size)
    client = FakeClient(drive, stick_snapshot(size=drive_size))
    out = FlashJob(client, image_path, B + "sdb", chunk_size=65536).run()
    return out, drive.read_bytes()


@pytest.mark.parametrize("ext,size", FORMATS)
def test_inspect_size(tmp_path, ext, size):
    info = inspect_image(make(tmp_path, ext))
    assert info.compression == ext
    assert info.size == size
    assert info.file_size > 0


@pytest.mark.parametrize("ext,size", FORMATS)
def test_round_trip(tmp_path, ext, size):
    out, drive = flash(tmp_path, make(tmp_path, ext))
    assert out.status is Status.SUCCESS, out.message
    assert out.bytes_done == out.image_size == len(DATA)
    assert out.sha256 == hashlib.sha256(DATA).hexdigest()
    assert drive[:len(DATA)] == DATA
    assert drive[len(DATA):] == b"\xaa" * (TARGET_SIZE - len(DATA))


@pytest.mark.parametrize("ext,size", FORMATS)
def test_truncated(tmp_path, ext, size):
    p = make(tmp_path, ext)
    p.write_bytes(p.read_bytes()[: p.stat().st_size * 2 // 3])
    try:
        info = inspect_image(p)
    except ImageError:
        return  # detected up front (zip, xz index)
    out, _ = flash(tmp_path, p)
    assert out.status is Status.ERROR
    assert out.drive_modified or "corrupt" in out.message or "unexpectedly" in out.message


@pytest.mark.parametrize("ext", [".gz", ".bz2", ".xz"])
def test_truncated_midstream_fails_cleanly(tmp_path, ext):
    p = make(tmp_path, ext, os.urandom(400_000))
    p.write_bytes(p.read_bytes()[:200_000])
    out, _ = flash(tmp_path, p)
    assert out.status is Status.ERROR
    assert "Error reading image" in out.message or "Cannot read" in out.message


@pytest.mark.parametrize("ext", [".gz", ".bz2", ".xz", ".zip"])
def test_corrupt(tmp_path, ext):
    p = make(tmp_path, ext, os.urandom(400_000))
    raw = bytearray(p.read_bytes())
    for i in range(len(raw) // 2, len(raw) // 2 + 64):
        raw[i] ^= 0xFF
    p.write_bytes(bytes(raw))
    try:
        inspect_image(p)
    except ImageError:
        return
    out, _ = flash(tmp_path, p)
    assert out.status is Status.ERROR
    assert "Error reading image" in out.message


def test_garbage_with_compressed_extension(tmp_path):
    p = tmp_path / "x.img.xz"
    p.write_bytes(b"not xz data at all" * 10)
    with pytest.raises(ImageError):
        inspect_image(p)


def test_bad_zip(tmp_path):
    p = tmp_path / "x.zip"
    p.write_bytes(b"nope")
    with pytest.raises(ImageError, match="zip"):
        inspect_image(p)


@pytest.mark.parametrize("ext", [".gz", ".bz2", ".xz", ".zip"])
def test_image_larger_than_drive(tmp_path, ext):
    big = bytes(2 * MB)
    out, drive = flash(tmp_path, make(tmp_path, ext, big), drive_size=MB)
    assert out.status is Status.ERROR
    assert "larger" in out.message
    assert len(drive) == MB


def test_unknown_size_larger_than_drive_is_capped(tmp_path):
    p = make(tmp_path, ".gz", os.urandom(3 * MB))
    assert inspect_image(p).size is None
    out, drive = flash(tmp_path, p, drive_size=MB)
    assert out.status is Status.ERROR
    assert "larger" in out.message
    assert len(drive) == MB


def test_iso_hybrid_check_through_compression(tmp_path):
    buf = bytearray(64 * 1024)
    buf[32769:32774] = b"CD001"
    p = make(tmp_path, ".xz", bytes(buf), name="d.iso")
    info = inspect_image(p)
    assert info.is_iso9660 and info.is_hybrid is False and info.warnings


def test_compressed_name_must_be_image(tmp_path):
    p = tmp_path / "notes.txt.gz"
    p.write_bytes(gzip.compress(b"hi"))
    with pytest.raises(ImageError, match="Unsupported"):
        inspect_image(p)


def test_7z_rejected(tmp_path):
    p = tmp_path / "a.img.7z"
    p.write_bytes(b"x")
    with pytest.raises(ImageError, match="Unsupported"):
        inspect_image(p)


def test_zip_without_image(tmp_path):
    p = tmp_path / "a.zip"
    _write_zip(p, b"hello", name="readme.txt")
    with pytest.raises(ImageError, match="no disk image"):
        inspect_image(p)


def test_zip_with_several_images(tmp_path):
    p = tmp_path / "a.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("a.img", b"1")
        zf.writestr("b.iso", b"2")
    with pytest.raises(ImageError, match="several"):
        inspect_image(p)


def test_zip_picks_the_image_among_other_files(tmp_path):
    p = tmp_path / "a.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("README.txt", b"hi")
        zf.writestr("disk.img", DATA)
    assert inspect_image(p).size == len(DATA)


def test_xz_multi_stream_and_block_sizes(tmp_path):
    p = tmp_path / "a.img.xz"
    a, b = os.urandom(100_000), os.urandom(50_000)
    p.write_bytes(lzma.compress(a) + lzma.compress(b))
    assert inspect_image(p).size == 150_000
    out, drive = flash(tmp_path, p)
    assert out.status is Status.SUCCESS, out.message
    assert drive[:150_000] == a + b


def test_zstd_unknown_size_streaming(tmp_path):
    if zstd is None:
        pytest.skip("needs Python 3.14")
    c = zstd.ZstdCompressor()
    p = tmp_path / "a.img.zst"
    p.write_bytes(c.compress(DATA) + c.flush())
    assert inspect_image(p).size is None
    out, drive = flash(tmp_path, p)
    assert out.status is Status.SUCCESS, out.message
    assert drive[:len(DATA)] == DATA
