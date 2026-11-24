"""Monthly archive: one file per calendar month, rebuilt from the permanent pick store.

data/archive/picks.json      every selected paper ever (backfill + daily), deduped
data/archive/coverage.json   date ranges whose publication dates have been screened
data/archive/screened/*.tsv  full audit per month (stage1 + final grade for everything)
archive/YYYY-MM.md           rendered month, grouped by publication date
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

ADATA = ROOT / "data" / "archive"
ADIR = ROOT / "archive"
MONTHS_DIR = ADATA / "months"      # one file per month: the only thing a run writes
SCREENED_DIR = ADATA / "screened"  # one audit file per month
AUDIT_HEAD = "stage1\tfinal\tsource\tdate\ttitle\turl"


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


# ------------------------------------------------- per-month store

def month_of(it: dict) -> str:
    return (it.get("date") or "unknown")[:7]


def load_month(month: str) -> dict:
    f = MONTHS_DIR / f"{month}.json"
    return json.loads(f.read_text()) if f.exists() else {"month": month, "covered": [], "items": []}


def save_month(d: dict) -> None:
    MONTHS_DIR.mkdir(parents=True, exist_ok=True)
    d["items"].sort(key=lambda x: ((x.get("date") or "")[:10], x["title"]))
    (MONTHS_DIR / f"{d['month']}.json").write_text(json.dumps(d, ensure_ascii=False, indent=1))


def months_on_disk() -> list[str]:
    return sorted(f.stem for f in MONTHS_DIR.glob("*.json")) if MONTHS_DIR.exists() else []


def load_picks() -> list[dict]:
    """Every paper ever selected, from all month files."""
    return [it for m in months_on_disk() for it in load_month(m)["items"]]


def add_picks(new: list[dict]) -> None:
    """File selected papers into their publication month. Only touches those months."""
    from run import keys_of

    by_month: dict[str, list[dict]] = {}
    for p in new:
        by_month.setdefault(month_of(p), []).append(p)
    for month, papers in by_month.items():
        d = load_month(month)
        index: dict[str, dict] = {k: it for it in d["items"] for k in keys_of(it)}
        for p in papers:
            hit = next((index[k] for k in keys_of(p) if k in index), None)
            if hit is None:
                d["items"].append(p)
                for k in keys_of(p):
                    index[k] = p
            else:
                hit.update({k: v for k, v in p.items() if v})
        save_month(d)


# ---------------------------------------------------------------- coverage

def _merge(ranges: list[list[str]]) -> list[list[str]]:
    out: list[list[str]] = []
    for s, e in sorted(ranges):
        if out and _d(s) <= _d(out[-1][1]) + timedelta(days=1):
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def load_coverage() -> list[list[str]]:
    return _merge([r for m in months_on_disk() for r in load_month(m)["covered"]])


def add_coverage(start: str, end: str) -> None:
    """Record a screened range, split across the month files it spans."""
    a, b = _d(start), _d(end)
    while a <= b:
        month = a.strftime("%Y-%m")
        m_end = min(month_bounds(month)[1], b)
        d = load_month(month)
        d["covered"] = _merge(d["covered"] + [[a.isoformat(), m_end.isoformat()]])
        save_month(d)
        a = m_end + timedelta(days=1)


def gaps(start: date, end: date) -> list[tuple[date, date]]:
    """Sub-ranges of [start, end] that no run has screened yet."""
    out, cur = [], start
    for s, e in load_coverage():
        s, e = _d(s), _d(e)
        if e < cur or s > end:
            continue
        if s > cur:
            out.append((cur, min(s - timedelta(days=1), end)))
        cur = max(cur, e + timedelta(days=1))
        if cur > end:
            break
    if cur <= end:
        out.append((cur, end))
    return out


def covered(start: date, end: date) -> bool:
    return not gaps(start, end)


# ---------------------------------------------------------------- audit

def add_screened(rows: list[list[str]]) -> None:
    """Append audit rows (one per screened paper) to their publication month's file."""
    SCREENED_DIR.mkdir(parents=True, exist_ok=True)
    by_month: dict[str, list[str]] = {}
    for r in rows:
        by_month.setdefault((r[3] or "unknown")[:7], []).append("\t".join(r))
    for month, new in by_month.items():
        f = SCREENED_DIR / f"{month}.tsv"
        old = [l for l in f.read_text().splitlines()[1:] if l] if f.exists() else []
        seen, keep = set(), []
        for line in old + new:                      # later rows win (re-screened)
            url = line.rsplit("\t", 1)[-1]
            if url in seen:
                continue
            seen.add(url)
            keep.append(line)
        f.write_text("\n".join([AUDIT_HEAD] + sorted(keep)) + "\n")


