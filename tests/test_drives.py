import os

import pytest

from usb_flasher.drives import (
    LARGE_DRIVE_THRESHOLD,
    classify_drives,
    format_size,
    is_system_mount,
    read_holders,
)

B = "/org/freedesktop/UDisks2/block_devices/"
D = "/org/freedesktop/UDisks2/drives/"
GB = 1000**3


def disk(name, drive, size=16 * GB, **block):
    return {B + name: {"Block": {
        "Device": f"/dev/{name}", "PreferredDevice": f"/dev/{name}", "Size": size,
        "Drive": D + drive if drive else "/", "HintSystem": False, "ReadOnly": False,
        "CryptoBackingDevice": "/", "Holders": [], **block,
    }}}


def drive(name, *, bus="usb", removable=True, media=True, vendor="SanDisk", model="Ultra", size=16 * GB):
    return {D + name: {"Drive": {
        "Vendor": vendor, "Model": model, "ConnectionBus": bus, "Removable": removable,
        "MediaRemovable": removable, "MediaAvailable": media, "Size": size,
    }}}


def part(name, table, drive_name, mounts=None, size=8 * GB, **block):
    obj = disk(name, drive_name, size, **block)
    obj[B + name]["Partition"] = {"Table": B + table, "Number": 1}
    if mounts is not None:
        obj[B + name]["Filesystem"] = {"MountPoints": mounts}
    return obj


def usb_stick(name="sdb", drv="stick", **kw):
    return {**drive(drv, **kw), **disk(name, drv, kw.get("size", 16 * GB))}


def system_disk():
    """Mirrors a real laptop: sda1 /boot, sda2 LUKS -> dm-0 btrfs at /."""
    snap = {
        **drive("ssd", bus="", removable=False, vendor="", model="KingFast", size=512 * GB),
        **disk("sda", "ssd", 512 * GB, HintSystem=True),
        **part("sda1", "sda", "ssd", ["/boot"], HintSystem=True),
        **part("sda2", "sda", "ssd", HintSystem=True, Holders=["/dev/dm-0"]),
        **disk("dm-0", None, 500 * GB, PreferredDevice="/dev/mapper/root",
               CryptoBackingDevice=B + "sda2", HintSystem=True),
    }
    snap[B + "dm-0"]["Filesystem"] = {"MountPoints": ["/", "/home", "/var/log"]}
    return snap


def by_dev(drives):
    return {d.device: d for d in drives}


def test_usb_stick_is_selectable():
    (d,) = classify_drives(usb_stick())
    assert d.device == "/dev/sdb"
    assert d.selectable
    assert d.label == "SanDisk Ultra"
    assert d.bus == "usb"
    assert d.size == 16 * GB
    assert d.drive_path == D + "stick"
    assert d.reasons == d.warnings == d.mount_points == []


def test_internal_system_disk_hidden_and_blocked():
    snap = {**system_disk(), **usb_stick()}
    assert [d.device for d in classify_drives(snap)] == ["/dev/sdb"]

    sda = by_dev(classify_drives(snap, include_hidden=True))["/dev/sda"]
    assert sda.hidden and not sda.selectable
    assert "Not a removable drive" in sda.reasons
    assert "System drive" in sda.reasons
    # / is found through sda2 -> LUKS -> dm-0
    system = [r for r in sda.reasons if r.startswith("Contains system mounts")]
    assert system == ["Contains system mounts: /, /boot, /home, /var/log"]


def test_luks_found_via_crypto_backing_device_without_holders():
    snap = system_disk()
    snap[B + "sda2"]["Block"]["Holders"] = []
    sda = by_dev(classify_drives(snap, include_hidden=True))["/dev/sda"]
    assert "Contains system mounts: /, /boot, /home, /var/log" in sda.reasons


def test_dm_and_zram_devices_are_never_candidates():
    snap = {**system_disk(), **disk("zram0", None, 16 * GB, HintSystem=True)}
    snap[B + "zram0"]["Swapspace"] = {"Active": True}
    devices = [d.device for d in classify_drives(snap, include_hidden=True)]
    assert devices == ["/dev/sda"]


def test_empty_card_reader_hidden():
    snap = {**drive("reader", media=False, size=0), **disk("sdc", "reader", 0)}
    assert classify_drives(snap) == []
    (d,) = classify_drives(snap, include_hidden=True)
    assert d.hidden and "No media" in d.reasons


def test_zero_size_hidden_even_if_media_flag_missing():
    snap = usb_stick(size=0)
    assert classify_drives(snap) == []


@pytest.mark.parametrize("bus", ["usb", "sdio"])
def test_non_removable_flag_but_usb_bus_is_shown(bus):
    # Many USB SSDs/enclosures report Removable=False.
    (d,) = classify_drives(usb_stick(bus=bus, removable=False))
    assert d.selectable


