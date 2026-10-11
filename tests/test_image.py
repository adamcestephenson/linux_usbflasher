import os

import pytest

from usb_flasher.image import ImageError, check_fits, inspect_image


def make_image(path, *, iso=False, mbr=False, size=64 * 1024):
    buf = bytearray(size)
    if mbr:
        buf[510:512] = b"\x55\xaa"
    if iso:
        buf[32769:32774] = b"CD001"
    path.write_bytes(bytes(buf))
    return path


def test_hybrid_iso(tmp_path):
    info = inspect_image(make_image(tmp_path / "distro.iso", iso=True, mbr=True))
    assert info.is_iso9660
    assert info.is_hybrid is True
    assert info.warnings == []
    assert info.size == 64 * 1024


def test_non_hybrid_iso_warns(tmp_path):
    info = inspect_image(make_image(tmp_path / "windows.iso", iso=True))
    assert info.is_iso9660
    assert info.is_hybrid is False
    assert len(info.warnings) == 1
    assert "hybrid" in info.warnings[0]


def test_raw_img_has_no_iso_checks(tmp_path):
    info = inspect_image(make_image(tmp_path / "rpi.img", mbr=True))
    assert not info.is_iso9660
    assert info.is_hybrid is None
    assert info.warnings == []


def test_small_file_shorter_than_iso_header(tmp_path):
    p = tmp_path / "tiny.img"
    p.write_bytes(b"x" * 100)
    info = inspect_image(p)
    assert info.size == 100
    assert not info.is_iso9660


def test_extension_case_insensitive(tmp_path):
    info = inspect_image(make_image(tmp_path / "DISK.IMG"))
    assert info.size > 0


def test_missing_file(tmp_path):
    with pytest.raises(ImageError, match="not found"):
        inspect_image(tmp_path / "nope.iso")


def test_empty_file(tmp_path):
    p = tmp_path / "empty.img"
    p.touch()
    with pytest.raises(ImageError, match="empty"):
        inspect_image(p)


def test_directory(tmp_path):
    d = tmp_path / "dir.iso"
    d.mkdir()
    with pytest.raises(ImageError, match="regular file"):
        inspect_image(d)


def test_unsupported_extension(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_bytes(b"hello")
    with pytest.raises(ImageError, match="Unsupported"):
        inspect_image(p)


@pytest.mark.parametrize("name", ["a.img.xz", "a.iso.gz", "a.zip", "a.img.zst"])
def test_compressed_rejected(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"data")
    with pytest.raises(ImageError, match="Compressed"):
        inspect_image(p)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_unreadable_file(tmp_path):
    p = make_image(tmp_path / "secret.img")
    p.chmod(0)
    try:
        with pytest.raises(ImageError, match="Cannot read"):
            inspect_image(p)
    finally:
        p.chmod(0o644)


def test_check_fits():
    check_fits(100, 100)
    check_fits(99, 100)
    with pytest.raises(ImageError, match="larger"):
        check_fits(101, 100)
