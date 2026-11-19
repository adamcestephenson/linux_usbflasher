"""Restore a drive to normal storage: validate -> unmount -> new partition
table -> one partition formatted as FAT32 or exFAT.

Like :class:`~usb_flasher.job.FlashJob` this is synchronous and blocking;
callers run it in a worker thread. It only uses the udisks client object, so
tests can supply a fake. Quick format only: nothing is zeroed.
"""

from __future__ import annotations

import enum
import threading
from dataclasses import dataclass
from typing import Callable

from .drives import Snapshot, mounted_filesystems
from .flasher import Status
from .job import Client, FlashOutcome, Stage, _status_for, check_target, unmount_all

FAT32_FILE_LIMIT_NOTE = "FAT32 cannot store single files larger than 4 GB."
EXFAT_THRESHOLD = 32 * 1000**3  # larger drives default to exFAT
MBR_MAX_SIZE = 2 * 1024**4  # an MBR can't address more than 2 TiB


class Filesystem(enum.Enum):
    FAT32 = "FAT32"
    EXFAT = "exFAT"

    @property
    def udisks_type(self) -> str:
        return "vfat" if self is Filesystem.FAT32 else "exfat"

    @property
    def mbr_type(self) -> str:
        """MBR partition type ID."""
        return "0x0c" if self is Filesystem.FAT32 else "0x07"

    @property
    def max_label(self) -> int:
        return 11 if self is Filesystem.FAT32 else 15

    @property
    def bad_label_chars(self) -> str:
        if self is Filesystem.FAT32:
            return '"*+,./:;<=>?[\\]|'
        return '"*/:<>?\\|'


def default_filesystem(size: int) -> Filesystem:
    """exFAT above 32 GB (FAT32 tools and OSes struggle there, and its 4 GB
    file limit hurts more); FAT32 otherwise."""
    return Filesystem.EXFAT if size > EXFAT_THRESHOLD else Filesystem.FAT32


def check_label(fs: Filesystem, label: str) -> str | None:
    """Return why ``label`` is invalid for ``fs``, or None if it is fine."""
    if len(label) > fs.max_label:
        return f"The label can be at most {fs.max_label} characters for {fs.value}."
    bad = sorted({c for c in label if c in fs.bad_label_chars or ord(c) < 32})
    if bad:
        return "The label cannot contain: " + " ".join(repr(c)[1:-1] for c in bad)
    return None


def normalize_label(fs: Filesystem, label: str) -> str:
    """FAT32 labels are stored in upper case."""
    label = label.strip()
    return label.upper() if fs is Filesystem.FAT32 else label


@dataclass(frozen=True)
class RestoreOptions:
    filesystem: Filesystem
    label: str = ""


class RestoreJob:
    def __init__(
        self,
        client: Client,
        target: str,
        options: RestoreOptions,
        *,
        allow_loop: bool = False,
        on_stage: Callable[[Stage], None] | None = None,
        cancel: threading.Event | None = None,
    ):
        """``target`` is the udisks Block object path of the whole disk."""
        self.client = client
        self.target = target
        self.options = options
        self.allow_loop = allow_loop
        self.on_stage = on_stage
        self.cancel = cancel or threading.Event()
        self.stage = Stage.PREPARING

    def _enter(self, stage: Stage) -> None:
        self.stage = stage
        if self.on_stage is not None:
            self.on_stage(stage)

    def _outcome(self, status: Status, message: str | None = None, **kw) -> FlashOutcome:
        return FlashOutcome(status, self.stage, message, **kw)

    def _cancelled(self) -> FlashOutcome | None:
        if self.cancel.is_set():
            return self._outcome(Status.CANCELLED, "Cancelled")
        return None

    def run(self) -> FlashOutcome:
        fs, label = self.options.filesystem, normalize_label(
            self.options.filesystem, self.options.label)
        self._enter(Stage.PREPARING)
        problem = check_label(fs, label)
        if problem:
            return self._outcome(Status.ERROR, problem)

        try:
            snapshot: Snapshot = self.client.snapshot()
        except Exception as e:  # noqa: BLE001 - surfaced to the user
            return self._outcome(Status.ERROR, str(e))
        drive, problem = check_target(snapshot, self.target, allow_loop=self.allow_loop)
        if problem:
            return self._outcome(Status.ERROR, problem)
        if (c := self._cancelled()):
            return c

        if mounted_filesystems(snapshot, self.target):
            self._enter(Stage.UNMOUNTING)
            try:
                snapshot = unmount_all(self.client, snapshot, self.target)
            except Exception as e:  # noqa: BLE001
                return self._outcome(_status_for(e), str(e))
            drive, problem = check_target(snapshot, self.target, allow_loop=self.allow_loop)
            if problem:
                return self._outcome(Status.ERROR, problem)
        if (c := self._cancelled()):
            return c

        # An MBR can't describe a drive over 2 TiB, so fall back to GPT there.
        table = "dos" if drive.size <= MBR_MAX_SIZE else "gpt"

        self._enter(Stage.PARTITIONING)
        try:
            self.client.create_partition_table(self.target, table)
        except Exception as e:  # noqa: BLE001
            return self._outcome(_status_for(e), str(e))
        # The old data is gone from here on, even if a later step fails.
        if (c := self._cancelled()):
            c.drive_modified = True
            c.message = "Cancelled. The drive has an empty partition table and no filesystem."
            return c

        self._enter(Stage.FORMATTING)
        try:
            self.client.create_partition_and_format(
                self.target, fs_type=fs.udisks_type, label=label,
                partition_type=fs.mbr_type if table == "dos" else "",
                mkfs_args=["-F", "32"] if fs is Filesystem.FAT32 else None,
            )
        except Exception as e:  # noqa: BLE001
            return self._outcome(
                _status_for(e),
                f"{e}. The drive has no usable filesystem; restore it again.",
                drive_modified=True)

        return self._finish()

    def _finish(self) -> FlashOutcome:
        warnings: list[str] = []
        self._enter(Stage.FINISHING)
        try:
            self.client.rescan(self.target)
        except Exception as e:  # noqa: BLE001 - cosmetic; udev also rescans
            warnings.append(f"Could not rescan partitions: {e}")
        return self._outcome(Status.SUCCESS, "Restore complete", warnings=warnings,
                             drive_modified=True)

