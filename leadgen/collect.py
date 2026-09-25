"""TWS lead collector.

Runs daily (GitHub Actions) before the Claude routine. It does the cheap,
mechanical part of lead generation so the routine only has to judge:

  1. gather candidate companies from job-board APIs and news RSS feeds;
  2. drop companies already passed on or on the kill list;
  3. probe each company's own ATS (Greenhouse / Ashby / Lever);
  4. run the cheap gates (staffing, stack, on-site only, dev team in
     EE/India/Vietnam, language, size);
  5. score the survivors and write the top N to output/latest.csv.

Standard library only, so the workflow needs no pip install.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUTPUT = ROOT.parent / "output"

UA = "Mozilla/5.0 (compatible; TWS-lead-collector/1.0)"
TIMEOUT = 20
TODAY = dt.date.today()

# How long a killed company stays out before the collector looks at it again.
KILL_RECHECK_DAYS = 90
# Ignore news older than this.
NEWS_MAX_AGE_DAYS = 30

# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

NEWS_FEEDS = [
    # (label, url, region)
    ("EU-Startups funding", "https://www.eu-startups.com/category/fundin/feed/", "EU"),
    ("EU-Startups", "https://www.eu-startups.com/feed/", "EU"),
    ("Tech.eu", "https://tech.eu/feed/", "EU"),
    ("Crunchbase News", "https://news.crunchbase.com/feed/", "ANY"),
    ("Fierce Healthcare", "https://www.fiercehealthcare.com/rss/xml", "US"),
    ("MobiHealthNews", "https://www.mobihealthnews.com/feed", "US"),
]

CMS_PAGES = [
    "https://www.cms.gov/health-tech-ecosystem/early-adopters/conversational-ai-assistants",
    "https://www.cms.gov/health-tech-ecosystem/early-adopters/kill-the-clipboard",
    "https://www.cms.gov/health-tech-ecosystem/early-adopters/diabetes-obesity",
]

# --------------------------------------------------------------------------
# Keyword tables
# --------------------------------------------------------------------------

ENG_TITLE = re.compile(
    r"engineer|developer|devops|\bsre\b|platform|backend|back-end|frontend|front-end|"
    r"full.?stack|architect|\bcto\b|tech(nical)? lead|machine learning|\bml\b|data engineer",
    re.I,
)

STACK_BAD = {
    "Java": r"\bjava\b(?!script)|spring boot|\bspring\b",
    ".NET/C#": r"\.net\b|\bc#|asp\.net|dotnet",
    "Angular": r"\bangular(js)?\b",
    "PHP": r"\bphp\b|laravel|symfony|magento",
    "Ruby": r"\bruby\b|\brails\b",
}
STACK_GOOD = {
    "TypeScript": r"typescript",
    "React": r"\breact\b",
    "Next.js": r"next\.?js",
    "Node": r"node\.?js|\bnode\b",
    "NestJS": r"nest\.?js",
    "Python": r"\bpython\b",
    "FastAPI": r"fastapi",
    "Django": r"django",
    "AWS": r"\baws\b",
    "Kubernetes": r"kubernetes|\bk8s\b",
    "Terraform": r"terraform",
    "Go": r"\bgolang\b",
}

INTEGRATION_HINT = re.compile(
    r"integrations?\b|connectors?\b|webhooks?|third.party api|\bhl7\b|\bedi\b|"
    r"\bpeppol\b|e-?invoic|marketplace api|partner api",
    re.I,
)
AI_HINT = re.compile(r"\bllm\b|\brag\b|genai|generative ai|ai agents?|langchain", re.I)
SECURITY_HINT = re.compile(r"soc ?2|iso ?27001|hipaa|nis2|\bcra\b|compliance", re.I)
HEALTH_HINT = re.compile(
    r"health|patient|clinic|care\b|medical|telehealth|telemed|pharma|therapy|hipaa", re.I
)

STAFFING_HINT = re.compile(
    r"staffing|recruitment agency|talent marketplace|outsourc|nearshor|offshor|"
    r"software house|dev(elopment)? agency|it services|consultancy|"
    r"we (are|build) (a )?software (development )?(company|agency)|for our clients?\b|"
    r"on behalf of (our|a) client",
    re.I,
)
LANGUAGE_HINT = re.compile(
    r"flie(ss|ß)end(e)? deutsch|fluent (in )?(german|french|dutch|italian)|"
    r"(german|french|dutch|italian) \(?(c1|c2|native|fluent)|deutschkenntnisse|"
    r"verhandlungssicher|fran[cç]ais courant|vloeiend nederlands|italiano fluente",
    re.I,
)

OFFSHORE_LOC = re.compile(
    r"ukraine|kyiv|kiev|lviv|kharkiv|poland|warsaw|krak[oó]w|wroc[lł]aw|gda[nń]sk|"
    r"romania|bucharest|cluj|bulgaria|sofia|serbia|belgrade|novi sad|hungary|budapest|"
    r"czech|prague|brno|slovakia|bratislava|croatia|zagreb|moldova|chi[sș]in[aă]u|"
    r"belarus|minsk|bosnia|sarajevo|macedonia|skopje|albania|tirana|"
    r"india|bangalore|bengaluru|hyderabad|pune|chennai|noida|gurgaon|gurugram|mumbai|delhi|"
    r"vietnam|ho chi minh|hanoi|da nang",
    re.I,
)

FUNDING_TITLE = re.compile(
    r"^(?P<co>[A-Z0-9][\w.&'’\- ]{1,40}?)(?:,[^,]{0,80},)?\s+"
    r"(?:raises|secures|lands|closes|bags|nabs|nets|gets|picks up|scores|snags|announces)\b"
    r".{0,40}?(?:[€$£]\s?\d|\d+(?:\.\d+)?\s?(?:million|m\b|bn|billion)|seed|series [a-d])",
    re.I,
)
ACQ_TITLE = re.compile(
    r"^(?P<buyer>[A-Z0-9][\w.&'’\- ]{1,40}?)\s+(?:acquires|has acquired|buys|snaps up|"
    r"to acquire|completes acquisition of|announces acquisition of)\s+"
    r"(?P<target>[A-Z0-9][\w.&'’\- ]{1,40}?)(?:[,:;]|\s+(?:to|in|for|as|from|and|,)\b|$)",
    re.I,
)
ACQUIRED_TITLE = re.compile(
    r"^(?P<co>[A-Z0-9][\w.&'’\- ]{1,40}?)\s+(?:is |has been |gets )?acquired by\b", re.I
)

LEGAL_SUFFIX = re.compile(
    r"[,.]?\s+(gmbh|ab|bv|b\.v\.|ltd|limited|inc|inc\.|sa|s\.a\.|nv|ag|llc|oy|as|aps|sas|srl|plc|co)\.?$",
    re.I,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def norm(name: str) -> str:
    """Normalized key for matching company names (routine STEP 1c)."""
    n = html.unescape(name or "").strip().lower()
    for _ in range(2):
        n = LEGAL_SUFFIX.sub("", n)
    n = re.sub(r"\.(io|ai|com|co|app|dev|tech|health)$", "", n)
    n = re.sub(r"[^a-z0-9]+", "", n)
    return n


def slugs(name: str) -> list[str]:
    """ATS board slugs to try for a company name."""
    base = html.unescape(name).strip().lower()
    base = LEGAL_SUFFIX.sub("", base)
    words = re.findall(r"[a-z0-9]+", base)
    if not words:
        return []
    out = ["".join(words), "-".join(words)]
    if len(words) > 1 and words[-1] in {"health", "hq", "app", "labs", "ai", "io", "technologies", "tech"}:
        out.append("".join(words[:-1]))
    return list(dict.fromkeys(s for s in out if len(s) >= 2))


def strip_html(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def fetch(url: str, *, as_json: bool = False):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        body = r.read().decode("utf-8", "replace")
    return json.loads(body) if as_json else body


def try_fetch(url: str, *, as_json: bool = False):
    try:
        return fetch(url, as_json=as_json)
    except Exception:
        return None


def parse_date(value) -> dt.date | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        v = value / 1000 if value > 1e11 else value
        return dt.datetime.fromtimestamp(v, dt.timezone.utc).date()
    s = str(value).strip()
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(s).date()
    except Exception:
        return None


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class Signal:
    kind: str  # acquirer | funding | cms | job
    date: dt.date | None
    text: str
    url: str


@dataclass
class Candidate:
    name: str
    region: str = "ANY"
    signals: list[Signal] = field(default_factory=list)
    job_texts: list[str] = field(default_factory=list)  # from job-board APIs
    job_locations: list[str] = field(default_factory=list)
    # filled by enrichment
    ats_url: str = ""
    ats_jobs: list[dict] = field(default_factory=list)
    kill: str = ""
    facts: dict = field(default_factory=dict)
    score: int = 0

    @property
    def key(self) -> str:
        return norm(self.name)


class Pool:
    def __init__(self) -> None:
        self.by_key: dict[str, Candidate] = {}
        self.acquired: set[str] = set()

    def add(self, name: str, region: str = "ANY") -> Candidate | None:
        name = re.sub(r"\s+", " ", html.unescape(name or "")).strip(" -–—:,.")
        k = norm(name)
        if len(k) < 2 or len(name) > 60:
            return None
        c = self.by_key.get(k)
        if c is None:
            c = self.by_key[k] = Candidate(name=name, region=region)
        elif c.region == "ANY":
            c.region = region
        return c


# --------------------------------------------------------------------------
# Collectors
# --------------------------------------------------------------------------


def rss_items(xml_text: str):
    root = ET.fromstring(xml_text.encode("utf-8"))
    for item in root.iter("item"):
        yield (
            strip_html(item.findtext("title") or ""),
            (item.findtext("link") or "").strip(),
            parse_date(item.findtext("pubDate")),
        )


def classify_headline(title: str):
    """Return (kind, company, extra) for a news headline, or None."""
    m = ACQUIRED_TITLE.match(title)
    if m:
        return ("acquired", m.group("co"), "")
    m = ACQ_TITLE.match(title)
    if m:
        return ("acquirer", m.group("buyer"), m.group("target"))
    m = FUNDING_TITLE.match(title)
    if m:
        return ("funding", m.group("co"), "")
    return None


def collect_news(pool: Pool) -> None:
    for label, url, region in NEWS_FEEDS:
        text = try_fetch(url)
        if not text:
            log(f"  news: {label}: unreachable")
            continue
        n = 0
        try:
            items = list(rss_items(text))
        except ET.ParseError:
            log(f"  news: {label}: not RSS")
            continue
        for title, link, date in items:
            if date and (TODAY - date).days > NEWS_MAX_AGE_DAYS:
                continue
            hit = classify_headline(title)
            if not hit:
                continue
            kind, co, target = hit
            if kind == "acquired":
                pool.acquired.add(norm(co))
                continue
            if kind == "acquirer" and target:
                pool.acquired.add(norm(target))
            c = pool.add(co, region)
            if c:
                c.signals.append(Signal(kind, date, title, link))
                n += 1
        log(f"  news: {label}: {len(items)} items, {n} signals")


def collect_cms(pool: Pool) -> None:
    for url in CMS_PAGES:
        page = try_fetch(url)
        if not page:
            log(f"  cms: {url.rsplit('/', 1)[-1]}: unreachable")
            continue
        main = re.search(r"<main.*?</main>", page, re.S | re.I)
        body = main.group(0) if main else page
        names = set()
        for cell in re.findall(r"<(?:li|td|h3|h4|strong)[^>]*>(.*?)</(?:li|td|h3|h4|strong)>", body, re.S | re.I):
            t = strip_html(cell)
            # company names are short and mostly capitalised; skip prose
            if 2 <= len(t) <= 45 and t[0].isupper() and len(t.split()) <= 5 and not t.endswith("."):
                names.add(t)
        for t in names:
            c = pool.add(t, "US")
            if c:
                c.signals.append(Signal("cms", None, f"CMS pledge: {url.rsplit('/', 1)[-1]}", url))
        log(f"  cms: {url.rsplit('/', 1)[-1]}: {len(names)} names")


def _add_job(pool: Pool, company: str, title: str, desc: str, location: str, date, url: str, region="ANY"):
    if not company or not ENG_TITLE.search(title or ""):
        return
    c = pool.add(company, region)
    if not c:
        return
    c.job_texts.append(f"{title}\n{strip_html(desc)[:6000]}")
    c.job_locations.append(location or "")
    c.signals.append(Signal("job", parse_date(date), title, url))


def collect_jobs(pool: Pool) -> None:
    before = len(pool.by_key)

    data = try_fetch("https://remotive.com/api/remote-jobs?category=software-dev", as_json=True)
    for j in (data or {}).get("jobs", []):
        _add_job(pool, j.get("company_name"), j.get("title"), j.get("description"),
                 j.get("candidate_required_location"), j.get("publication_date"), j.get("url"))
    log(f"  jobs: remotive: {len((data or {}).get('jobs', []))}")

    data = try_fetch("https://remoteok.com/api", as_json=True)
    rows = [j for j in (data or []) if isinstance(j, dict) and j.get("company")]
    for j in rows:
        _add_job(pool, j.get("company"), j.get("position"), j.get("description"),
                 j.get("location"), j.get("date"), j.get("url"))
    log(f"  jobs: remoteok: {len(rows)}")

    for offset in (0, 20, 40, 60, 80):
        data = try_fetch(f"https://himalayas.app/jobs/api?limit=20&offset={offset}", as_json=True)
        jobs = (data or {}).get("jobs", [])
        for j in jobs:
            loc = ", ".join(j.get("locationRestrictions") or [])
            _add_job(pool, j.get("companyName"), j.get("title"), j.get("description") or j.get("excerpt"),
                     loc, j.get("pubDate"), j.get("applicationLink") or j.get("guid"))
        if not jobs:
            break
    log("  jobs: himalayas: done")

    url = "https://www.arbeitnow.com/api/job-board-api"
    for _ in range(3):
        data = try_fetch(url, as_json=True)
        if not data:
            break
        for j in data.get("data", []):
            if not j.get("remote"):
                continue
            _add_job(pool, j.get("company_name"), j.get("title"), j.get("description"),
                     j.get("location"), j.get("created_at"), j.get("url"), "EU")
        url = (data.get("links") or {}).get("next")
        if not url:
            break
    log("  jobs: arbeitnow: done")
    log(f"  jobs: {len(pool.by_key) - before} new companies")


# --------------------------------------------------------------------------
# ATS enrichment
# --------------------------------------------------------------------------


def ats_greenhouse(slug: str):
    data = try_fetch(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true", as_json=True)
    if not data or "jobs" not in data:
        return None
    jobs = []
    for j in data["jobs"]:
        loc = (j.get("location") or {}).get("name", "")
        jobs.append({
            "title": j.get("title", ""),
            "location": loc,
            "remote": "remote" in loc.lower(),
            "opened": parse_date(j.get("first_published") or j.get("updated_at")),
            "text": strip_html(j.get("content", ""))[:6000],
        })
    return f"https://boards.greenhouse.io/{slug}", jobs


def ats_ashby(slug: str):
    data = try_fetch(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", as_json=True)
    if not data or "jobs" not in data:
        return None
    jobs = []
    for j in data["jobs"]:
        loc = j.get("location", "") or ""
        wt = (j.get("workplaceType") or "").lower()
        jobs.append({
            "title": j.get("title", ""),
            "location": loc,
            "remote": bool(j.get("isRemote")) or wt == "remote" or "remote" in loc.lower(),
            "opened": parse_date(j.get("publishedAt")),
            "text": (j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")))[:6000],
        })
    return f"https://jobs.ashbyhq.com/{slug}", jobs


def ats_lever(slug: str):
    data = try_fetch(f"https://api.lever.co/v0/postings/{slug}?mode=json", as_json=True)
    if not isinstance(data, list):
        return None
    jobs = []
    for j in data:
        cat = j.get("categories") or {}
        loc = cat.get("location", "") or ""
        jobs.append({
            "title": j.get("text", ""),
            "location": loc,
            "remote": (j.get("workplaceType") or "").lower() == "remote" or "remote" in loc.lower(),
            "opened": parse_date(j.get("createdAt")),
            "text": (j.get("descriptionPlain") or "")[:6000],
        })
    return f"https://jobs.lever.co/{slug}", jobs


def enrich(c: Candidate) -> Candidate:
    for slug in slugs(c.name):
        for probe in (ats_ashby, ats_greenhouse, ats_lever):
            res = probe(slug)
            if res and res[1]:
                c.ats_url, c.ats_jobs = res
                return c
    return c


# --------------------------------------------------------------------------
# Gates and scoring
# --------------------------------------------------------------------------


def hits(patterns: dict[str, str], text: str) -> dict[str, int]:
    out = {}
    for label, pat in patterns.items():
        n = len(re.findall(pat, text, re.I))
        if n:
            out[label] = n
    return out


def evaluate(c: Candidate, kill_keys: set[str], acquired: set[str]) -> None:
    f = c.facts
    if c.key in kill_keys:
        c.kill = "kill list"
        return
    if c.key in acquired:
        c.kill = "G2: acquired (news)"
        return

    eng_jobs = [j for j in c.ats_jobs if ENG_TITLE.search(j["title"])]
    texts = [f"{j['title']}\n{j['text']}" for j in eng_jobs] + c.job_texts
    blob = "\n".join(texts)
    locations = [j["location"] for j in eng_jobs] + c.job_locations

    if STAFFING_HINT.search(c.name) or len(STAFFING_HINT.findall(blob)) >= 2:
        c.kill = "G0: staffing/outsourcing wording"
        return

    bad, good = hits(STACK_BAD, blob), hits(STACK_GOOD, blob)
    f["stack_bad"], f["stack_good"] = bad, good
    if sum(bad.values()) >= 2 and sum(bad.values()) > sum(good.values()):
        c.kill = "G1: stack " + "/".join(sorted(bad, key=bad.get, reverse=True))
        return

    offshore = sorted({m.group(0).title() for loc in locations for m in [OFFSHORE_LOC.search(loc or "")] if m})
    if offshore:
        c.kill = "G3: eng roles in " + ", ".join(offshore[:3])
        return

    if LANGUAGE_HINT.search(blob):
        c.kill = "language: local language required"
        return

    if len(c.ats_jobs) > 60 or len(eng_jobs) > 25:
        c.kill = f"G5: too big ({len(c.ats_jobs)} open roles, {len(eng_jobs)} eng)"
        return

    if eng_jobs and not any(j["remote"] for j in eng_jobs) and not c.job_texts:
        c.kill = "on-site only (own ATS)"
        return

    ages = [(TODAY - j["opened"]).days for j in eng_jobs if j["opened"]]
    f["eng_open"] = len(eng_jobs) or len(c.job_texts)
    f["oldest_days"] = max(ages) if ages else ""
    f["remote"] = any(j["remote"] for j in eng_jobs) or bool(c.job_texts)
    f["locations"] = sorted({l for l in locations if l})[:4]
    f["integration"] = bool(INTEGRATION_HINT.search(blob))
    f["ai"] = bool(AI_HINT.search(blob))
    f["security"] = bool(SECURITY_HINT.search(blob))
    f["health"] = bool(HEALTH_HINT.search(blob + " " + " ".join(s.text for s in c.signals)))

    kinds = {s.kind for s in c.signals}
    # Without a verifiable hiring page or a strong news trigger there is too little to go on.
    if not c.ats_url and not c.job_texts and not ({"acquirer", "cms"} & kinds):
        c.kill = "no ATS and weak signal"
        return

    score = 0
    score += 5 if "acquirer" in kinds else 0
    score += 3 if "cms" in kinds else 0
    score += 1 if "funding" in kinds else 0
    score += 3 if isinstance(f["oldest_days"], int) and f["oldest_days"] > 60 else 0
    score += 2 if c.ats_url else 0
    score += 1 if 1 <= f["eng_open"] <= 10 else 0
    score += 2 if f["integration"] else 0
    score += 1 if f["ai"] else 0
    score += 1 if f["security"] else 0
    score += 1 if sum(good.values()) >= 2 else 0
    score += 1 if len(kinds - {"job"}) >= 1 and (c.ats_url or c.job_texts) else 0
    c.score = score


def trigger_of(c: Candidate) -> Signal | None:
    rank = {"acquirer": 0, "cms": 1, "funding": 3, "job": 4}
    sigs = sorted(c.signals, key=lambda s: (rank.get(s.kind, 9), -(s.date.toordinal() if s.date else 0)))
    return sigs[0] if sigs else None


def icp_hint(c: Candidate) -> str:
    if c.facts.get("health") and c.region in ("US", "ANY"):
        return "1?"
    n = c.facts.get("eng_open") or 0
    return "2A?" if n <= 4 else "2B?"


def offer_hint(c: Candidate) -> str:
    kinds = {s.kind for s in c.signals}
    if "acquirer" in kinds:
        return "план злиття"
    if c.facts.get("ai"):
        return "AI-фіча з інтеграціями"
    if c.facts.get("security"):
        return "пакет доказів безпеки"
    return "пакет конекторів"


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


def load_kill_keys() -> set[str]:
    path = DATA / "kill_list.txt"
    return {norm(l) for l in path.read_text().splitlines() if l.strip() and not l.startswith("#")}


def load_seen() -> tuple[list[dict], set[str]]:
    path = DATA / "seen.csv"
    rows = list(csv.DictReader(path.open())) if path.exists() else []
    skip = set()
    for r in rows:
        d = parse_date(r.get("date"))
        if r.get("status") == "passed" or (d and (TODAY - d).days < KILL_RECHECK_DAYS):
            skip.add(r["key"])
    return rows, skip


def save_seen(rows: list[dict]) -> None:
    with (DATA / "seen.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["key", "company", "status", "reason", "date"])
        w.writeheader()
        w.writerows(rows)


OUT_FIELDS = [
    "rank", "collected_on", "company", "score", "icp_hint", "offer_hint", "trigger_date", "trigger",
    "trigger_url", "other_signals", "ats_url", "eng_roles_open", "oldest_eng_role_days", "remote",
    "stack_seen", "stack_red_flags", "eng_locations", "integration_wording", "to_verify",
]


def row_for(rank: int, c: Candidate) -> dict:
    f = c.facts
    t = trigger_of(c)
    others = [f"{s.date or ''} {s.kind}: {s.text}".strip() for s in c.signals if s is not t][:3]
    return {
        "rank": rank,
        "collected_on": TODAY.isoformat(),
        "company": c.name,
        "score": c.score,
        "icp_hint": icp_hint(c),
        "offer_hint": offer_hint(c),
        "trigger_date": t.date.isoformat() if t and t.date else "",
        "trigger": f"{t.kind}: {t.text}" if t else "",
        "trigger_url": t.url if t else "",
        "other_signals": " | ".join(others),
        "ats_url": c.ats_url,
        "eng_roles_open": f.get("eng_open", ""),
        "oldest_eng_role_days": f.get("oldest_days", ""),
        "remote": "yes" if f.get("remote") else "no",
        "stack_seen": ", ".join(sorted(f.get("stack_good", {}))),
        "stack_red_flags": ", ".join(sorted(f.get("stack_bad", {}))),
        "eng_locations": "; ".join(f.get("locations", [])),
        "integration_wording": "yes" if f.get("integration") else "no",
        "to_verify": "website, size, ownership (G2), who pays (G0b), named tech contact (G6)",
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=30, help="how many candidates to pass on")
    ap.add_argument("--max-enrich", type=int, default=250, help="cap on ATS probes")
    ap.add_argument("--dry-run", action="store_true", help="do not update seen.csv")
    args = ap.parse_args()

    kill_keys = load_kill_keys()
    seen_rows, skip = load_seen()

    pool = Pool()
    log("Collecting…")
    collect_news(pool)
    collect_cms(pool)
    collect_jobs(pool)

    fresh = [c for c in pool.by_key.values() if c.key not in skip]
    log(f"{len(pool.by_key)} companies found, {len(fresh)} not seen before")

    # Strong news signals first, so the enrichment cap never drops them.
    prio = {"acquirer": 0, "cms": 1, "funding": 2, "job": 3}
    fresh.sort(key=lambda c: min((prio.get(s.kind, 9) for s in c.signals), default=9))
    fresh = fresh[: args.max_enrich]

    log(f"Probing ATS for {len(fresh)} companies…")
    with ThreadPoolExecutor(max_workers=16) as ex:
        fresh = list(ex.map(enrich, fresh))

    for c in fresh:
        evaluate(c, kill_keys, pool.acquired)

    passed = sorted((c for c in fresh if not c.kill), key=lambda c: -c.score)[: args.top]
    killed = [c for c in fresh if c.kill]

    reasons: dict[str, int] = {}
    for c in killed:
        r = c.kill.split(":")[0]
        reasons[r] = reasons.get(r, 0) + 1
    log(f"Passed {len(passed)}, killed {len(killed)}: {reasons}")

    OUTPUT.mkdir(exist_ok=True)
    rows = [row_for(i + 1, c) for i, c in enumerate(passed)]
    for path in (OUTPUT / "latest.csv", OUTPUT / f"candidates-{TODAY.isoformat()}.csv"):
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=OUT_FIELDS, quoting=csv.QUOTE_ALL)
            w.writeheader()
            w.writerows(rows)
    log(f"Wrote {len(rows)} rows to output/latest.csv")

    if not args.dry_run:
        # Killed ones come back after KILL_RECHECK_DAYS; passed ones never do.
        # Candidates that survived but missed the top N are not recorded, so they compete again tomorrow.
        today = TODAY.isoformat()
        seen_rows += [{"key": c.key, "company": c.name, "status": "passed", "reason": "", "date": today} for c in passed]
        seen_rows += [{"key": c.key, "company": c.name, "status": "killed", "reason": c.kill, "date": today}
                      for c in killed if c.kill != "kill list"]
        save_seen(seen_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
