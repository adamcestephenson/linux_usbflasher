import hashlib
import os
import threading

import pytest

from usb_flasher import job as job_mod
from usb_flasher.drives import snapshot_from_managed_objects, mounted_filesystems
from usb_flasher.flasher import Phase, Status
from usb_flasher.job import FlashJob, Stage

from fakes import B, D, MB, TARGET_SIZE, AuthDismissed, FakeClient, stick_snapshot


def fd_is_open(fd):
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


@pytest.fixture
def image(tmp_path):
    data = os.urandom(MB + 12345)
    p = tmp_path / "image.img"
    p.write_bytes(data)
    return p, data


@pytest.fixture
def drive_file(tmp_path):
    p = tmp_path / "drive.bin"
    p.write_bytes(b"\xaa" * TARGET_SIZE)
    return p


@pytest.fixture
def client(drive_file):
    return FakeClient(drive_file, stick_snapshot())


def run(client, image_path, **kw):
    stages = []
    progress = []
    j = FlashJob(client, image_path, B + "sdb", on_stage=stages.append,
                 on_progress=progress.append, **kw)
    return j.run(), stages, progress


def test_full_success(client, image, drive_file):
    path, data = image
    out, stages, progress = run(client, path)

    assert out.status is Status.SUCCESS, out.message
    assert out.sha256 == hashlib.sha256(data).hexdigest()
    assert out.bytes_done == out.image_size == len(data)
    assert out.drive_modified and out.warnings == []
    assert not out.ejected
    assert stages == [Stage.PREPARING, Stage.UNMOUNTING, Stage.OPENING,
                      Stage.WRITING, Stage.VERIFYING, Stage.FINISHING]
    assert client.names() == ["unmount", "open", "rescan"]
    assert client.calls[1] == ("unmount", B + "sdb1")
    phases = {p.phase for p in progress}
    assert phases == {Phase.WRITING, Phase.VERIFYING}

    content = drive_file.read_bytes()
    assert content[:len(data)] == data
    assert content[len(data):] == b"\xaa" * (TARGET_SIZE - len(data))
    assert not any(fd_is_open(fd) for fd in client.opened_fds)


def test_no_mounts_skips_unmount_stage(drive_file, image):
    client = FakeClient(drive_file, stick_snapshot(mounts=None))
    out, stages, _ = run(client, image[0])
    assert out.status is Status.SUCCESS
    assert Stage.UNMOUNTING not in stages
    assert client.names() == ["open", "rescan"]


def test_without_verify(client, image):
    out, stages, progress = run(client, image[0], verify=False)
    assert out.status is Status.SUCCESS
    assert Stage.VERIFYING not in stages
    assert {p.phase for p in progress} == {Phase.WRITING}


def test_unmounts_deepest_first(drive_file, image):
    snap = stick_snapshot()
    snap[B + "sdb1"]["Block"]["Holders"] = []
    snap[B + "dm-3"] = {
        "Block": {"Device": "/dev/dm-3", "PreferredDevice": "/dev/mapper/x", "Size": 1,
                  "Drive": "/", "CryptoBackingDevice": B + "sdb1", "Holders": []},
        "Filesystem": {"MountPoints": ["/run/media/u/inner"]},
    }
    assert mounted_filesystems(snap, B + "sdb") == [B + "dm-3", B + "sdb1"]


def test_bad_image(client, tmp_path):
    out, stages, _ = run(client, tmp_path / "missing.iso")
    assert out.status is Status.ERROR
    assert "not found" in out.message
    assert stages == [Stage.PREPARING]
    assert client.names() == []


def test_drive_disappeared(drive_file, image):
    client = FakeClient(drive_file, {})
    out, _, _ = run(client, image[0])
    assert out.status is Status.ERROR
    assert "no longer connected" in out.message
    assert client.names() == []


def test_drive_became_unsafe(drive_file, image):
    snap = stick_snapshot()
    snap[B + "sdb"]["Block"]["HintSystem"] = True
    client = FakeClient(drive_file, snap)
    out, _, _ = run(client, image[0])
    assert out.status is Status.ERROR
    assert "System drive" in out.message
    assert client.names() == []


def test_image_too_large_for_drive(drive_file, image):
    client = FakeClient(drive_file, stick_snapshot(size=MB))
    out, _, _ = run(client, image[0])
    assert out.status is Status.ERROR
    assert "larger than the target" in out.message
    assert client.names() == []
    assert not out.drive_modified


def test_real_device_smaller_than_reported(tmp_path, image):
    small = tmp_path / "small.bin"
    small.write_bytes(b"\0" * 1000)
    client = FakeClient(small, stick_snapshot())
    out, _, _ = run(client, image[0])
    assert out.status is Status.ERROR
    assert "larger than the target" in out.message
    assert not out.drive_modified
    assert small.read_bytes() == b"\0" * 1000


def test_unmount_failure(client, image):
    client.unmount_error = RuntimeError("Failed to unmount the drive: target is busy")
    out, _, _ = run(client, image[0])
    assert out.status is Status.ERROR
    assert out.stage is Stage.UNMOUNTING
    assert "busy" in out.message
    assert "open" not in client.names()