def test_non_removable_sata_hidden():
    assert classify_drives(usb_stick(bus="ata", removable=False)) == []


def test_removable_on_other_bus_shown():
    (d,) = classify_drives(usb_stick(bus="", removable=True))
    assert d.selectable


def test_system_hint_blocks():
    snap = usb_stick()
    snap[B + "sdb"]["Block"]["HintSystem"] = True
    (d,) = classify_drives(snap)
    assert not d.hidden and not d.selectable
    assert d.reasons == ["System drive"]


def test_read_only_blocks():
    snap = usb_stick()
    snap[B + "sdb"]["Block"]["ReadOnly"] = True
    (d,) = classify_drives(snap)
    assert d.reasons == ["Read-only"]


def test_user_mounts_are_listed_not_blocking():
    snap = {**usb_stick(), **part("sdb1", "sdb", "stick", ["/run/media/aces/STICK"])}
    (d,) = classify_drives(snap)
    assert d.selectable
    assert d.mount_points == ["/run/media/aces/STICK"]


@pytest.mark.parametrize("mp", ["/", "/boot", "/boot/efi", "/efi", "/home", "/var", "/usr"])
def test_system_mount_on_usb_blocks(mp):
    snap = {**usb_stick(), **part("sdb1", "sdb", "stick", [mp])}
    (d,) = classify_drives(snap)
    assert not d.selectable
    assert d.reasons == [f"Contains system mounts: {mp}"]


def test_mount_directly_on_whole_disk():
    snap = usb_stick()
    snap[B + "sdb"]["Filesystem"] = {"MountPoints": ["/run/media/aces/X"]}
    (d,) = classify_drives(snap)
    assert d.selectable and d.mount_points == ["/run/media/aces/X"]


def test_active_swap_partition_blocks():
    snap = {**usb_stick(), **part("sdb1", "sdb", "stick")}
    snap[B + "sdb1"]["Swapspace"] = {"Active": True}
    (d,) = classify_drives(snap)
    assert d.reasons == ["In use as swap"]


def test_inactive_swap_partition_ok():
    snap = {**usb_stick(), **part("sdb1", "sdb", "stick")}
    snap[B + "sdb1"]["Swapspace"] = {"Active": False}
    (d,) = classify_drives(snap)
    assert d.selectable


def test_unlocked_luks_on_usb_blocks():
    snap = {
        **usb_stick(),
        **part("sdb1", "sdb", "stick", Holders=["/dev/dm-3"]),
        **disk("dm-3", None, PreferredDevice="/dev/mapper/luks-abc",
               CryptoBackingDevice=B + "sdb1"),
    }
    snap[B + "dm-3"]["Filesystem"] = {"MountPoints": ["/run/media/aces/secret"]}
    (d,) = classify_drives(snap)
    assert not d.selectable
    assert d.reasons == ["In use by /dev/mapper/luks-abc (lock or deactivate first)"]
    assert d.mount_points == ["/run/media/aces/secret"]


def test_lvm_on_usb_with_system_mount_via_holders():
    # PV on sdb1 -> LV dm-5 mounted at /var: only discoverable via holders.
    snap = {
        **usb_stick(),
        **part("sdb1", "sdb", "stick", Holders=["/dev/dm-5"]),
        **disk("dm-5", None, PreferredDevice="/dev/mapper/vg-var"),
    }
    snap[B + "dm-5"]["Filesystem"] = {"MountPoints": ["/var"]}
    (d,) = classify_drives(snap)
    assert d.reasons == ["Contains system mounts: /var"]


def test_holder_chain_is_followed_and_cycles_are_safe():
    snap = {
        **usb_stick(),
        **disk("md0", None, Holders=["/dev/dm-1"]),
        **disk("dm-1", None, Holders=["/dev/md0"]),  # pathological cycle
    }
    snap[B + "sdb"]["Block"]["Holders"] = ["/dev/md0"]
    snap[B + "dm-1"]["Filesystem"] = {"MountPoints": ["/home"]}
    (d,) = classify_drives(snap)
    assert d.reasons == ["Contains system mounts: /home"]


def test_large_drive_warns_but_selectable():
    (d,) = classify_drives(usb_stick(size=LARGE_DRIVE_THRESHOLD + 1))
    assert d.selectable
    assert len(d.warnings) == 1 and "Large drive" in d.warnings[0]
    (d,) = classify_drives(usb_stick(size=LARGE_DRIVE_THRESHOLD))
    assert d.warnings == []


