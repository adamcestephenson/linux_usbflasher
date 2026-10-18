"""A complete flash operation: validate -> unmount -> open -> write -> verify -> finish.

:class:`FlashJob` is synchronous and blocking; callers (GUI, CLI) run it in a
worker thread. Callbacks are invoked from that thread. It talks to udisks2
only through the client object, so tests can supply a fake.
"""

from __future__ import annotations

import enum
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from . import flasher
from .drives import DriveInfo, Snapshot, classify_drives, mounted_filesystems
from .flasher import Progress, Status
from .archives import ArchiveError
from .image import ImageError, ImageInfo, check_fits, inspect_image


class Stage(enum.Enum):
    PREPARING = "preparing"
    UNMOUNTING = "unmounting"
    OPENING = "opening"  # may show a polkit password prompt
    WRITING = "writing"
    VERIFYING = "verifying"
    FINISHING = "finishing"
    EJECTING = "ejecting"


class Client(Protocol):
    """The subset of :class:`usb_flasher.udisks.UDisksClient` used here."""

    def snapshot(self) -> Snapshot: ...
    def unmount(self, fs_path: str) -> None: ...
    def open_exclusive(self, block_path: str) -> int: ...
    def rescan(self, block_path: str) -> None: ...
    def power_off(self, drive_path: str) -> None: ...


@dataclass
class FlashOutcome:
    status: Status
    stage: Stage
    message: str | None = None
    sha256: str | None = None
    image_size: int = 0
    bytes_done: int = 0
    error_offset: int | None = None
    errno: int | None = None
    warnings: list[str] = field(default_factory=list)  # non-fatal problems
    drive_modified: bool = False  # True once any data may have been written
    ejected: bool = False  # the drive was powered off after a successful flash


INCOMPLETE = "The drive now contains an incomplete image."


def eject_drive(client: Client, block_path: str, drive_path: str) -> None:
    """Unmount anything on the disk (e.g. partitions automounted after a
    flash) and power the drive off. Raises on failure."""
    for fs in mounted_filesystems(client.snapshot(), block_path):
        client.unmount(fs)
    client.power_off(drive_path)


def _status_for(e: Exception) -> Status:
    """Dismissing the polkit prompt counts as a cancel, not an error."""
    return Status.CANCELLED if getattr(e, "auth_dismissed", False) else Status.ERROR

UNMOUNT_SETTLE_TRIES = 10
UNMOUNT_SETTLE_DELAY = 0.2


