"""Screen a past period (e.g. one month) and write archive/<label>.md.

Usage: python src/backfill.py --start 2026-10-22 --end 2026-11-21
Fetches in one-week chunks (keeps every API query small), uses publication date
for PubMed, skips papers already screened or listed, and records what it screens
so the daily run never shows them again.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from fetch import fetch_all  # noqa: E402
from render import render_archive  # noqa: E402
from run import (DATA, SCREENED, archive_items, dedupe, h, keys_of, load_json,  # noqa: E402
                 primary)
from screen import screen  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    label = f"{start}_to_{end}"

    cfg = yaml.safe_load((ROOT / "config/sources.yaml").read_text())
    profile = (ROOT / "config/profile.md").read_text()

    cands, stats = [], {}
    d = start
    while d <= end:
        chunk_end = min(d + timedelta(days=6), end)
        c, s = fetch_all(cfg, chunk_end, start=d, pubmed_datetype="pdat")
        print(f"{d}..{chunk_end}: {s}")
        cands += c
        for k, v in s.items():
            stats[k] = stats.get(k, 0) + v
        d = chunk_end + timedelta(days=1)

    cands = dedupe(cands)
    # keep papers published inside the period (or undated)
    cands = [p for p in cands if not p.get("date") or args.start <= p["date"][:10] <= args.end]

    screened = {}
    if SCREENED.exists():
        for line in SCREENED.read_text().splitlines():
            k, v = line.split("\t")
            screened[k] = v
    reported = {k for it in load_json(DATA / "items.json", []) + archive_items() for k in keys_of(it)}
    fresh = [p for p in cands
             if not (keys_of(p) & reported) and not screened.get(h(primary(p)), "").replace(":t", "")]
    print(f"candidates {len(cands)}, to screen {len(fresh)}")

    picked = screen(fresh, profile, cfg["models"], workers=args.workers)
    for p in picked:
        for k in ("abstract", "pub_types", "stage1", "final"):
            p.pop(k, None)
        p["run_date"] = f"archive:{label}"

    stats.update({"candidates": len(cands), "screened": len(fresh), "picked": len(picked)})
    adata = DATA / "archive"
    adata.mkdir(parents=True, exist_ok=True)
    (adata / f"{label}.json").write_text(json.dumps(
        {"start": args.start, "end": args.end, "stats": stats, "items": picked}, ensure_ascii=False, indent=1))
    render_archive(label, args.start, args.end, picked, stats)
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
