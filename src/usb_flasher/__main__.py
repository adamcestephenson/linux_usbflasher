"""Command-line entry point. The GUI will be added in a later milestone."""

from __future__ import annotations

import argparse
import os
import sys
import threading

from . import __version__
from .drives import DriveInfo, DrivesError, classify_drives, format_size


def _print_drives(drives: list[DriveInfo]) -> None:
    if not drives:
        print("No drives found.")
        return
    for d in drives:
        if d.selectable:
            state = "OK"
        elif d.hidden:
            state = "HIDDEN"
        else:
            state = "BLOCKED"
        print(f"{d.device:<14} {format_size(d.size):>10}  {d.bus or '-':<5} {state:<8} {d.label}")
        for r in d.reasons:
            print(f"{'':16}- {r}")
        for w in d.warnings:
            print(f"{'':16}! {w}")
        if d.mount_points:
            print(f"{'':16}  mounted: {', '.join(d.mount_points)} (will be unmounted)")


def _list_drives(args) -> int:
    from .drives import DriveMonitor

    try:
        monitor = DriveMonitor()
    except DrivesError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    kwargs = dict(allow_loop=args.dev_allow_loop, include_hidden=args.all, source_path=args.source)
    _print_drives(monitor.drives(**kwargs))

    if args.watch:
        from gi.repository import GLib

        def on_changed(_m):
            print("\n--- drives changed ---")
            _print_drives(monitor.drives(**kwargs))

        monitor.connect("changed", on_changed)
        loop = GLib.MainLoop()
        try:
            loop.run()
        except KeyboardInterrupt:
            pass
    return 0


def _format_eta(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _flash(args) -> int:
    from .flasher import Progress, Status
    from .image import ImageError, inspect_image
    from .job import FlashJob, Stage
    from .udisks import UDisksClient, UDisksError

    # Keep stdout and stderr (progress) in order when piped.
    sys.stdout.reconfigure(line_buffering=True)
    try:
        image = inspect_image(args.flash)
    except ImageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for w in image.warnings:
        print(f"warning: {w}", file=sys.stderr)

    try:
        client = UDisksClient()
        snapshot = client.snapshot()
    except UDisksError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    target = os.path.realpath(args.target)
    drives = classify_drives(
        snapshot, allow_loop=args.dev_allow_loop, include_hidden=True, source_path=args.flash,
    )
    matches = [d for d in drives if os.path.realpath(d.device) == target]
    if not matches:
        print(f"error: {args.target} is not a whole-disk drive known to udisks2", file=sys.stderr)
        return 1
    drive = matches[0]
    if not drive.selectable:
        print(f"error: {drive.device} cannot be used:", file=sys.stderr)
        for r in drive.reasons:
            print(f"  - {r}", file=sys.stderr)
        return 1

    print(f"Image:  {image.path} ({format_size(image.size)})")
    print(f"Target: {drive.device} - {drive.label} ({format_size(drive.size)})")
    for w in drive.warnings:
        print(f"warning: {w}", file=sys.stderr)
    if drive.mount_points:
        print(f"Will unmount: {', '.join(drive.mount_points)}")
    if not args.yes:
        answer = input(f"ALL DATA ON {drive.device} WILL BE ERASED. Type 'yes' to continue: ")
        if answer.strip().lower() != "yes":
            print("Aborted.")
            return 1

    stage_labels = {
        Stage.PREPARING: "Preparing", Stage.UNMOUNTING: "Unmounting",
        Stage.OPENING: "Opening drive (you may be asked for your password)",
        Stage.WRITING: "Writing", Stage.VERIFYING: "Verifying",
        Stage.FINISHING: "Finishing", Stage.EJECTING: "Ejecting",
    }
    tty = sys.stderr.isatty()
    last_line = [0.0]

    def on_stage(stage: Stage) -> None:
        print(f"\n{stage_labels[stage]}..." if tty else f"{stage_labels[stage]}...",
              file=sys.stderr, flush=True)

    def on_progress(p: Progress) -> None:
        import time

        now = time.monotonic()
        if p.bytes_done != p.total and now - last_line[0] < 0.25:
            return
        last_line[0] = now
        line = (f"  {p.fraction * 100:5.1f}%  {format_size(p.bytes_done):>9} / "
                f"{format_size(p.total)}  {format_size(int(p.speed))}/s  ETA {_format_eta(p.eta)}")
        if tty:
            print("\r" + line, end="", file=sys.stderr, flush=True)
        elif p.bytes_done == p.total:
            print(line, file=sys.stderr, flush=True)

    cancel = threading.Event()
    job = FlashJob(
        client, image.path, drive.object_path, verify=not args.no_verify, eject=args.eject,
        allow_loop=args.dev_allow_loop, on_stage=on_stage, on_progress=on_progress,
        cancel=cancel,
    )
    result = []
    worker = threading.Thread(target=lambda: result.append(job.run()), daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(0.2)
    except KeyboardInterrupt:
        print("\nCancelling...", file=sys.stderr)
        cancel.set()
        worker.join()

    outcome = result[0]
    print(file=sys.stderr)
    for w in outcome.warnings:
        print(f"warning: {w}", file=sys.stderr)
    if outcome.status is Status.SUCCESS:
        print(f"Done. {'Verified. ' if not args.no_verify else ''}SHA-256: {outcome.sha256}")
        return 0
    label = "Cancelled" if outcome.status is Status.CANCELLED else "Failed"
    print(f"{label} during {outcome.stage.value}: {outcome.message}", file=sys.stderr)
    return 130 if outcome.status is Status.CANCELLED else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="usb-flasher")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--list-drives", action="store_true", help="list candidate target drives")
    parser.add_argument("--all", action="store_true", help="also show hidden (non-removable) drives")
    parser.add_argument("--watch", action="store_true", help="with --list-drives: relist on hotplug")
    parser.add_argument("--source", metavar="IMAGE", help="mark the drive containing IMAGE as unusable")
    parser.add_argument("--flash", metavar="IMAGE", help="write IMAGE to --target")
    parser.add_argument("--target", metavar="DEVICE", help="target drive for --flash, e.g. /dev/sdb")
    parser.add_argument("--no-verify", action="store_true", help="skip read-back verification")
    parser.add_argument("--eject", action="store_true", help="power off the drive when done")
    parser.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    parser.add_argument(
        "--dev-allow-loop", action="store_true",
        help="developer option: offer loop devices as targets",
    )
    args = parser.parse_args(argv)

    if args.flash:
        if not args.target:
            parser.error("--flash requires --target")
        return _flash(args)
    if args.list_drives:
        return _list_drives(args)
    parser.error("nothing to do (the GUI is not implemented yet; try --list-drives or --flash)")
    return 2


if __name__ == "__main__":
    sys.exit(main())
