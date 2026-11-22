"""Two-stage screening with the Claude API.

Stage 1 (cheap model): grade every abstract core / high / low, in batches.
Stage 2 (stronger model): for core/high only, confirm the grade and write the
Chinese takeaway and reason shown in the list.
"""
from __future__ import annotations

import json

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
                        "grade": {"type": "string", "enum": ["core", "high", "low"]},
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
    abstract = p["abstract"][:2500]
    return f"<paper id=\"{pid}\">\nTitle: {p['title']}\nSource: {p['source']}\nAbstract: {abstract}\n</paper>"


def stage1(papers: list[dict], profile: str, model: str, batch: int = 20) -> dict[str, str]:
    grades: dict[str, str] = {}
    for i in range(0, len(papers), batch):
        chunk = papers[i:i + batch]
        body = "\n\n".join(_paper_block(str(i + j), p) for j, p in enumerate(chunk))
        try:
            out = _call(model, 2000, profile, GRADE_TOOL,
                        "按上面的标准给每篇论文定级（core / high / low）。大多数应为 low。\n\n" + body)
        except anthropic.APIError as e:
            print(f"stage1 batch {i} failed: {e}")
            continue
        for g in (out or {}).get("grades", []):
            grades[str(g["id"])] = g["grade"]
    return grades


def stage2(paper: dict, profile: str, model: str) -> dict:
    out = _call(model, 600, profile, WRITE_TOOL,
                "这篇论文初筛被标为 core 或 high。请按标准复核等级（不够格就改成 low），"
                "然后用中文写“最重要的结论”和“相关理由或下一步”。只依据摘要，不要编造摘要里没有的数字。\n\n"
                + _paper_block("0", paper))
    return out or {"grade": "low", "takeaway": "", "reason": ""}


def screen(papers: list[dict], profile: str, models: dict) -> list[dict]:
    if not papers:
        return []
    g = stage1(papers, profile, models["screen"])
    picked = [p for i, p in enumerate(papers) if g.get(str(i)) in ("core", "high")]
    out = []
    print(f"stage1: {len(papers)} screened, {len(picked)} core/high")
    for p in picked:
        try:
            r = stage2(p, profile, models["write"])
        except anthropic.APIError as e:
            print(f"stage2 failed for {p.get('doi')}: {e}")
            continue
        if r.get("grade") in ("core", "high"):
            out.append({**p, **r})
    return out


if __name__ == "__main__":  # quick manual check: python src/screen.py paper.json
    import sys
    from pathlib import Path
    prof = Path("config/profile.md").read_text()
    print(json.dumps(screen(json.load(open(sys.argv[1])), prof,
                            {"screen": "claude-haiku-5-5", "write": "claude-sonnet-5-5"}),
                     ensure_ascii=False, indent=2))
