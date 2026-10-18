# USB Flasher

A small Linux app for writing disk images (`.img`, `.iso`, `.raw`) to USB drives
and SD cards, similar to [balenaEtcher](https://etcher.balena.io/). It uses
Python with GTK4/libadwaita, and it does **not** run as root: all privileged
work goes through [udisks2](https://www.freedesktop.org/wiki/Software/udisks/),
and polkit asks for your password when needed.

> **Status:** early development (v0.1.0). Flashing and verification have been
> tested on loop devices and on one real USB stick (a 16 GB Corsair Voyager
> flashed with a Linux Mint ISO), including the polkit password prompt. That is
> a small amount of hardware testing, so double-check the target drive before
> flashing.

## Features

- Three steps: pick an image, pick a target drive, flash.
- Read-back verification after writing (on by default), plus a SHA-256
  checksum of the image.
- Only removable drives are offered. Drives are blocked, with the reason
  shown, if they:
  - are marked as system drives or are read-only;
  - have system paths mounted (`/`, `/boot`, `/home`, `/var`, …), including
    through LUKS, LVM or RAID;
  - are in use as swap, or by an unlocked LUKS volume or active LVM/RAID;
  - contain the image being flashed;
  - are too small for the image.
- Warns about drives over 64 GB, and about ISO images that aren't hybrid
  (and may not boot from USB).
- The drive list updates live as drives are plugged in or removed.
- Mounted partitions on the target are unmounted automatically.
- The drive is opened exclusively for the whole write and verify, so nothing
  can mount it partway through.
- Cancel at any time. Optionally eject (power off) the drive when done.
- Drag and drop an image onto the window.
- Suspend and logout are blocked while flashing, and closing the window
  mid-flash asks for confirmation.
- A command-line mode for listing drives and flashing.

- Compressed images (`.xz`, `.gz`, `.bz2`, `.zip`, and `.zst` on Python 3.14+)
  are decompressed while writing. For `.gz` and `.bz2` the uncompressed size
  is unknown up front, so progress is indeterminate and the write fails if
  the image turns out larger than the drive. A `.zip` must contain exactly
  one `.img`/`.iso`/`.raw`.

Not supported yet: flashing from a URL, writing to several drives at once, and cloning a drive.

## Requirements

- Linux with udisks2 and polkit running, and a polkit authentication agent in
  your desktop session
- Python 3.11+
- PyGObject, GTK 4 and libadwaita 1.5+ (developed against GTK 4.22 and
  libadwaita 1.9)

On Arch Linux:

```sh
sudo pacman -S python python-gobject gtk4 libadwaita udisks2 polkit
```

## Installation

### Arch Linux package

`packaging/arch/PKGBUILD` builds a `usb-flasher-git` package from this
repository's latest commit. It installs the `usb-flasher` command, the desktop
entry and the icon system-wide, so no manual launcher setup is needed:

```sh
git clone https://github.com/adamcestephenson/linux_usbflasher.git
cd linux_usbflasher/packaging/arch
makepkg -si
```

`makepkg` installs the build dependencies, runs the test suite, and then
installs the package with `pacman`. The package conflicts with a future
`usb-flasher` release package.

### From a source checkout

PyGObject comes from the system, so create the virtual environment with
`--system-site-packages`:

```sh
git clone https://github.com/adamcestephenson/linux_usbflasher.git
cd linux_usbflasher
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e '.[dev]'
```

### Application launcher entry (optional)

To start the app from your launcher (Walker, rofi, GNOME, …), link the command
onto your `PATH` and install the desktop entry and icon for your user:

```sh
mkdir -p ~/.local/bin
ln -sf "$PWD/.venv/bin/usb-flasher" ~/.local/bin/usb-flasher
install -Dm644 data/io.github.adamcestephenson.UsbFlasher.desktop \
  ~/.local/share/applications/io.github.adamcestephenson.UsbFlasher.desktop
install -Dm644 data/icons/hicolor/scalable/apps/io.github.adamcestephenson.UsbFlasher.svg \
  ~/.local/share/icons/hicolor/scalable/apps/io.github.adamcestephenson.UsbFlasher.svg
```

The `usb-flasher` command must be on `PATH`, otherwise desktop environments hide
the entry. The entry also lets you pick "USB Flasher" in a file manager's
"Open With" menu for `.iso` and `.img` files.

## Usage

### GUI

```sh
.venv/bin/usb-flasher                 # start the app
.venv/bin/usb-flasher ~/distro.iso    # start with an image already selected
```

### Command line

```sh
# List drives that can be flashed (--all also shows hidden ones)
.venv/bin/usb-flasher --list-drives
.venv/bin/usb-flasher --list-drives --all --watch

# Flash an image (asks you to type "yes" first)
.venv/bin/usb-flasher --flash ~/distro.iso --target /dev/sdX

# Skip verification, power off the drive when done, don't ask for confirmation
.venv/bin/usb-flasher --flash ~/distro.iso --target /dev/sdX --no-verify --eject --yes
```

Press Ctrl-C to cancel. Exit codes: `0` success, `1` error, `130` cancelled.

Run `usb-flasher --help` for all options.

## How it works

1. The image is checked: file type, size, and whether an ISO is hybrid.
2. The target drive is checked again with udisks2 just before writing.
3. Mounted filesystems on the drive are unmounted with
   `Filesystem.Unmount`, innermost layers first.
4. The drive is opened with `Block.OpenDevice` (read-write, `O_EXCL | O_SYNC`).
   polkit may ask for your password at this point.
5. The image is written in 4 MiB chunks and then flushed with `fsync`.
6. The page cache is dropped and the data is read back and compared chunk by
   chunk. If something differs, the offset of the first bad chunk is reported.
7. The descriptor is closed, the partition table is rescanned, and the drive
   is powered off with `Drive.PowerOff` if you asked for that.

Dismissing the password prompt counts as a cancel, not an error.

## Development

### Project layout

```
src/usb_flasher/
  __main__.py      # entry point: GUI by default, or the CLI modes
  app.py           # Adw.Application
  window.py        # main window: select / progress / result pages
  drive_picker.py  # target drive dialog
  drives.py        # drive discovery and safety filtering (udisks2)
  udisks.py        # privileged udisks2 calls (unmount, open, rescan, power off)
  job.py           # FlashJob: one complete flash, run in a worker thread
  flasher.py       # write/verify engine on raw file descriptors
  image.py         # image validation
data/              # desktop entry and app icon
packaging/arch/    # PKGBUILD
tests/             # pytest suite
```

`flasher.py`, `image.py` and the filtering logic in `drives.py` don't depend
on GTK or D-Bus, so they are tested with temporary files and fake udisks data.

### Running the tests

```sh
.venv/bin/pytest
```

The GUI tests start their own headless Broadway display (`gtk4-broadwayd`,
part of the `gtk4` package), so no windows appear on your desktop. Set
`USB_FLASHER_TEST_NATIVE_DISPLAY=1` to run them on your real display instead.

### Testing without a USB stick

Attach an image file as a loop device through udisks and start the app with
`--dev-allow-loop`, which lets loop devices be chosen as targets:

```sh
truncate -s 256M /tmp/target.img
udisksctl loop-setup -f /tmp/target.img        # prints e.g. /dev/loop0
.venv/bin/usb-flasher --dev-allow-loop
# or: .venv/bin/usb-flasher --flash some.img --target /dev/loop0 --dev-allow-loop
udisksctl loop-delete -b /dev/loop0
```

udisks doesn't ask for a password for loop devices you set up yourself, so
this doesn't exercise the polkit prompt.

## License

[MIT](LICENSE) © 2026 Adam Stephenson. Anyone may use, modify and redistribute
this software, including commercially, as long as the copyright and license
notice are kept.
