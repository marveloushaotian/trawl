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


def archive_items() -> list[dict]:
    """Every paper ever selected, so nothing is listed twice."""
    f = DATA / "archive" / "picks.json"
    return json.loads(f.read_text()) if f.exists() else []


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
    return hashlib.sha1(k.encode()).hexdigest()[:12]


def primary(p: dict) -> str:
    return p.get("doi") or norm_title(p["title"])


def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def load_screened(today: date, keep_days: int) -> dict[str, str]:
    """key-hash -> 'YYYY-MM-DD' (graded with abstract) or 'YYYY-MM-DD:t' (title only)."""
    out = {}
    if SCREENED.exists():
        for line in SCREENED.read_text().splitlines():
            k, v = line.split("\t")
            if date.fromisoformat(v[:10]) >= today - timedelta(days=keep_days):
                out[k] = v
    return out


def is_preprint(p: dict) -> bool:
    return p["source"] in ("bioRxiv", "medRxiv")


def dedupe(cands: list[dict]) -> list[dict]:
    """Merge duplicates within one run; journal version wins over its preprint."""
    by_key: dict[str, dict] = {}
    result: list[dict] = []
    # journal articles first, so their keys claim the slot
    for p in sorted(cands, key=lambda x: (is_preprint(x), not x.get("abstract"))):
        hit = next((by_key[k] for k in keys_of(p) if k in by_key), None)
        if hit is None:
            result.append(p)
            for k in keys_of(p):
                by_key[k] = p
            continue
        if is_preprint(p) and not is_preprint(hit):
            hit["preprint_url"] = p["url"]
        if not hit.get("abstract") and p.get("abstract"):
            hit["abstract"] = p["abstract"]
        if not hit.get("date") and p.get("date"):
            hit["date"] = p["date"]
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
    ap.add_argument("--reset-today", action="store_true",
                    help="forget today's results and screening, then run again (for testing)")
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
    if args.reset_today:
        iso = today.isoformat()
        items = [it for it in items if it.get("run_date") != iso]
        runs = [r for r in runs if r["date"] != iso]

    from fetch import fetch_all
    from screen import screen

    cands, stats = fetch_all(cfg, today)
    cands = dedupe(cands)
    # drop late-indexed papers published long ago
    cutoff = today - timedelta(days=cfg["max_age_days"])
    cands = [p for p in cands if not p.get("date") or date.fromisoformat(p["date"][:10]) >= cutoff]

    screened = load_screened(today, cfg["max_age_days"])
    if args.reset_today:
        screened = {k: v for k, v in screened.items() if not v.startswith(today.isoformat())}
    reported = {k for it in items + archive_items() for k in keys_of(it)}

    def needs_screen(p: dict) -> bool:
        if keys_of(p) & reported:
            return False
        prev = screened.get(h(primary(p)))
        if not prev:
            return True
        # graded earlier on the title alone; grade again once the abstract is available
        return bool(p.get("abstract")) and prev.endswith(":t")

    fresh = [p for p in cands if needs_screen(p)]
    # a newly indexed journal version of a paper already listed as a preprint: annotate, don't relist
    for p in cands:
        if not is_preprint(p) and keys_of(p) & reported:
            for it in items:
                if keys_of(it) & keys_of(p) and is_preprint(it) and not it.get("published_url"):
                    it["published_url"] = p["url"]
                    it["published_in"] = p["source"]
    fresh = fresh[: cfg["max_candidates"]]

    profile = (ROOT / "config/profile.md").read_text()
    picked = screen(fresh, profile, cfg["models"])

    # audit trail of the latest run: every screened paper with both grades (overwritten each run)
    AUDIT = DATA / "last_run.tsv"
    audit_rows = [[p.get("stage1", ""), p.get("final", ""), p["source"], (p.get("date") or "")[:10],
                   p["title"].replace("\t", " ")[:200], p["url"]]
                  for p in sorted(fresh, key=lambda x: (x.get("stage1", ""), x.get("final", "")))]
    DATA.mkdir(exist_ok=True)
    AUDIT.write_text("\n".join(["stage1\tfinal\tsource\tdate\ttitle\turl"]
                                + ["\t".join(r) for r in audit_rows]) + "\n")

    for p in picked:
        p["run_date"] = today.isoformat()
        p.pop("stage1", None)
        p.pop("final", None)
        p.pop("abstract", None)
        p.pop("pub_types", None)
    items = [it for it in items if date.fromisoformat(it["run_date"]) >= today - timedelta(days=60)] + picked
    for p in fresh:
        screened[h(primary(p))] = today.isoformat() + ("" if p.get("abstract") else ":t")

    prev = next((r for r in runs if r["date"] == today.isoformat()), None)
    stats["after_dedupe"] = len(cands)
    runs = [r for r in runs if r["date"] != today.isoformat()]
    runs.append({"date": today.isoformat(), "candidates": len(cands),
                 "screened": len(fresh) + (prev["screened"] if prev else 0),
                 "picked": len(picked) + (prev["picked"] if prev else 0), "by_source": stats})
    runs = sorted(runs, key=lambda r: r["date"])[-cfg["keep_days"]:]

    import archive as arch
    arch.add_picks([dict(p) for p in picked])
    arch.add_screened(audit_rows)
    arch.add_coverage(today.isoformat(), today.isoformat())
    arch.rebuild(today)

    DATA.mkdir(exist_ok=True)
    ITEMS.write_text(json.dumps(items, ensure_ascii=False, indent=1))
    RUNS.write_text(json.dumps(runs, ensure_ascii=False, indent=1))
    SCREENED.write_text("".join(f"{k}\t{d}\n" for k, d in sorted(screened.items())))
    render(items, runs, cfg, now)
    print(json.dumps(runs[-1], ensure_ascii=False))


if __name__ == "__main__":
    main()
