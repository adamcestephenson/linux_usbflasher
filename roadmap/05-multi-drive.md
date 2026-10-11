# 5. Flash several drives at once

Pick several drives and write the same image to all of them, with progress and
a result for each.

## Why

Useful for classrooms, workshops and install parties.

## Approach

- `FlashJob` is already one job per drive, so this means running several in
  parallel.
- Read the image once and feed each drive from the shared reader, rather than
  reading the file N times. A slow drive should not stall the others
  indefinitely, so use a bounded buffer per drive.
- UI: allow multiple selection in the drive picker, show one progress row per
  drive, and a per-drive result on the result page.
- Hotplug during a flash needs per-drive handling: one drive disappearing must
  not cancel the rest.

## Open issues

- One polkit prompt per drive, or one for the batch? `auth_admin_keep` caches
  the authorization, so likely one.
- USB bandwidth is shared, so adding drives slows each one down; show per-drive
  and total speed.
- Per-drive cancel versus cancel all.
- Closing the window mid-flash must account for all running jobs.
- Partial failure: some drives succeed and others fail; the result page needs
  to show both clearly.
- The window currently holds a single `worker`, `cancel_event` and
  `flash_target`, so the state needs reorganising first.
