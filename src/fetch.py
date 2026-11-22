"""Fetch candidate papers from bioRxiv/medRxiv and PubMed."""
from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import requests

UA = {"User-Agent": "trawl/1.0 (personal literature feed)"}
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def _get(url: str, params: dict | None = None, tries: int = 4) -> requests.Response:
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=60)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 * (i + 1))
                continue
            r.raise_for_status()
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed: {url}")


def _ncbi(params: dict) -> dict:
    params = dict(params, tool="trawl")
    if os.environ.get("NCBI_API_KEY"):
        params["api_key"] = os.environ["NCBI_API_KEY"]
    return params


# ---------------------------------------------------------------- keywords

def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9\-]+", text.lower()))


def keyword_hit(text: str, keyword_sets: dict) -> bool:
    low = text.lower()
    words = _words(text)

    def has(term: str) -> bool:
        term = term.lower()
        return term in low if (" " in term or "-" in term) else term in words

    for spec in keyword_sets.values():
        if "any" in spec and any(has(t) for t in spec["any"]):
            return True
        if "pairs" in spec and all(any(has(t) for t in group) for group in spec["pairs"]):
            return True
    return False


def pubmed_keyword_query(keyword_sets: dict) -> str:
    def q(t: str) -> str:
        return f'"{t}"[tiab]'

    parts = []
    for spec in keyword_sets.values():
        if "any" in spec:
            parts.append("(" + " OR ".join(q(t) for t in spec["any"]) + ")")
        if "pairs" in spec:
            groups = ["(" + " OR ".join(q(t) for t in g) + ")" for g in spec["pairs"]]
            parts.append("(" + " AND ".join(groups) + ")")
    return " OR ".join(parts)


# ---------------------------------------------------------------- bioRxiv

def fetch_rxiv(server: str, start: date, end: date) -> list[dict]:
    out, cursor = [], 0
    while True:
        url = f"https://api.biorxiv.org/details/{server}/{start}/{end}/{cursor}"
        data = _get(url).json()
        coll = data.get("collection", [])
        for c in coll:
            pub = (c.get("published") or "").strip()
            out.append({
                "source": "bioRxiv" if server == "biorxiv" else "medRxiv",
                "doi": (c.get("doi") or "").lower(),
                "title": (c.get("title") or "").strip(),
                "authors": (c.get("authors") or "").strip(),
                "abstract": (c.get("abstract") or "").strip(),
                "date": c.get("date", ""),
                "category": (c.get("category") or "").strip().lower(),
                "published_doi": "" if pub in ("", "NA") else pub.lower(),
                "url": f"https://www.{server}.org/content/{c.get('doi')}",
            })
        msg = (data.get("messages") or [{}])[0]
        total = int(msg.get("total", 0) or 0)
        cursor += len(coll)
        if not coll or cursor >= total:
            break
        time.sleep(0.5)
    latest: dict[str, dict] = {}
    for p in out:  # API lists versions in order; keep the newest per DOI
        latest[p["doi"]] = p
    return list(latest.values())


# ---------------------------------------------------------------- PubMed

def _esearch(term: str, start: date, end: date) -> list[str]:
    params = _ncbi({
        "db": "pubmed", "term": term, "retmax": 5000, "retmode": "json",
        "datetype": "edat", "mindate": start.strftime("%Y/%m/%d"),
        "maxdate": end.strftime("%Y/%m/%d"),
    })
    r = _get(f"{EUTILS}/esearch.fcgi", params)
    return r.json()["esearchresult"].get("idlist", [])


def _text(el) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def _parse_pubmed(art) -> dict:
    pmid = _text(art.find(".//PMID"))
    title = _text(art.find(".//ArticleTitle"))
    abstract = " ".join(_text(a) for a in art.findall(".//Abstract/AbstractText"))
    journal = _text(art.find(".//Journal/Title"))
    ta = _text(art.find(".//MedlineJournalInfo/MedlineTA"))
    doi = ""
    for aid in art.findall(".//PubmedData/ArticleIdList/ArticleId"):
        if aid.get("IdType") == "doi":
            doi = (aid.text or "").strip().lower()
    names = []
    for au in art.findall(".//AuthorList/Author")[:6]:
        last, ini = _text(au.find("LastName")), _text(au.find("Initials"))
        if last:
            names.append(f"{last} {ini}".strip())
    ptypes = [_text(p) for p in art.findall(".//PublicationType")]
    return {
        "source": ta or journal,
        "pmid": pmid,
        "doi": doi,
        "title": title,
        "authors": ", ".join(names),
        "abstract": abstract,
        "pub_types": ptypes,
        "url": f"https://doi.org/{doi}" if doi else f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
    }


def _efetch(pmids: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(pmids), 200):
        params = _ncbi({"db": "pubmed", "id": ",".join(pmids[i:i + 200]), "retmode": "xml"})
        root = ET.fromstring(_get(f"{EUTILS}/efetch.fcgi", params).content)
        out.extend(_parse_pubmed(a) for a in root.findall(".//PubmedArticle"))
        time.sleep(0.4)
    return out


SKIP_TYPES = {"Published Erratum", "Retraction of Publication", "Comment", "Editorial", "News"}


def fetch_pubmed(cfg: dict, start: date, end: date) -> list[dict]:
    ids: set[str] = set()
    all_j = " OR ".join(f'"{j}"[ta]' for j in cfg["journals_all"])
    ids.update(_esearch(f"({all_j})", start, end))
    filt_j = " OR ".join(f'"{j}"[ta]' for j in cfg["journals_filtered"])
    ids.update(_esearch(f"({filt_j}) AND ({cfg['microbial_filter']})", start, end))
    ids.update(_esearch(pubmed_keyword_query(cfg["keyword_sets"]), start, end))
    papers = _efetch(sorted(ids))
    return [p for p in papers
            if p["title"] and p["abstract"] and not SKIP_TYPES & set(p["pub_types"])]


def fetch_all(cfg: dict, today: date) -> tuple[list[dict], dict]:
    start = today - timedelta(days=cfg["window_days"])
    cats = {c.lower() for c in cfg["biorxiv_categories"]}
    bio = [p for p in fetch_rxiv("biorxiv", start, today) if p["category"] in cats]
    med = [p for p in fetch_rxiv("medrxiv", start, today)
           if keyword_hit(p["title"] + " " + p["abstract"], cfg["keyword_sets"])]
    pm = fetch_pubmed(cfg, start, today)
    return bio + med + pm, {"bioRxiv": len(bio), "medRxiv": len(med), "PubMed": len(pm)}
