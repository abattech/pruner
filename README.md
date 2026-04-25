# pruner

A small Python script that deletes old files from a device, but only after
confirming each file is already backed up over SSH.

How it works (per `CLAUDE.md`):

1. You drop a `.prune` file containing a max age in days into any folder.
2. `prune.py` walks your configured roots, finds those folders, and selects
   files older than `max_age_days`.
3. For each candidate, it asks the backup server (over SSH) whether the file
   exists at the matching remote path with the same byte size.
4. It prompts you per folder before deleting only the verified-old files.

Folders, symlinks, and `.prune` files themselves are never deleted.

## Setup

### Termux (Android)

```sh
pkg install python openssh
pip install pyyaml
```

Generate a key and add it to the backup server:

```sh
ssh-keygen -t ed25519
ssh-copy-id user@backup.lan
```

### Linux dev box

Same idea — Python 3.10+, OpenSSH client, `pip install pyyaml`.

## Configure

Copy `prune.yaml.example` to `prune.yaml` and edit the `scan_targets` list.
Each target is `{ local, ssh, remote }`: files at `<local>/<rel>` are checked
against `<ssh>:<remote>/<rel>`.

## Run

```sh
python prune.py --dry-run        # see what would be deleted
python prune.py                  # interactive: prompt per folder
python prune.py --yes            # auto-confirm every folder
python prune.py --config /path/to/prune.yaml
```

The per-folder prompt looks like:

```
Folder "/storage/.../DCIM/Camera": total size 4321.0 Mb, size to prune 980.5 Mb. Allow pruning [y]/n?
```

Hit Enter to accept (default is yes), `n` to skip that folder.
