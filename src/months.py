import json, sys
from datetime import date

def expand(spec: str) -> list[str]:
    out = []
    for part in spec.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if ".." in part:
            a, b = (x.strip() for x in part.split("..", 1))
            y, m = (int(v) for v in a.split("-"))
            ey, em = (int(v) for v in b.split("-"))
            while (y, m) <= (ey, em):
                out.append(f"{y:04d}-{m:02d}")
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        else:
            date.fromisoformat(part + "-01")
            out.append(part)
    if not out:
        raise SystemExit("no months given")
    return out

print(json.dumps(expand(sys.argv[1])))
