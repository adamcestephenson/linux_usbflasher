# 2. Restore a drive to normal storage

A "Restore Drive" action: wipe the drive and leave one partition formatted as
FAT32 or exFAT, with a label the user chooses.

## Why

After a stick has been used as an installer, it keeps an odd partition layout
and file managers often show it as tiny or unusable. Etcher does not offer
this, and it is a common annoyance.

## Approach

- udisks2 can do it: `Block.Format` (create a new partition table), then
  `PartitionTable.CreatePartitionAndFormat`.
- Reuse the existing safety rules (`classify_drives`) so system drives, drives
  in use and non-removable drives stay off-limits.
- Reuse the unmount step from `FlashJob` and the polkit flow in `udisks.py`.
- UI: an entry in the main menu opens the drive picker, then a small dialog
  for filesystem (FAT32 / exFAT) and label, then the same destructive
  confirmation as flashing.

## Open issues

- FAT32 cannot hold files over 4 GB; offer exFAT for large drives, and pick a
  sensible default by drive size.
- Partition table type: MBR for compatibility, or GPT?
- Whether to offer a "quick" versus "zero the whole drive" option. Zeroing is
  slow and rarely needed.
- Needs additional polkit actions for formatting, so the password prompt may
  appear more than once.

## Tests

Fake-client tests for the call sequence and error handling; an end-to-end run
on a loop device through real udisks.