def screened_index() -> set[str]:
    """Keys of every paper ever screened, from the permanent per-month audit.

    data/screened.tsv only keeps 60 days, so this is what stops an old month
    from being graded (and paid for) a second time.
    """
    from run import norm_title

    keys: set[str] = set()
    for f in sorted(SCREENED_DIR.glob("*.tsv")):
        for line in f.read_text().splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) < 6:
                continue
            keys.add(parts[5].strip())
            keys.add(norm_title(parts[4]))
    return keys


def screened_count(month: str) -> int:
    f = SCREENED_DIR / f"{month}.tsv"
    return max(len(f.read_text().splitlines()) - 1, 0) if f.exists() else 0


# ---------------------------------------------------------------- render

def month_bounds(month: str) -> tuple[date, date]:
    first = date.fromisoformat(month + "-01")
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt - timedelta(days=1)


def rebuild(today: date | None = None) -> list[str]:
    """Rewrite every month file and the index from picks.json."""
    from render import TABLE_HEAD, in_zotero, table_rows

    today = today or date.today()
    ADIR.mkdir(exist_ok=True)
    by_month = {m: load_month(m)["items"] for m in months_on_disk()}

    written = []
    for month in sorted(by_month, reverse=True):
        items = by_month[month]
        n_core = sum(it.get("grade") == "core" for it in items)
        n_zot = sum(in_zotero(it) for it in items)
        if month == "unknown":
            label, note = "undated papers", ""
        else:
            start, end = month_bounds(month)
            end = min(end, today)
            label = start.strftime("%B %Y")
            missing = gaps(start, end)
            note = (" Fully screened." if not missing
                    else " Partly screened, missing " + ", ".join(f"{a} to {b}" for a, b in missing) + ".")
        out = [f"# trawl archive: {label}", "",
               f"Screened {screened_count(month)} · selected {len(items)} "
               f"({n_core} Core, {len(items) - n_core} High) · {n_zot} already in Zotero."
               f"{note} Grouped by publication date, newest first.", ""]
        by_day: dict[str, list[dict]] = {}
        for it in items:
            by_day.setdefault((it.get("date") or "unknown")[:10], []).append(it)
        for day in sorted(by_day, reverse=True):
            out += [f"## {day}", ""] + TABLE_HEAD + table_rows(by_day[day]) + [""]
        (ADIR / f"{month}.md").write_text("\n".join(out))
        written.append(month)

    rows = []
    for month in written:
        items = by_month[month]
        if month == "unknown":
            status = "—"
        else:
            start, end = month_bounds(month)
            status = "complete" if covered(start, min(end, today)) else "partial"
        rows.append(f"| [{month}]({month}.md) | {screened_count(month)} | {len(items)} "
                    f"| {sum(i.get('grade') == 'core' for i in items)} | {status} |")
    idx = ["# trawl archive", "",
           "Every selected paper, one file per calendar month, by publication date. "
           "The daily feed in the top-level README is the rolling view of the last two weeks; "
           "this is the permanent record.", "",
           "| Month | Screened | Selected | Core | Coverage |", "|---|---|---|---|---|"] + rows + [""]
    (ADIR / "README.md").write_text("\n".join(idx))
    return written


if __name__ == "__main__":                 # `python src/archive.py --rebuild`
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    ap.parse_args()
    print("rebuilt:", ", ".join(rebuild()))
