"""Fakes shared by the tests: a udisks client backed by a temp file."""

import copy
import os

B = "/org/freedesktop/UDisks2/block_devices/"
D = "/org/freedesktop/UDisks2/drives/"
MB = 1000**2
TARGET_SIZE = 4 * MB


class AuthDismissed(Exception):
    auth_dismissed = True


def stick_snapshot(size=TARGET_SIZE, mounts=("/run/media/u/STICK",)):
    snap = {
        D + "stick": {"Drive": {
            "Vendor": "SanDisk", "Model": "Ultra", "ConnectionBus": "usb",
            "Removable": True, "MediaRemovable": True, "MediaAvailable": True, "Size": size,
        }},
        B + "sdb": {"Block": {
            "Device": "/dev/sdb", "PreferredDevice": "/dev/sdb", "Size": size,
            "Drive": D + "stick", "HintSystem": False, "ReadOnly": False,
            "CryptoBackingDevice": "/", "Holders": [],
        }},
    }
    if mounts is not None:
        snap[B + "sdb1"] = {
            "Block": {
                "Device": "/dev/sdb1", "PreferredDevice": "/dev/sdb1", "Size": size // 2,
                "Drive": D + "stick", "HintSystem": False, "CryptoBackingDevice": "/",
                "Holders": [],
            },
            "Partition": {"Table": B + "sdb", "Number": 1},
            "Filesystem": {"MountPoints": list(mounts)},
        }
    return snap


class FakeClient:
    """Mimics UDisksClient; the 'drive' is a temp file."""

    def __init__(self, drive_file, snapshot):
        self.drive_file = drive_file
        self.snap = snapshot
        self.calls = []
        self.opened_fds = []
        self.unmount_error = None
        self.open_error = None
        self.keep_mounted = False
        self.remount_after_rescan = False

    def snapshot(self):
        self.calls.append(("snapshot",))
        return copy.deepcopy(self.snap)

    def unmount(self, fs_path):
        self.calls.append(("unmount", fs_path))
        if self.unmount_error:
            raise self.unmount_error
        if not self.keep_mounted:
            self.snap[fs_path]["Filesystem"]["MountPoints"] = []

    def open_exclusive(self, block_path):
        self.calls.append(("open", block_path))
        if self.open_error:
            raise self.open_error
        fd = os.open(self.drive_file, os.O_RDWR)
        self.opened_fds.append(fd)
        return fd

    def rescan(self, block_path):
        self.calls.append(("rescan", block_path))
        if self.remount_after_rescan:
            self.snap[B + "sdb1"]["Filesystem"]["MountPoints"] = ["/run/media/u/NEW"]

    def power_off(self, drive_path):
        self.calls.append(("power_off", drive_path))

    def names(self):
        return [c[0] for c in self.calls if c[0] != "snapshot"]
