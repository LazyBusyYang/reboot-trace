"""Compatibility entry point for legacy single-file migrations.

All explicit offline migrations now produce the format-v2 evidence-capsule
layout. Existing automation may keep invoking ``storage_migrate``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .storage_repack import repack


def migrate(data_dir: Path, backup_root: Path, *, dry_run: bool = False) -> dict[str, object]:
    return repack(data_dir, backup_root, dry_run=dry_run)


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description="Migrate stopped reboot-trace storage to evidence capsules")
    parser.add_argument("--data-dir",type=Path,required=True)
    parser.add_argument("--backup-dir",type=Path,required=True)
    parser.add_argument("--dry-run",action="store_true")
    args=parser.parse_args(argv)
    try:
        print(json.dumps(migrate(args.data_dir,args.backup_dir,dry_run=args.dry_run),ensure_ascii=False,sort_keys=True))
    except Exception as exc:
        print(f"migration failed: {exc}",file=sys.stderr)
        return 1
    return 0


if __name__=="__main__":raise SystemExit(main())
