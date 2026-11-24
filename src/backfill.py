"""Screen a past calendar month (or an explicit range) and fold it into the monthly archive.

Usage: python src/backfill.py --month 2026-09
       python src/backfill.py --start 2026-09-01 --end 2026-09-21

Fetches in one-week chunks (keeps every API query small), uses publication date for
PubMed, skips papers already screened or listed, and only screens the days that no
earlier run has covered, so re-running a month costs nothing.
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

import archive as arch  # noqa: E402
from fetch import fetch_all  # noqa: E402
from run import (DATA, SCREENED, archive_items, dedupe, h, keys_of, load_json,  # noqa: E402
                 norm_title, primary)
from screen import screen  # noqa: E402


def screen_range(cfg: dict, profile: str, start: date, end: date, workers: int) -> tuple[list[dict], list[list[str]], dict]:
    cands, stats = [], {}
    d = start
    while d <= end:
        chunk_end = min(d + timedelta(days=6), end)
        c, s = fetch_all(cfg, chunk_end, start=d, pubmed_datetype="pdat")
        print(f"{d}..{chunk_end}: {s}", flush=True)
        cands += c
        for k, v in s.items():
            stats[k] = stats.get(k, 0) + v
        d = chunk_end + timedelta(days=1)

    cands = dedupe(cands)
    # keep papers published inside the period (or undated)
    cands = [p for p in cands if not p.get("date") or start.isoformat() <= p["date"][:10] <= end.isoformat()]

    screened = {}
    if SCREENED.exists():
        for line in SCREENED.read_text().splitlines():
            k, v = line.split("\t")
            screened[k] = v
    reported = {k for it in load_json(DATA / "items.json", []) + archive_items() for k in keys_of(it)}
    seen = arch.screened_index()          # permanent: every paper graded in any earlier run
    fresh = [p for p in cands
             if not (keys_of(p) & reported)
             and not screened.get(h(primary(p)), "").replace(":t", "")
             and p["url"].strip() not in seen and norm_title(p["title"]) not in seen]
    print(f"candidates {len(cands)}, to screen {len(fresh)}", flush=True)

    picked = screen(fresh, profile, cfg["models"], workers=workers)
    rows = [[p.get("stage1", ""), p.get("final", ""), p["source"], (p.get("date") or "")[:10],
             p["title"].replace("\t", " ")[:200], p["url"]]
            for p in sorted(fresh, key=lambda x: (x.get("stage1", ""), x.get("final", ""), x.get("date") or ""))]
    for p in picked:
        for k in ("abstract", "pub_types", "stage1", "final"):
            p.pop(k, None)
    stats.update({"candidates": len(cands), "screened": len(fresh), "picked": len(picked)})
    return picked, rows, stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", help="calendar month, e.g. 2026-09")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--replace", action="store_true", help="screen the whole period again, ignoring coverage")
    args = ap.parse_args()

    if args.month:
        start, end = arch.month_bounds(args.month)
        end = min(end, date.today())
    elif args.start and args.end:
        start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    else:
        ap.error("give --month YYYY-MM, or both --start and --end")

    todo = [(start, end)] if args.replace else arch.gaps(start, end)
    if not todo:
        print(f"{start} to {end} is already screened; rebuilding the archive only.")
        arch.rebuild()
        return
    print(f"screening {', '.join(f'{a} to {b}' for a, b in todo)}", flush=True)

    cfg = yaml.safe_load((ROOT / "config/sources.yaml").read_text())
    profile = (ROOT / "config/profile.md").read_text()
    total = {}
    for a, b in todo:
        picked, rows, stats = screen_range(cfg, profile, a, b, args.workers)
        arch.add_picks(picked)
        arch.add_screened(rows)
        arch.add_coverage(a.isoformat(), b.isoformat())
        for k, v in stats.items():
            total[k] = total.get(k, 0) + v
    months = arch.rebuild()
    print(json.dumps({"range": [start.isoformat(), end.isoformat()], "months": months, **total}))


if __name__ == "__main__":
    main()
