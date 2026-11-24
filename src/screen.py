"""Two-stage screening with the Claude API.

Stage 1 (cheap model): grade every abstract core / high / low, in batches.
Stage 2 (stronger model): for core/high only, confirm the grade and write the
Chinese takeaway and reason shown in the list.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import anthropic

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

GRADE_TOOL = {
    "name": "record_grades",
    "description": "Record one grade per paper.",
    "input_schema": {
        "type": "object",
        "properties": {
            "grades": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "grade": {"type": "string", "enum": ["core", "high", "maybe", "low"]},
                    },
                    "required": ["id", "grade"],
                },
            }
        },
        "required": ["grades"],
    },
}

WRITE_TOOL = {
    "name": "record_entry",
    "description": "Record the final grade and the Chinese text for one paper.",
    "input_schema": {
        "type": "object",
        "properties": {
            "grade": {"type": "string", "enum": ["core", "high", "low"]},
            "takeaway": {"type": "string", "description": "最重要的结论，中文，一句话，40 字以内，保留英文术语"},
            "reason": {"type": "string", "description": "与他研究的关系或下一步，中文，一句话，40 字以内"},
        },
        "required": ["grade", "takeaway", "reason"],
    },
}


def _call(model: str, max_tokens: int, profile: str, tool: dict, prompt: str) -> dict | None:
    """Ask for one tool call. Some models reject a forced tool_choice; fall back to auto."""
    kwargs = dict(model=model, max_tokens=max_tokens, system=_system(profile), tools=[tool],
                  messages=[{"role": "user", "content": prompt}])
    try:
        msg = client.messages.create(tool_choice={"type": "tool", "name": tool["name"]}, **kwargs)
    except anthropic.BadRequestError as e:
        if "tool_choice" not in str(e):
            raise
        kwargs["messages"][0]["content"] = prompt + f"\n\n请只通过调用 {tool['name']} 工具作答。"
        msg = client.messages.create(tool_choice={"type": "auto"}, **kwargs)
    for block in msg.content:
        if block.type == "tool_use":
            return dict(block.input)
    return None


def _system(profile: str) -> list[dict]:
    return [{"type": "text", "text": profile, "cache_control": {"type": "ephemeral"}}]


def _paper_block(pid: str, p: dict) -> str:
    abstract = (p.get("abstract") or "")[:2500] or "（暂无摘要，只能根据标题判断）"
    return f"<paper id=\"{pid}\">\nTitle: {p['title']}\nSource: {p['source']}\nAbstract: {abstract}\n</paper>"


def _parse_grades(out: dict | None) -> dict[str, str]:
    """The model usually returns [{"id": .., "grade": ..}], but not always. Ignore anything else."""
    grades = (out or {}).get("grades")
    if isinstance(grades, dict):                    # {"0": "low", ...}
        return {str(k): v for k, v in grades.items() if isinstance(v, str)}
    if not isinstance(grades, list):
        print(f"stage1: unusable grades payload {type(grades).__name__}")
        return {}
    out_map: dict[str, str] = {}
    for g in grades:
        if isinstance(g, dict) and "id" in g and isinstance(g.get("grade"), str):
            out_map[str(g["id"])] = g["grade"]
        else:
            print(f"stage1: skipping malformed grade entry {g!r:.80}")
    return out_map


def stage1(papers: list[dict], profile: str, model: str, batch: int = 20, workers: int = 1) -> dict[str, str]:
    def one(i: int) -> dict[str, str]:
        chunk = papers[i:i + batch]
        body = "\n\n".join(_paper_block(str(i + j), p) for j, p in enumerate(chunk))
        try:
            out = _call(model, 2000, profile, GRADE_TOOL,
                        "按上面的标准给每篇论文定级：core / high / maybe / low。大多数应为 low。"
                        "拿不准、但有可能是 core 或 high 的标 maybe（会交给更强的模型复核）；"
                        "只有标题、没有摘要时，看起来相关就标 maybe。\n\n" + body)
        except Exception as e:                      # one bad batch must not kill the month
            print(f"stage1 batch {i} failed: {type(e).__name__}: {e}")
            return {}
        return _parse_grades(out)

    grades: dict[str, str] = {}
    starts = list(range(0, len(papers), batch))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for g in ex.map(one, starts):
            grades.update(g)
    return grades


def stage2(paper: dict, profile: str, model: str) -> dict:
    out = _call(model, 600, profile, WRITE_TOOL,
                "这篇论文通过了初筛。请严格按标准定最终等级（不够格就标 low，宁缺毋滥），"
                "然后用中文写“最重要的结论”和“相关理由或下一步”。只依据摘要，不要编造摘要里没有的数字；"
                "没有摘要时，结论写“暂无摘要，按标题判断：……”。\n\n"
                + _paper_block("0", paper))
    return out or {"grade": "low", "takeaway": "", "reason": ""}


def screen(papers: list[dict], profile: str, models: dict, workers: int = 1) -> list[dict]:
    if not papers:
        return []
    g = stage1(papers, profile, models["screen"], workers=workers)
    for i, p in enumerate(papers):
        p["stage1"] = g.get(str(i), "error")
    errors = sum(p["stage1"] == "error" for p in papers)
    if errors > max(20, len(papers) // 5):
        # a broad outage: stop instead of sending everything to the expensive model
        raise RuntimeError(f"stage1 failed for {errors} of {len(papers)} papers")
    if errors:
        print(f"stage1: {errors} ungraded, sending them to stage 2 rather than dropping them")
    picked = [p for p in papers if p["stage1"] in ("core", "high", "maybe", "error")]
    print(f"stage1: {len(papers)} screened, {len(picked)} passed to stage 2")

    def two(p: dict) -> dict | None:
        try:
            r = stage2(p, profile, models["write"])
        except Exception as e:
            print(f"stage2 failed for {p.get('doi')}: {type(e).__name__}: {e}")
            return None
        p["final"] = r.get("grade", "low")
        return {**p, **r} if p["final"] in ("core", "high") else None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return [r for r in ex.map(two, picked) if r]


if __name__ == "__main__":  # quick manual check: python src/screen.py paper.json
    import sys
    from pathlib import Path
    prof = Path("config/profile.md").read_text()
    print(json.dumps(screen(json.load(open(sys.argv[1])), prof,
                            {"screen": "claude-haiku-5-5", "write": "claude-sonnet-5-5"}),
                     ensure_ascii=False, indent=2))