def test_still_mounted_after_unmount(client, image, monkeypatch):
    monkeypatch.setattr(job_mod, "UNMOUNT_SETTLE_DELAY", 0)
    client.keep_mounted = True
    out, _, _ = run(client, image[0])
    assert out.status is Status.ERROR
    assert "still mounted" in out.message
    assert "open" not in client.names()


def test_auth_dismissed_is_cancel(client, image, drive_file):
    client.open_error = AuthDismissed("Authentication was cancelled")
    out, _, _ = run(client, image[0])
    assert out.status is Status.CANCELLED
    assert out.stage is Stage.OPENING
    assert not out.drive_modified
    assert drive_file.read_bytes() == b"\xaa" * TARGET_SIZE


def test_auth_dismissed_during_unmount_is_cancel(client, image):
    client.unmount_error = AuthDismissed("Authentication was cancelled")
    out, _, _ = run(client, image[0])
    assert out.status is Status.CANCELLED


def test_open_not_authorized(client, image):
    client.open_error = RuntimeError("Not authorized to open the drive for writing")
    out, _, _ = run(client, image[0])
    assert out.status is Status.ERROR
    assert "Not authorized" in out.message


def test_cancel_before_start(client, image):
    cancel = threading.Event()
    cancel.set()
    out, _, _ = run(client, image[0], cancel=cancel)
    assert out.status is Status.CANCELLED
    assert client.names() == []


def test_cancel_during_write(client, image):
    cancel = threading.Event()

    def on_progress(p):
        if p.phase is Phase.WRITING and p.bytes_done >= 128 * 1024:
            cancel.set()

    j = FlashJob(client, image[0], B + "sdb", on_progress=on_progress, cancel=cancel,
                 chunk_size=64 * 1024)
    out = j.run()
    assert out.status is Status.CANCELLED
    assert out.stage is Stage.WRITING
    assert out.drive_modified
    assert "incomplete" in out.message
    assert "rescan" not in client.names()
    assert not any(fd_is_open(fd) for fd in client.opened_fds)


def test_verify_mismatch(client, image, drive_file):
    corrupted = []

    def on_progress(p):
        if p.phase is Phase.VERIFYING and not corrupted:
            with open(drive_file, "r+b") as f:
                f.seek(500_000)
                b = f.read(1)
                f.seek(500_000)
                f.write(bytes([b[0] ^ 1]))
            corrupted.append(True)

    j = FlashJob(client, image[0], B + "sdb", on_progress=on_progress)
    out = j.run()
    assert out.status is Status.ERROR
    assert out.stage is Stage.VERIFYING
    assert "mismatch" in out.message
    assert "incomplete" not in out.message
    assert out.error_offset == 0  # first 4 MiB chunk
    assert out.drive_modified
    assert not any(fd_is_open(fd) for fd in client.opened_fds)


def test_eject_unmounts_automounted_then_powers_off(client, image):
    client.remount_after_rescan = True
    out, stages, _ = run(client, image[0], eject=True)
    assert out.status is Status.SUCCESS
    assert stages[-1] is Stage.EJECTING
    assert client.names() == ["unmount", "open", "rescan", "unmount", "power_off"]
    assert client.calls[-1] == ("power_off", D + "stick")
    assert out.ejected


def test_eject_failure_is_a_warning(client, image):
    def boom(_):
        raise RuntimeError("Failed to eject the drive: busy")
    client.power_off = boom
    out, _, _ = run(client, image[0], eject=True)
    assert out.status is Status.SUCCESS
    assert out.warnings == ["Flashed successfully, but ejecting failed: Failed to eject the drive: busy"]
    assert not out.ejected


def test_rescan_failure_is_a_warning(client, image):
    def boom(_):
        raise RuntimeError("nope")
    client.rescan = boom
    out, _, _ = run(client, image[0])
    assert out.status is Status.SUCCESS
    assert out.warnings == ["Could not rescan partitions: nope"]


def test_loop_eject_is_noop(drive_file, image):
    snap = {B + "loop0": {
        "Block": {"Device": "/dev/loop0", "PreferredDevice": "/dev/loop0", "Size": TARGET_SIZE,
                  "Drive": "/", "HintSystem": True, "CryptoBackingDevice": "/", "Holders": []},
        "Loop": {"BackingFile": "/tmp/x.img"},
    }}
    client = FakeClient(drive_file, snap)
    out = FlashJob(client, image[0], B + "loop0", allow_loop=True, eject=True).run()
    assert out.status is Status.SUCCESS
    assert "power_off" not in client.names()
    assert not out.ejected

    out = FlashJob(client, image[0], B + "loop0").run()  # loops refused by default
    assert out.status is Status.ERROR


def test_snapshot_from_managed_objects():
    def bs(s):
        return list(s.encode()) + [0]

    objects = {
        B + "nonexistent9": {
            "org.freedesktop.UDisks2.Block": {"Device": bs("/dev/nonexistent9"), "Size": 7},
            "org.freedesktop.UDisks2.Filesystem": {"MountPoints": [bs("/run/media/a")]},
            "org.freedesktop.DBus.Properties": {},
        },
    }
    snap = snapshot_from_managed_objects(objects)
    assert snap == {B + "nonexistent9": {
        "Block": {"Device": "/dev/nonexistent9", "Size": 7, "Holders": []},
        "Filesystem": {"MountPoints": ["/run/media/a"]},
    }}