class FlashJob:
    def __init__(
        self,
        client: Client,
        image_path: str | os.PathLike,
        target: str,
        *,
        verify: bool = True,
        eject: bool = False,
        allow_loop: bool = False,
        on_stage: Callable[[Stage], None] | None = None,
        on_progress: Callable[[Progress], None] | None = None,
        cancel: threading.Event | None = None,
        chunk_size: int = flasher.CHUNK_SIZE,
    ):
        """``target`` is the udisks Block object path of the whole disk
        (:attr:`DriveInfo.object_path`)."""
        self.client = client
        self.image_path = image_path
        self.target = target
        self.verify = verify
        self.eject = eject
        self.allow_loop = allow_loop
        self.on_stage = on_stage
        self.on_progress = on_progress
        self.cancel = cancel or threading.Event()
        self.stage = Stage.PREPARING
        self.chunk_size = chunk_size
        self._dst_fd: int | None = None

    # -- helpers ---------------------------------------------------------

    def _enter(self, stage: Stage) -> None:
        self.stage = stage
        if self.on_stage is not None:
            self.on_stage(stage)

    def _outcome(self, status: Status, message: str | None = None, **kw) -> FlashOutcome:
        return FlashOutcome(status, self.stage, message, **kw)

    def _cancelled(self) -> bool:
        return self.cancel.is_set()

    def _find_drive(self, snapshot: Snapshot) -> DriveInfo | None:
        for d in classify_drives(
            snapshot, allow_loop=self.allow_loop, include_hidden=True,
            source_path=self.image_path,
        ):
            if d.object_path == self.target:
                return d
        return None

    def _check_drive(self, snapshot: Snapshot) -> tuple[DriveInfo | None, str | None]:
        drive = self._find_drive(snapshot)
        if drive is None:
            return None, "The drive is no longer connected."
        if not drive.selectable:
            return drive, f"{drive.device} cannot be used: " + "; ".join(drive.reasons)
        return drive, None

    # -- main ------------------------------------------------------------

    def _close_dst(self) -> None:
        fd, self._dst_fd = self._dst_fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def run(self) -> FlashOutcome:
        self._enter(Stage.PREPARING)
        try:
            image = inspect_image(self.image_path)
        except ImageError as e:
            return self._outcome(Status.ERROR, str(e))

        try:
            snapshot = self.client.snapshot()
        except Exception as e:  # noqa: BLE001 - surfaced to the user
            return self._outcome(Status.ERROR, str(e))
        drive, problem = self._check_drive(snapshot)
        if problem:
            return self._outcome(Status.ERROR, problem, image_size=image.size or 0)
        try:
            check_fits(image.size, drive.size)
        except ImageError as e:
            return self._outcome(Status.ERROR, str(e), image_size=image.size or 0)

        if self._cancelled():
            return self._outcome(Status.CANCELLED, "Cancelled", image_size=image.size or 0)

        # Unmount everything on the drive, deepest first.
        mounted = mounted_filesystems(snapshot, self.target)
        if mounted:
            self._enter(Stage.UNMOUNTING)
            for fs in mounted:
                try:
                    self.client.unmount(fs)
                except Exception as e:  # noqa: BLE001
                    status = _status_for(e)
                    return self._outcome(status, str(e), image_size=image.size or 0)

            for attempt in range(UNMOUNT_SETTLE_TRIES):
                try:
                    snapshot = self.client.snapshot()
                except Exception as e:  # noqa: BLE001
                    return self._outcome(Status.ERROR, str(e), image_size=image.size or 0)
                if not mounted_filesystems(snapshot, self.target):
                    break
                time.sleep(UNMOUNT_SETTLE_DELAY)
            else:
                return self._outcome(
                    Status.ERROR, "The drive is still mounted after unmounting.",
                    image_size=image.size or 0,
                )
            drive, problem = self._check_drive(snapshot)
            if problem:
                return self._outcome(Status.ERROR, problem, image_size=image.size or 0)

        if self._cancelled():
            return self._outcome(Status.CANCELLED, "Cancelled", image_size=image.size or 0)

        self._enter(Stage.OPENING)
        try:
            dst_fd = self.client.open_exclusive(self.target)
        except Exception as e:  # noqa: BLE001
            status = _status_for(e)
            return self._outcome(status, str(e), image_size=image.size or 0)

        self._dst_fd = dst_fd
        try:
            if self._cancelled():
                return self._outcome(Status.CANCELLED, "Cancelled", image_size=image.size or 0)
            return self._write_and_verify(image, dst_fd, drive)
        finally:
            self._close_dst()

    def _write_and_verify(self, image: ImageInfo, dst_fd: int,
                          drive: DriveInfo) -> FlashOutcome:
        image_size = image.size or 0

        def fail(res: flasher.Result, modified: bool) -> FlashOutcome:
            msg = res.message or "Failed"
            if modified and res.status is not Status.SUCCESS and self.stage is Stage.WRITING:
                msg = f"{msg}. {INCOMPLETE}"
            return self._outcome(
                res.status, msg, image_size=image_size, bytes_done=res.bytes_done,
                error_offset=res.error_offset, errno=res.errno, drive_modified=modified,
            )

        try:
            target_size = os.lseek(dst_fd, 0, os.SEEK_END)
            os.lseek(dst_fd, 0, os.SEEK_SET)
        except OSError as e:
            return self._outcome(Status.ERROR, f"Cannot determine drive size: {e.strerror}",
                                 image_size=image_size, errno=e.errno)

        try:
            with image.open() as src:
                self._enter(Stage.WRITING)
                written = flasher.write_image(
                    src, dst_fd, image.size, target_size=target_size,
                    progress=self.on_progress, cancel=self.cancel, chunk_size=self.chunk_size,
                )
        except (OSError, ArchiveError) as e:
            return self._outcome(Status.ERROR, f"Cannot open image: {getattr(e, 'strerror', None) or e}",
                                 image_size=image_size, errno=getattr(e, "errno", None))

        modified = written.bytes_done > 0 or written.status is Status.SUCCESS
        if written.status is not Status.SUCCESS:
            return fail(written, modified)

        if self.verify:
            self._enter(Stage.VERIFYING)
            checked = flasher.verify(
                dst_fd, written, progress=self.on_progress, cancel=self.cancel,
            )
            if checked.status is not Status.SUCCESS:
                return fail(checked, True)

        return self._finish(drive, written, written.total)

    def _finish(self, drive: DriveInfo, written: flasher.WriteResult,
                image_size: int) -> FlashOutcome:
        warnings: list[str] = []
        self._enter(Stage.FINISHING)
        # Release exclusive access so the kernel can see the new partitions.
        self._close_dst()
        try:
            self.client.rescan(self.target)
        except Exception as e:  # noqa: BLE001 - cosmetic; udev also rescans on close
            warnings.append(f"Could not rescan partitions: {e}")

        ejected = False
        if self.eject and drive.drive_path is not None:  # loops: nothing to power off
            self._enter(Stage.EJECTING)
            try:
                eject_drive(self.client, self.target, drive.drive_path)
                ejected = True
            except Exception as e:  # noqa: BLE001
                warnings.append(f"Flashed successfully, but ejecting failed: {e}")

        return self._outcome(
            Status.SUCCESS, "Flash complete", sha256=written.sha256,
            image_size=image_size, bytes_done=image_size, warnings=warnings,
            drive_modified=True, ejected=ejected,
        )
