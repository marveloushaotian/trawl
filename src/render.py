"""Write README.md: the literature list for the last N days."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRADE = {"core": "**Core**", "high": "High"}


def cell(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


def first_author(authors: str) -> str:
    if not authors:
        return ""
    sep = ";" if ";" in authors else ","
    names = [a.strip() for a in authors.split(sep) if a.strip()]
    if not names:
        return ""
    return names[0] + (" et al." if len(names) > 1 else "")


def paper_cell(it: dict) -> str:
    s = f"[{cell(it['title'])}]({it['url']})"
    fa = first_author(it.get("authors", ""))
    return f"{s} — {cell(fa)}" if fa else s


def source_cell(it: dict) -> str:
    s = cell(it["source"])
    if it.get("preprint_url"):
        s += f" · [preprint]({it['preprint_url']})"
    if it.get("published_url"):
        s += f" · [now in {cell(it.get('published_in', 'journal'))}]({it['published_url']})"
    return s


def render(items: list[dict], runs: list[dict], cfg: dict, now: datetime) -> None:
    keep = cfg["keep_days"]
    today = now.date()
    run_by_day = {r["date"]: r for r in runs}

    out = [
        "# trawl",
        "",
        "New papers on plasmids, phages and mobile genetic elements, screened daily around 12:00 Helsinki time. "
        f"Last {keep} days, newest first. Updated {now:%Y-%m-%d %H:%M}.",
        "",
    ]
    for i in range(keep):
        d = today - timedelta(days=i)
        iso = d.isoformat()
        day_items = [it for it in items if it.get("run_date") == iso]
        run = run_by_day.get(iso)
        if not run and not day_items:
            continue
        out += [f"## {iso} ({d:%a})", ""]
        if run:
            out += [f"Screened {run['screened']} · selected {run['picked']}", ""]
        if not day_items:
            out += ["No matching papers.", ""]
            continue
        day_items.sort(key=lambda it: (it.get("grade") != "core", it.get("source", "")))
        out += ["| Grade | Paper | Source | Published | 最重要的结论 | 相关理由 |",
                "|---|---|---|---|---|---|"]
        for it in day_items:
            out.append(f"| {GRADE.get(it.get('grade'), '')} | {paper_cell(it)} | {source_cell(it)} "
                       f"| {cell((it.get('date') or '')[:10])} "
                       f"| {cell(it.get('takeaway', ''))} | {cell(it.get('reason', ''))} |")
        out.append("")

    (ROOT / "README.md").write_text("\n".join(out))
