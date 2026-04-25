#!/usr/bin/env python3
"""Prune old files from device folders, but only after confirming each file
exists with matching size on a remote SSH backup server.

See CLAUDE.md and prune.yaml.example for usage.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import posixpath
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

import yaml


REMOTE_CHECK_SCRIPT = r"""
while [ "$#" -gt 0 ]; do
  p=$1; s=$2; shift 2
  if [ ! -e "$p" ]; then
    echo "MISSING"
  else
    rs=$(stat -c '%s' "$p" 2>/dev/null)
    if [ "$rs" = "$s" ]; then
      echo "OK"
    else
      echo "MISMATCH:$rs"
    fi
  fi
done
"""


@dataclasses.dataclass
class Target:
    local: Path
    ssh: str
    remote: str  # remote root, posix path string


@dataclasses.dataclass
class Candidate:
    path: Path          # absolute local path
    size: int
    mtime: float
    remote_path: str    # absolute remote path (posix)


def load_config(path: Path) -> list[Target]:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    raw = data.get("scan_targets")
    if not isinstance(raw, list) or not raw:
        sys.exit(f"{path}: 'scan_targets' must be a non-empty list")
    targets: list[Target] = []
    for i, entry in enumerate(raw):
        for key in ("local", "ssh", "remote"):
            if key not in entry:
                sys.exit(f"{path}: scan_targets[{i}] missing '{key}'")
        local = Path(entry["local"])
        if not local.is_dir():
            sys.exit(f"{path}: scan_targets[{i}].local '{local}' is not a directory")
        targets.append(Target(local=local, ssh=entry["ssh"], remote=entry["remote"].rstrip("/")))
    return targets


def find_prune_folders(root: Path) -> list[Path]:
    """Return every directory under `root` (inclusive) that contains a `.prune` file."""
    found: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        if ".prune" in filenames:
            found.append(Path(dirpath))
    return found


def read_max_age_days(prune_file: Path) -> int | None:
    """Parse the integer day count from a .prune file. Tolerates whitespace
    and `#` comments. Returns None on invalid/empty input."""
    try:
        text = prune_file.read_text()
    except OSError as e:
        print(f"warning: cannot read {prune_file}: {e}", file=sys.stderr)
        return None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            n = int(line)
        except ValueError:
            print(f"warning: {prune_file}: cannot parse '{line}' as int", file=sys.stderr)
            return None
        if n <= 0:
            print(f"warning: {prune_file}: max age must be positive, got {n}", file=sys.stderr)
            return None
        return n
    print(f"warning: {prune_file}: empty (no max age)", file=sys.stderr)
    return None


def governed_files(prune_dir: Path, all_prune_dirs: set[Path]) -> Iterable[Path]:
    """Yield every regular file under `prune_dir` whose closest ancestor
    `.prune` directory is `prune_dir` itself. Skips the `.prune` file.
    Does not follow symlinks. Folders are never yielded."""
    for dirpath, dirnames, filenames in os.walk(prune_dir, followlinks=False):
        # Prune any subdir that has its own .prune (it'll be processed separately).
        # Don't prune prune_dir itself even though it matches all_prune_dirs.
        kept_dirs = []
        for d in dirnames:
            sub = Path(dirpath, d)
            if sub in all_prune_dirs:
                continue
            kept_dirs.append(d)
        dirnames[:] = kept_dirs

        for name in filenames:
            if name == ".prune":
                continue
            p = Path(dirpath, name)
            if p.is_symlink():
                continue
            if p.is_file():
                yield p


def select_old(files: Iterable[Path], max_age_days: int, now: float) -> list[Candidate]:
    cutoff = now - max_age_days * 86400.0
    out: list[Candidate] = []
    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime < cutoff:
            out.append(Candidate(path=p, size=st.st_size, mtime=st.st_mtime, remote_path=""))
    return out


def fill_remote_paths(candidates: list[Candidate], local_root: Path, remote_root: str) -> None:
    for c in candidates:
        rel = c.path.relative_to(local_root)
        c.remote_path = posixpath.join(remote_root, *rel.parts)


def remote_check(ssh: str, candidates: list[Candidate]) -> dict[str, str]:
    """Run a single SSH call to check existence + size for every candidate.
    Returns per-path status: "OK", "MISSING", or "MISMATCH:<remote_size>".
    Raises RuntimeError on SSH or protocol failure."""
    if not candidates:
        return {}
    # ssh joins remote-command argv with spaces, so we must shell-quote ourselves
    # for paths with spaces, parens, quotes, etc. to survive intact.
    quoted = " ".join(
        shlex.quote(a)
        for c in candidates
        for a in (c.remote_path, str(c.size))
    )
    remote_cmd = f"bash -s -- {quoted}"
    args = ["ssh", "-o", "BatchMode=yes", ssh, remote_cmd]
    try:
        result = subprocess.run(
            args,
            input=REMOTE_CHECK_SCRIPT,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"ssh failed: {e}") from e
    if result.returncode != 0:
        raise RuntimeError(f"ssh exit {result.returncode}: {result.stderr.strip()}")
    lines = result.stdout.splitlines()
    if len(lines) != len(candidates):
        raise RuntimeError(
            f"ssh returned {len(lines)} lines for {len(candidates)} files; "
            f"stderr: {result.stderr.strip()}"
        )
    return {c.remote_path: line for c, line in zip(candidates, lines)}


def human_size(n: int) -> str:
    return f"{n / (1024 * 1024):.0f} Mb"


USE_COLOR = sys.stdout.isatty()


def yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m" if USE_COLOR else s


def display_name(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return str(path)
    s = str(rel)
    return "." if s == "." else s


def folder_header(folder: Path, root: Path) -> str:
    return yellow(f'Folder "{display_name(folder, root)}":')


def prompt_confirm(question: str) -> bool:
    try:
        ans = input(question).strip().lower()
    except EOFError:
        return False
    return ans in ("", "y", "yes")


def delete_files(files: list[Candidate], dry_run: bool) -> tuple[int, int]:
    deleted = 0
    freed = 0
    for c in files:
        if dry_run:
            deleted += 1
            freed += c.size
            continue
        try:
            os.unlink(c.path)
        except OSError as e:
            print(f"  failed to delete {c.path}: {e}", file=sys.stderr)
            continue
        deleted += 1
        freed += c.size
    return deleted, freed


def process_prune_folder(
    folder: Path,
    target: Target,
    all_prune_dirs: set[Path],
    args: argparse.Namespace,
    now: float,
    errors: list[tuple[str, str]],
) -> tuple[int, int]:
    """Process one .prune folder. Returns (files_deleted, bytes_freed)."""
    max_age = read_max_age_days(folder / ".prune")
    if max_age is None:
        return 0, 0

    files = list(governed_files(folder, all_prune_dirs))
    total_size = sum(f.stat().st_size for f in files if f.exists())

    candidates = select_old(files, max_age, now)
    fill_remote_paths(candidates, target.local, target.remote)

    header = folder_header(folder, target.local)
    folder_disp = display_name(folder, target.local)

    try:
        statuses = remote_check(target.ssh, candidates)
    except RuntimeError as e:
        if args.skip_errors:
            errors.append((folder_disp, f'ssh — {e}'))
            return 0, 0
        print(header, file=sys.stderr)
        sys.exit(f'   Aborting: cannot verify backup — {e}')

    verified: list[Candidate] = []
    unverified: list[tuple[Candidate, str]] = []
    for c in candidates:
        s = statuses.get(c.remote_path, "MISSING")
        if s == "OK":
            verified.append(c)
        else:
            unverified.append((c, s))

    if unverified:
        if args.skip_errors:
            for c, reason in unverified:
                rel = c.path.relative_to(folder)
                errors.append((folder_disp, f'[{reason}] {rel}'))
        else:
            print(header, file=sys.stderr)
            print(f'   {len(unverified)} old file(s) not safely backed up:', file=sys.stderr)
            for c, reason in unverified:
                rel = c.path.relative_to(folder)
                print(f"      [{reason}] {rel}", file=sys.stderr)
            sys.exit("Aborting: backup is not in sync. Re-run after the backup is up to date.")

    prune_size = sum(c.size for c in verified)

    if not verified:
        return 0, 0

    size_line = (
        f'   total size {human_size(total_size)}, '
        f'size to prune {human_size(prune_size)}.'
    )

    if args.yes:
        print(header)
        print(size_line)
        print('   Auto-confirmed (--yes).')
    else:
        question = f'{header}\n{size_line}\n   Allow pruning [y]/n? '
        if not prompt_confirm(question):
            print('   Skipped by user.')
            return 0, 0

    deleted, freed = delete_files(verified, args.dry_run)
    suffix = " (dry run)" if args.dry_run else ""
    print(f'   Deleted {deleted} file(s), freed {human_size(freed)}{suffix}.')
    return deleted, freed


def process_target(target: Target, args: argparse.Namespace, now: float,
                   errors: list[tuple[str, str]]) -> tuple[int, int]:
    prune_dirs = find_prune_folders(target.local)
    if not prune_dirs:
        print(f"[{target.local}] no .prune files found.")
        return 0, 0
    prune_dir_set = set(prune_dirs)
    print(f"[{target.local}]\n found {len(prune_dirs)} pruned folders")
    total_deleted = 0
    total_freed = 0
    for folder in sorted(prune_dirs):
        d, f = process_prune_folder(folder, target, prune_dir_set, args, now, errors)
        total_deleted += d
        total_freed += f
    return total_deleted, total_freed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backup-aware folder pruner.")
    parser.add_argument("--config", default="prune.yaml", type=Path,
                        help="Path to prune.yaml (default: ./prune.yaml).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Do everything except actually deleting files.")
    parser.add_argument("--yes", action="store_true",
                        help="Auto-confirm every folder prompt.")
    parser.add_argument("--skip-errors", action="store_true",
                        help="Skip files/folders that can't be verified instead of aborting; "
                             "list them at the end.")
    parser.add_argument("--verbose", action="store_true",
                        help="Reserved for future use.")
    args = parser.parse_args(argv)

    if not args.config.is_absolute():
        args.config = Path(__file__).resolve().parent / args.config
    if not args.config.exists():
        sys.exit(f"config not found: {args.config}")
    targets = load_config(args.config)

    now = time.time()
    grand_deleted = 0
    grand_freed = 0
    errors: list[tuple[str, str]] = []
    for t in targets:
        d, f = process_target(t, args, now, errors)
        grand_deleted += d
        grand_freed += f

    if errors:
        print(f"\nSkipped {len(errors)} item(s) due to errors:")
        last_folder = None
        for folder_disp, msg in errors:
            if folder_disp != last_folder:
                print(f'   {yellow(folder_disp)}')
                last_folder = folder_disp
            print(f'      {msg}')

    suffix = " (dry run)" if args.dry_run else ""
    print(f"\nDone. Deleted {grand_deleted} file(s), freed {human_size(grand_freed)}{suffix}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
