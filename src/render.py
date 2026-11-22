"""Write README.md: literature (last N days) + research-social + events."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRADE = {"core": "**Core**", "high": "High"}
WEEKDAY = "一二三四五六日"


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
    if fa:
        s += f"<br><sub>{cell(fa)}</sub>"
    return s


def source_cell(it: dict) -> str:
    s = cell(it["source"])
    if it.get("preprint_url"):
        s += f"<br><sub>[预印本]({it['preprint_url']})</sub>"
    if it.get("published_url"):
        s += f"<br><sub>[已发表于 {cell(it.get('published_in', '期刊'))}]({it['published_url']})</sub>"
    return s


def section_file(name: str, empty: str) -> str:
    p = ROOT / "data" / name
    text = p.read_text().strip() if p.exists() else ""
    return text or empty


def render(items: list[dict], runs: list[dict], cfg: dict, now: datetime) -> None:
    keep = cfg["keep_days"]
    today = now.date()
    days = [today - timedelta(days=i) for i in range(keep)]
    run_by_day = {r["date"]: r for r in runs}

    out = [
        "# trawl",
        "",
        f"<sub>更新于 {now:%Y-%m-%d %H:%M}（赫尔辛基时间）· 文献每天中午更新，显示最近 {keep} 天 · "
        "科研社交与活动每周更新两次 · 点标题直达原文</sub>",
        "",
        "**目录：** [📅 活动](#-活动) · [📄 文献](#-文献) · [💬 科研社交](#-科研社交)",
        "",
        "## 📅 活动",
        "",
        section_file("events.md", "_暂无。_"),
        "",
        "## 📄 文献",
        "",
    ]
    for d in days:
        iso = d.isoformat()
        day_items = [it for it in items if it.get("run_date") == iso]
        run = run_by_day.get(iso)
        if not run and not day_items:
            continue
        head = f"### {iso}（周{WEEKDAY[d.weekday()]}）"
        if run:
            head += f" · 候选 {run['candidates']} 篇，新筛 {run['screened']} 篇，入选 {run['picked']} 篇"
        out += [head, ""]
        if not day_items:
            out += ["_今日无符合条件的文献。_", ""]
            continue
        day_items.sort(key=lambda it: (it.get("grade") != "core", it.get("source", "")))
        out += ["| 等级 | 论文 | 来源 | 最重要的结论 | 相关理由或下一步 |",
                "|---|---|---|---|---|"]
        for it in day_items:
            out.append(f"| {GRADE.get(it.get('grade'), '')} | {paper_cell(it)} | {source_cell(it)} "
                       f"| {cell(it.get('takeaway', ''))} | {cell(it.get('reason', ''))} |")
        out.append("")

    out += ["## 💬 科研社交", "", section_file("social.md", "_暂无。_"), ""]
    (ROOT / "README.md").write_text("\n".join(out))