def test_source_image_on_drive_blocks(tmp_path):
    snap = {**system_disk(), **usb_stick(),
            **part("sdb1", "sdb", "stick", ["/run/media/aces/STICK"])}
    (d,) = classify_drives(snap, source_path="/run/media/aces/STICK/isos/arch.iso")
    assert d.reasons == ["Contains the source image"]

    # Prefix match must respect path boundaries.
    (d,) = classify_drives(snap, source_path="/run/media/aces/STICK2/arch.iso")
    assert d.selectable
    # Image on the system disk doesn't affect the stick.
    (d,) = classify_drives(snap, source_path="/home/aces/arch.iso")
    assert d.selectable


def test_loop_devices_only_with_allow_loop():
    snap = disk("loop0", None, 64 * 1000**2, HintSystem=True)
    snap[B + "loop0"]["Loop"] = {"BackingFile": "/tmp/test.img"}
    assert classify_drives(snap) == []

    (d,) = classify_drives(snap, allow_loop=True)
    assert d.selectable  # HintSystem ignored for loops
    assert d.bus == "loop"
    assert d.drive_path is None
    assert d.label == "Loop device (/tmp/test.img)"


def test_loop_device_still_respects_mounts():
    snap = disk("loop0", None, 64 * 1000**2)
    snap[B + "loop0"]["Loop"] = {"BackingFile": "/tmp/test.img"}
    snap.update(part("loop0p1", "loop0", None, ["/"]))
    (d,) = classify_drives(snap, allow_loop=True)
    assert d.reasons == ["Contains system mounts: /"]


def test_label_falls_back_to_device():
    (d,) = classify_drives(usb_stick(vendor="", model=" "))
    assert d.label == "/dev/sdb"


def test_sorted_by_device():
    snap = {**usb_stick("sdc", "b"), **usb_stick("sdb", "a")}
    assert [d.device for d in classify_drives(snap)] == ["/dev/sdb", "/dev/sdc"]


def test_is_system_mount():
    assert is_system_mount("/")
    assert is_system_mount("/boot/efi")
    assert not is_system_mount("/run/media/aces/X")
    assert not is_system_mount("/homework")
    assert not is_system_mount("/mnt/usb")


def test_format_size():
    assert format_size(0) == "0 B"
    assert format_size(999) == "999 B"
    assert format_size(16 * GB) == "16.0 GB"
    assert format_size(2 * 1000**4) == "2.0 TB"
    assert format_size(5000 * 1000**4) == "5000.0 TB"


def test_read_holders(tmp_path):
    (tmp_path / "sdb1" / "holders").mkdir(parents=True)
    (tmp_path / "sdb1" / "holders" / "dm-3").touch()
    (tmp_path / "sdb1" / "holders" / "dm-1").touch()
    assert read_holders("/dev/sdb1", sysfs=str(tmp_path)) == ["/dev/dm-1", "/dev/dm-3"]
    assert read_holders("/dev/nope", sysfs=str(tmp_path)) == []


def test_snapshot_from_manager_decodes_bytestrings():
    from usb_flasher.drives import snapshot_from_manager

    class V:
        def __init__(self, v):
            self.v = v

        def unpack(self):
            return self.v

    class Proxy:
        def __init__(self, name, props):
            self.name, self.props = name, props

        def get_interface_name(self):
            return self.name

        def get_cached_property_names(self):
            return list(self.props)

        def get_cached_property(self, k):
            return V(self.props[k])

    class Obj:
        def __init__(self, path, proxies):
            self.path, self.proxies = path, proxies

        def get_object_path(self):
            return self.path

        def get_interfaces(self):
            return self.proxies

    def bs(s):
        return list(s.encode()) + [0]

    class Manager:
        def get_objects(self):
            return [Obj(B + "nonexistent9", [
                Proxy("org.freedesktop.UDisks2.Block", {
                    "Device": bs("/dev/nonexistent9"), "Size": 5}),
                Proxy("org.freedesktop.UDisks2.Filesystem", {
                    "MountPoints": [bs("/run/media/x")]}),
                Proxy("org.freedesktop.DBus.Properties", {}),
            ])]

    snap = snapshot_from_manager(Manager())
    obj = snap[B + "nonexistent9"]
    assert obj["Block"] == {"Device": "/dev/nonexistent9", "Size": 5, "Holders": []}
    assert obj["Filesystem"] == {"MountPoints": ["/run/media/x"]}
    assert set(obj) == {"Block", "Filesystem"}


@pytest.mark.skipif(not os.path.exists("/run/dbus/system_bus_socket"), reason="no system bus")
def test_drive_monitor_smoke():
    """Talks to the real udisks2 (read-only): must not raise."""
    from usb_flasher.drives import DriveMonitor, DrivesError

    try:
        monitor = DriveMonitor()
    except DrivesError as e:
        pytest.skip(str(e))
    drives = monitor.drives(include_hidden=True)
    assert all(d.device.startswith("/dev/") for d in drives)
