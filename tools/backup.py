#!/usr/bin/env python3
"""Copy the trade ledger out of the repo folder (stdlib only).

  python tools/backup.py [DEST_DIR]

Default DEST_DIR: %USERPROFILE%/OneDrive/Backup-anwar/rs3-flipper (synced off this machine).
Keeps one snapshot per day (state-YYYY-MM-DD.json, overwritten during the day) for 90 days,
plus a mirror of ledger/plans/. Also trims the local ledger/backups/ to the newest 300 files.
Run by the cron wrapper after every plan; safe to run by hand any time.
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "ledger"
DEFAULT_DEST = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "OneDrive" / "Backup-anwar" / "rs3-flipper"
KEEP_DAYS = 90
KEEP_LOCAL = 300


def backup(dest: Path) -> Path | None:
    state = LEDGER / "state.json"
    if not state.exists():
        return None
    dest.mkdir(parents=True, exist_ok=True)
    snap = dest / f"state-{dt.date.today():%Y-%m-%d}.json"
    tmp = snap.with_suffix(".tmp")
    shutil.copy2(state, tmp)
    tmp.replace(snap)
    if (LEDGER / "plans").exists():
        shutil.copytree(LEDGER / "plans", dest / "plans", dirs_exist_ok=True)
    cutoff = dt.date.today() - dt.timedelta(days=KEEP_DAYS)
    for f in dest.glob("state-*.json"):
        try:
            if dt.date.fromisoformat(f.stem[6:]) < cutoff:
                f.unlink()
        except ValueError:
            pass
    local = sorted((LEDGER / "backups").glob("state-*.json")) if (LEDGER / "backups").exists() else []
    for f in local[:-KEEP_LOCAL]:
        f.unlink()
    return snap


if __name__ == "__main__":
    out = backup(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DEST)
    print(f"Ledger backed up to {out}" if out else "No ledger yet; nothing to back up.")
