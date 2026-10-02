#!/usr/bin/env python3
"""Expire only finalized Fluentd archive files; never touch positions or buffers."""
import argparse
from pathlib import Path
import re
import stat
import time

ARCHIVE = Path('/var/lib/devops-foundation/fluentd/archive')
RETENTION_SECONDS = 3 * 24 * 60 * 60
ARCHIVE_NAME = re.compile(r'nginx\.\d{12}\.jsonl')


def expired_files(directory, now):
    directory = Path(directory)
    for path in [directory, *directory.parents]:
        if path.is_symlink():
            raise ValueError('Refusing an archive path with a symbolic-link component')
    if not directory.is_dir():
        raise ValueError('Archive directory is absent')
    expired = []
    for path in directory.iterdir():
        if not ARCHIVE_NAME.fullmatch(path.name):
            continue
        info = path.lstat()
        if stat.S_ISREG(info.st_mode) and info.st_mtime < now - RETENTION_SECONDS:
            expired.append(path)
    return sorted(expired)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='List eligible files without deleting them')
    args = parser.parse_args()
    files = expired_files(ARCHIVE, time.time())
    for path in files:
        if not args.dry_run:
            path.unlink()
        print(('Would remove ' if args.dry_run else 'Removed ') + path.name)
    print(f'Archive files selected: {len(files)}; retention: 72 hours')


if __name__ == '__main__':
    main()
