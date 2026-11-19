import threading

import pytest

from usb_flasher.flasher import Status
from usb_flasher.job import Stage
from usb_flasher.restore import (
    Filesystem, RestoreJob, RestoreOptions, check_label, default_filesystem, normalize_label,
)

from fakes import B, AuthDismissed, FakeClient, stick_snapshot

GB = 1000**3


@pytest.fixture
def client():
    return FakeClient("/nonexistent/never-opened", stick_snapshot())


def run(client, fs=Filesystem.FAT32, label="STICK", target=B + "sdb", **kw):
    stages = []
    job = RestoreJob(client, target, RestoreOptions(fs, label), on_stage=stages.append, **kw)
    return job.run(), stages


def test_default_filesystem_by_size():
    assert default_filesystem(16 * GB) is Filesystem.FAT32
    assert default_filesystem(32 * GB) is Filesystem.FAT32
    assert default_filesystem(32 * GB + 1) is Filesystem.EXFAT
    assert default_filesystem(128 * GB) is Filesystem.EXFAT


def test_label_rules():
    assert check_label(Filesystem.FAT32, "BACKUP") is None
    assert check_label(Filesystem.FAT32, "") is None
    assert "11" in check_label(Filesystem.FAT32, "A" * 12)
    assert check_label(Filesystem.EXFAT, "A" * 15) is None
    assert "15" in check_label(Filesystem.EXFAT, "A" * 16)
    assert check_label(Filesystem.FAT32, "a/b") is not None
    assert check_label(Filesystem.EXFAT, "a\nb") is not None
    assert normalize_label(Filesystem.FAT32, " my usb ") == "MY USB"
    assert normalize_label(Filesystem.EXFAT, " My USB ") == "My USB"


def test_success_fat32(client):
    out, stages = run(client)
    assert out.status is Status.SUCCESS, out.message
    assert out.drive_modified
    assert stages == [Stage.PREPARING, Stage.UNMOUNTING, Stage.PARTITIONING,
                      Stage.FORMATTING, Stage.FINISHING]
    assert client.names() == [
        "unmount", "create_table", "format", "rescan",
    ]
    assert client.calls[1] == ("unmount", B + "sdb1")
    assert ("create_table", B + "sdb", "dos") in client.calls
    assert ("format", B + "sdb", "vfat", "STICK", "0x0c", ("-F", "32")) in client.calls


def test_success_exfat_keeps_label_case(client):
    out, _ = run(client, Filesystem.EXFAT, "My Drive")
    assert out.status is Status.SUCCESS
    assert ("format", B + "sdb", "exfat", "My Drive", "0x07", ()) in client.calls


def test_fat32_label_uppercased(client):
    run(client, Filesystem.FAT32, "stick")
    assert any(c[0] == "format" and c[3] == "STICK" for c in client.calls)


def test_no_mounts_skips_unmount():
    client = FakeClient("x", stick_snapshot(mounts=None))
    out, stages = run(client)
    assert out.status is Status.SUCCESS
    assert Stage.UNMOUNTING not in stages
    assert client.names() == ["create_table", "format", "rescan"]


def test_huge_drive_uses_gpt():
    client = FakeClient("x", stick_snapshot(size=3 * 1024**4, mounts=None))
    out, _ = run(client, Filesystem.EXFAT)
    assert out.status is Status.SUCCESS
    assert ("create_table", B + "sdb", "gpt") in client.calls
    assert any(c[0] == "format" and c[4] == "" for c in client.calls)


def test_invalid_label_touches_nothing(client):
    out, _ = run(client, Filesystem.FAT32, "A" * 12)
    assert out.status is Status.ERROR
    assert client.calls == []


@pytest.mark.parametrize("change", [
    lambda s: s[B + "sdb"]["Block"].update(HintSystem=True),
    lambda s: s[B + "sdb"]["Block"].update(ReadOnly=True),
    lambda s: s[B + "sdb1"]["Filesystem"].update(MountPoints=["/home"]),
    lambda s: s["/org/freedesktop/UDisks2/drives/stick"]["Drive"].update(
        Removable=False, MediaRemovable=False, ConnectionBus="sata"),
])
def test_off_limits_drives_are_refused(change):
    snap = stick_snapshot()
    change(snap)
    client = FakeClient("x", snap)
    out, stages = run(client)
    assert out.status is Status.ERROR
    assert client.names() == []
    assert stages == [Stage.PREPARING]


def test_drive_in_use_is_refused():
    snap = stick_snapshot()
    snap[B + "sdb1"]["Block"]["Holders"] = []
    snap[B + "dm-3"] = {
        "Block": {"Device": "/dev/dm-3", "PreferredDevice": "/dev/mapper/x", "Size": 1,
                  "Drive": "/", "CryptoBackingDevice": B + "sdb1", "Holders": []},
    }
    client = FakeClient("x", snap)
    out, _ = run(client)
    assert out.status is Status.ERROR and "In use" in out.message
    assert client.names() == []


def test_drive_disappeared():
    client = FakeClient("x", {})
    out, _ = run(client)
    assert out.status is Status.ERROR and "no longer connected" in out.message
    assert client.names() == []


def test_unmount_failure_stops(client):
    client.unmount_error = RuntimeError("busy")
    out, _ = run(client)
    assert out.status is Status.ERROR and "busy" in out.message
    assert client.names() == ["unmount"]
    assert not out.drive_modified


def test_still_mounted_after_unmount(client, monkeypatch):
    from usb_flasher import job
    monkeypatch.setattr(job, "UNMOUNT_SETTLE_DELAY", 0)
    client.keep_mounted = True
    out, _ = run(client)
    assert out.status is Status.ERROR and "still mounted" in out.message
    assert "create_table" not in client.names()


def test_auth_dismissed_on_table_is_cancel(client):
    client.table_error = AuthDismissed("dismissed")
    out, _ = run(client)
    assert out.status is Status.CANCELLED
    assert "format" not in client.names()


def test_auth_dismissed_on_format_reports_modified(client):
    client.format_error = AuthDismissed("dismissed")
    out, _ = run(client)
    assert out.status is Status.CANCELLED
    assert out.drive_modified
    assert "rescan" not in client.names()


def test_format_error(client):
    client.format_error = RuntimeError("mkfs.exfat not found")
    out, stages = run(client, Filesystem.EXFAT)
    assert out.status is Status.ERROR
    assert "mkfs.exfat not found" in out.message
    assert out.drive_modified and out.stage is Stage.FORMATTING


def test_rescan_failure_is_a_warning(client):
    def boom(_p):
        raise RuntimeError("nope")
    client.rescan = boom
    out, _ = run(client)
    assert out.status is Status.SUCCESS
    assert "Could not rescan" in out.warnings[0]


def test_cancel_before_start(client):
    cancel = threading.Event()
    cancel.set()
    out, _ = run(client, cancel=cancel)
    assert out.status is Status.CANCELLED and not out.drive_modified
    assert client.names() == []


def test_cancel_after_table_created(client):
    cancel = threading.Event()
    orig = client.create_partition_table

    def table(path, kind):
        orig(path, kind)
        cancel.set()

    client.create_partition_table = table
    out, _ = run(client, cancel=cancel)
    assert out.status is Status.CANCELLED and out.drive_modified
    assert "format" not in client.names()
