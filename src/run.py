"""Daily entry point: fetch -> dedupe -> screen -> update state -> render README."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

DATA = ROOT / "data"
TZ = ZoneInfo("Europe/Helsinki")
SCREENED = DATA / "screened.tsv"   # keys already graded (kept a few days, avoids paying twice)
ITEMS = DATA / "items.json"        # selected papers (kept 60 days for version dedupe)
RUNS = DATA / "runs.json"          # per-day counts


def norm_title(t: str) -> str:
    return "t:" + re.sub(r"[^a-z0-9]", "", t.lower())[:120]


def keys_of(p: dict) -> set[str]:
    ks = {norm_title(p["title"])}
    if p.get("doi"):
        ks.add(p["doi"])
    if p.get("published_doi"):
        ks.add(p["published_doi"])
    return ks


def h(k: str) -> str:
    return hashlib.sha1(k.encode()).hexdigest()[:16]


def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def load_screened(today: date) -> dict[str, str]:
    out = {}
    if SCREENED.exists():
        for line in SCREENED.read_text().splitlines():
            k, d = line.split("\t")
            if date.fromisoformat(d) >= today - timedelta(days=6):
                out[k] = d
    return out


def dedupe(cands: list[dict]) -> list[dict]:
    """Merge duplicates within one run; journal version wins over its preprint."""
    by_key: dict[str, dict] = {}
    result: list[dict] = []
    # journal articles first, so their keys claim the slot
    for p in sorted(cands, key=lambda x: x["source"] in ("bioRxiv", "medRxiv")):
        hit = next((by_key[k] for k in keys_of(p) if k in by_key), None)
        if hit is None:
            result.append(p)
            for k in keys_of(p):
                by_key[k] = p
        elif p["source"] in ("bioRxiv", "medRxiv") and hit["source"] not in ("bioRxiv", "medRxiv"):
            hit["preprint_url"] = p["url"]
            for k in keys_of(p):
                by_key[k] = hit
    return result


def should_run(runs: list[dict], now: datetime, scheduled: bool) -> bool:
    if not scheduled:
        return True
    if now.hour < 12:
        return False
    return not any(r["date"] == now.date().isoformat() for r in runs)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scheduled", action="store_true", help="cron mode: run once per day after 12:00 Helsinki")
    ap.add_argument("--render-only", action="store_true")
    ap.add_argument("--window-days", type=int, default=None)
    args = ap.parse_args()

    from render import render  # local import keeps --render-only light

    cfg = yaml.safe_load((ROOT / "config/sources.yaml").read_text())
    now = datetime.now(TZ)
    today = now.date()
    runs = load_json(RUNS, [])
    items = load_json(ITEMS, [])

    if args.render_only:
        render(items, runs, cfg, now)
        return
    if not should_run(runs, now, args.scheduled):
        print("Nothing to do (before 12:00 Helsinki or already ran today).")
        return
    if args.window_days:
        cfg["window_days"] = args.window_days

    from fetch import fetch_all
    from screen import screen

    cands, stats = fetch_all(cfg, today)
    cands = dedupe(cands)
    screened = load_screened(today)
    reported = {k for it in items for k in keys_of(it)}

    fresh = [p for p in cands
             if not ({h(k) for k in keys_of(p)} & screened.keys()) and not (keys_of(p) & reported)]
    # a newly indexed journal version of a paper already listed as a preprint: annotate, don't relist
    for p in cands:
        if p["source"] not in ("bioRxiv", "medRxiv") and keys_of(p) & reported:
            for it in items:
                if keys_of(it) & keys_of(p) and it["source"] in ("bioRxiv", "medRxiv") and not it.get("published_url"):
                    it["published_url"] = p["url"]
                    it["published_in"] = p["source"]
    fresh = fresh[: cfg["max_candidates"]]

    profile = (ROOT / "config/profile.md").read_text()
    picked = screen(fresh, profile, cfg["models"])

    for p in picked:
        p["run_date"] = today.isoformat()
        p.pop("abstract", None)
        p.pop("pub_types", None)
    items = [it for it in items if date.fromisoformat(it["run_date"]) >= today - timedelta(days=60)] + picked
    for p in fresh:
        for k in keys_of(p):
            screened[h(k)] = today.isoformat()

    prev = next((r for r in runs if r["date"] == today.isoformat()), None)
    runs = [r for r in runs if r["date"] != today.isoformat()]
    runs.append({"date": today.isoformat(), "candidates": len(cands),
                 "screened": len(fresh) + (prev["screened"] if prev else 0),
                 "picked": len(picked) + (prev["picked"] if prev else 0), "by_source": stats})
    runs = sorted(runs, key=lambda r: r["date"])[-cfg["keep_days"]:]

    DATA.mkdir(exist_ok=True)
    ITEMS.write_text(json.dumps(items, ensure_ascii=False, indent=1))
    RUNS.write_text(json.dumps(runs, ensure_ascii=False, indent=1))
    SCREENED.write_text("".join(f"{k}\t{d}\n" for k, d in sorted(screened.items())))
    render(items, runs, cfg, now)
    print(json.dumps(runs[-1], ensure_ascii=False))


if __name__ == "__main__":
    main()
