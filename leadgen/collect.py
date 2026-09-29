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
    r"software|\bswe\b|developer|devops|\bsre\b|platform engineer|backend|back-end|frontend|front-end|"
    r"full.?stack|\bcto\b|tech(nical)? lead|machine learning|\bml\b|\bai engineer|data engineer|"
    r"infrastructure engineer|cloud engineer|forward deployed|technical staff|engineering manager|"
    r"head of engineering|vp,? engineering|(web|mobile|ios|android|integrations?|product) engineer",
    re.I,
)
# Engineering-sounding titles that are not product engineering.
NON_ENG_TITLE = re.compile(
    r"estimator|architectural|civil|mechanical|electrical|structural|hardware|sales engineer|"
    r"solutions? (architect|engineer|consultant)|consultant|pre-?sales|support engineer|"
    r"recruit|intern\b|designer|test (technician|operator)|instructor|teacher|trainer|tutor|peoplesoft|analyst",
    re.I,
)


def is_eng(title: str) -> bool:
    return bool(ENG_TITLE.search(title or "")) and not NON_ENG_TITLE.search(title or "")

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
    r"health ?care|digital health|patients?\b|clinic|medical|telehealth|telemed|pharma|therapy|hipaa", re.I
)

STAFFING_HINT = re.compile(
    r"staffing|recruitment agency|talent marketplace|outsourc|nearshor|offshor|"
    r"software house|dev(elopment)? agency|it services|consultancy|"
    r"we (are|build) (a )?software (development )?(company|agency)|for our clients?\b|"
    r"on behalf of (our|a) client",
    re.I,
)
STAFFING_NAME = re.compile(
    r"consult|staffing|recruit|talent|careers|outsourc|nearshore|offshore|infotech|infosys|"
    r"it solutions|software solutions|technologies (inc|llc|pvt)|\bpvt\b|services (inc|llc|ltd)",
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

# Sentences that describe what the company itself does.
ABOUT_SENTENCE = re.compile(
    r"\b(we are|we're|we build|we make|we help|we provide|we offer|our (platform|product|mission|customers)|"
    r"(is|are) (a|an|the) (leading |fast.growing |venture.backed |yc.backed )?[\w-]+( [\w-]+)?"
    r" (platform|company|startup|tool|service|provider|solution|app|software))\b",
    re.I,
)
# The product itself is infrastructure, dev tooling or security (routine rule: kill).
INFRA_PRODUCT = re.compile(
    r"developer (platform|tools?|experience platform)|dev ?tools|ci/cd (platform|tool)|continuous integration|"
    r"observability (platform|company|tool)|monitoring platform|\bapm\b|data warehouse|vector (db|database)|"
    r"database (company|platform|product)|(open.source|serverless|distributed|managed) (database|postgres)|"
    r"cloud (infrastructure|platform|provider|hosting)|infrastructure (platform|company|software|provider)|"
    r"(web|cloud|managed) hosting|\bcdn\b|edge (network|cloud|platform)|kubernetes (platform|management)|"
    r"(security|cybersecurity|identity|endpoint|threat|vulnerability|devsecops) (platform|company|solution|detection|management)|"
    r"cybersecurity|api gateway|serverless platform|backend.as.a.service|artifact (management|repository)|"
    r"package (manager|registry)|feature flag|deployment platform|internal developer platform",
    re.I,
)
PUBLIC_NAME = re.compile(r"\bplc\b", re.I)
NON_TARGET_NAME = re.compile(
    r"universit|college|school|foundation|government|county|city of|ministry|hospital|\bbank\b|"
    r"institute|philanthrop|association|society|council|charity",
    re.I,
)
# Not a company name at all: HN header fragments, places, placeholders.
JUNK_NAME = re.compile(
    r"(?i::|^(location|remote|hybrid|onsite|on-site|full.?time|part.?time|contract|hiring|we're|we are)\b|"
    r"^(director|head|vp|vice president) of\b|^stealth\b|"
    r"^(a |an |the )?(saas|stealth|ai|fintech|healthtech|b2b|seed.stage|early.stage)( \w+)? (startup|company)$)|"
    r"^[A-Z][a-z]+, (?!Inc\b|LLC\b|Ltd\b|GmbH\b|Co\b)[A-Z][a-zA-Z]+( [A-Z][a-zA-Z]+)?$"
)
NONPROFIT_ABOUT = re.compile(
    r"non-?profit|not-for-profit|501\(c\)|philanthrop|charit(y|able)|research institute|academic|"
    r"open.source (project|community|initiative)|humanitarian|scholarly",
    re.I,
)
AGENCY_ABOUT = re.compile(
    r"\b(agency|consultancy|consulting (firm|company)|transformation firm|dev(elopment)? shop|software house|"
    r"for our clients|our clients'|on-demand teams|staff augmentation|nearshore|outsourc|consultant network)",
    re.I,
)
GOV_ABOUT = re.compile(
    r"national security|\bdefen[cs]e\b|government agencies|federal (agencies|government)|public sector|\bdod\b|"
    r"health plans|payers|medicare advantage|insurers",
    re.I,
)
HEADCOUNT = re.compile(r"(\d[\d,.]*)\s*(k)?\+?\s*(employees|people|staff|team members|engineers)\b", re.I)
# One-line descriptions that mean dev tooling / infrastructure / security on their own.
INFRA_ABOUT = re.compile(
    r"open.source [\w-]+ (framework|sdk|library|runtime)|framework for building|\bsdk\b|\bruntime\b|"
    r"\bbpf\b|linux (kernel|internals)|(give|help|for) developers|security teams|attack surface|"
    r"infrastructure for|(uptime|application|infrastructure|synthetic|api) monitoring|observability|\bdevops\b|"
    r"developer.first|infrastructure.as.code|infrastructure orchestrat|data replication|\bcdc\b|making the tools",
    re.I,
)


def headcount(text: str) -> int:
    """Largest 'N employees/people' figure a company states about itself, 0 if none."""
    best = 0
    for num, k, _ in HEADCOUNT.findall(text or ""):
        try:
            n = float(num.replace(",", ""))
        except ValueError:
            continue
        best = max(best, int(n * 1000 if k else n))
    return best

# Contact details in a company's own HN post.
EMAIL = re.compile(
    r"([a-z0-9._%+-]+)\s*(?:@|\[at\]|\(at\)|\{at\}|\s\[\s?at\s?\]\s)\s*"
    r"([a-z0-9-]+(?:(?:\.|\s?\[dot\]\s?|\s?\(dot\)\s?)[a-z0-9-]+)+)",
    re.I,
)
EMAIL_TLDS = set(
    "com io ai co org net dev app tech health cx xyz so sh gg me inc cloud systems services software studio "
    "space team work jobs care bio energy finance money legal law build tools solutions digital agency "
    "eu uk de fr nl be lu ch at dk se no fi is ee lv lt pl cz sk es pt it ie us ca au nz il sg in br mx".split()
)
GENERIC_MAILBOX = re.compile(r"^(jobs|careers|hiring|recruit\w*|talent|hr|hello|hi|info|team|apply|work|join\w*|people)$", re.I)
TECH_TITLE = (r"CTO|CEO|(?:technical |tech )?co-?founder|founder|VP,? (?:of )?Engineering|Head of Engineering|"
              r"Director of Engineering|Engineering Manager|Head of Platform|Head of AI|Chief Technology Officer|"
              r"Staff Engineer|Principal Engineer|Tech Lead")
CONTACT_INTRO = re.compile(
    r"(?:I'm|I am|my name is|this is|hi,? I'm)\s+([A-Z][a-z]+(?: [A-Z][a-z]+)?),?\s+(?:the |a |one of the )?"
    r"(" + TECH_TITLE + r")\b",
)
CONTACT_SIGNOFF = re.compile(r"([A-Z][a-z]+(?: [A-Z][a-z]+)?)\s*[,(–—-]\s*(" + TECH_TITLE + r")\b")
CONTACT_REACH = re.compile(
    r"(?i:email|e-mail|reach out to|contact|message|write to|ping)\s+(?i:me|our|the)?\s*"
    r"(" + TECH_TITLE + r")?,?\s*([A-Z][a-z]+(?: [A-Z][a-z]+)?)?\s*(?i:directly|at|on|via|:)",
)
NOT_A_NAME = re.compile(r"^(The|Our|We|Me|Us|Please|Directly|Apply|Email|Contact|Hiring|Remote|Team)\b")
EUROPE_HINT = re.compile(
    r"europe|\bemea\b|\beu\b|\bcet\b|\buk\b|united kingdom|london|germany|berlin|munich|netherlands|"
    r"amsterdam|france|paris|spain|madrid|barcelona|portugal|lisbon|ireland|dublin|sweden|stockholm|"
    r"denmark|copenhagen|norway|oslo|finland|helsinki|belgium|brussels|austria|vienna|switzerland|zurich|"
    r"italy|milan|estonia|tallinn|latvia|riga|lithuania|vilnius|worldwide|anywhere",
    re.I,
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
    website: str = ""
    signals: list[Signal] = field(default_factory=list)
    job_texts: list[str] = field(default_factory=list)  # from job-board APIs
    job_locations: list[str] = field(default_factory=list)
    about: str = ""  # one line on what the company does
    team_size: int = 0  # stated by a directory (YC), 0 if unknown
    contact: dict = field(default_factory=dict)  # name, title, email, source
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

    def add(self, name: str, region: str = "ANY", website: str = "") -> Candidate | None:
        name = re.sub(r"\s+", " ", html.unescape(name or "")).strip(" -–—:,.")
        k = norm(name)
        if len(k) < 2 or len(name) > 60:
            return None
        c = self.by_key.get(k)
        if c is None:
            c = self.by_key[k] = Candidate(name=name, region=region)
        elif c.region == "ANY":
            c.region = region
        if website and not c.website:
            c.website = website
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
            item.findtext("description") or "",
        )


def clean_news_name(name: str) -> str:
    """'Amsterdam-based Duqu' -> 'Duqu', 'London traveltech Stasher' -> 'Stasher'."""
    toks = name.split()
    cut = -1
    for i, t in enumerate(toks[:-1]):
        if t.endswith("-based") or t.endswith("’s") or t.endswith("'s") or t.islower():
            cut = i
    return " ".join(toks[cut + 1:])


def classify_headline(title: str):
    """Return (kind, company, extra) for a news headline, or None."""
    m = ACQUIRED_TITLE.match(title)
    if m:
        return ("acquired", clean_news_name(m.group("co")), "")
    m = ACQ_TITLE.match(title)
    if m:
        return ("acquirer", clean_news_name(m.group("buyer")), clean_news_name(m.group("target")))
    m = FUNDING_TITLE.match(title)
    if m:
        return ("funding", clean_news_name(m.group("co")), "")
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
        for title, link, date, _ in items:
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


NON_COMPANY_HOSTS = re.compile(
    r"(^|\.)(cms|hhs|medicare|medicaid|healthit|usa|whitehouse|youtube|twitter|x|linkedin|facebook|"
    r"instagram|github|google|apple|microsoft)\.(gov|com)$|\.gov$",
    re.I,
)


def cms_companies(page: str) -> dict[str, str]:
    """Company name -> website, from the pledge links that point off cms.gov."""
    main = re.search(r"<main.*?</main>", page, re.S | re.I)
    body = main.group(0) if main else page
    out = {}
    for href, inner in re.findall(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', body, re.S | re.I):
        host = urllib.parse.urlparse(href).netloc.lower().removeprefix("www.")
        if not host or NON_COMPANY_HOSTS.search(host):
            continue
        name = strip_html(inner)
        if not name:
            alt = re.search(r'alt="([^"]+)"', inner)
            name = html.unescape(alt.group(1)).strip() if alt else ""
        name = re.sub(r"\s+logo$", "", name, flags=re.I)
        if 2 <= len(name) <= 50 and not re.search(r"learn more|click|read|here|download|\bpdf\b", name, re.I):
            out.setdefault(name, f"https://{host}")
    return out


def collect_cms(pool: Pool) -> None:
    for url in CMS_PAGES:
        label = url.rsplit("/", 1)[-1]
        page = try_fetch(url)
        if not page:
            log(f"  cms: {label}: unreachable")
            continue
        names = cms_companies(page)
        for name, site in names.items():
            c = pool.add(name, "US", site)
            if c:
                c.signals.append(Signal("cms", None, f"CMS pledge: {label}", url))
        log(f"  cms: {label}: {len(names)} companies")


YC_URL = "https://yc-oss.github.io/api/companies/all.json"
YC_SKIP_SUBINDUSTRY = re.compile(r"engineering, product and design|security|infrastructure|developer tools|devops", re.I)
YC_INDUSTRY = re.compile(r"b2b|healthcare|fintech|industrials|real estate|education|government", re.I)
YC_REGION = re.compile(r"europe|united kingdom|germany|france|netherlands|nordics|spain|united states|america|canada|remote", re.I)
YC_PER_DAY = 80


def yc_candidates(rows: list[dict], day: dt.date) -> list[dict]:
    """Active, hiring, 8–200 people, B2B/health, US/Europe; a different daily slice of them."""
    keep = []
    for r in rows:
        if not isinstance(r, dict) or r.get("status") != "Active" or not r.get("isHiring"):
            continue
        size = r.get("team_size") or 0
        if not (8 <= size <= 200):
            continue
        industry = f"{r.get('industry', '')} {r.get('subindustry', '')}"
        if not YC_INDUSTRY.search(industry) or YC_SKIP_SUBINDUSTRY.search(industry):
            continue
        regions = " ".join(r.get("regions") or []) + " " + (r.get("all_locations") or "")
        if not YC_REGION.search(regions):
            continue
        keep.append(r)
    # Rotate through the list: every day starts at a different offset, so each company comes up in turn.
    keep.sort(key=lambda r: r.get("slug") or r.get("name") or "")
    if not keep:
        return []
    start = (day.toordinal() * YC_PER_DAY) % len(keep)
    return (keep[start:] + keep[:start])[:YC_PER_DAY]


def collect_yc(pool: Pool) -> None:
    rows = try_fetch(YC_URL, as_json=True)
    if not isinstance(rows, list):
        log("  yc: unreachable")
        return
    picked = yc_candidates(rows, TODAY)
    for r in picked:
        regions = " ".join(r.get("regions") or [])
        region = "EU" if re.search(r"europe|united kingdom|germany|france|netherlands|nordics|spain", regions, re.I) else "US"
        c = pool.add(r.get("name", ""), region, r.get("website") or "")
        if not c:
            continue
        c.team_size = int(r.get("team_size") or 0)
        desc = " ".join(x for x in (r.get("one_liner"), r.get("long_description")) if x)
        c.about = c.about or strip_html(desc)[:280]
        c.job_texts.append(strip_html(desc)[:3000])
        c.job_locations.append(r.get("all_locations") or regions)
        slug = r.get("slug") or ""
        c.signals.append(Signal("yc", None, f"YC {r.get('batch', '')}: {c.team_size} people, hiring, {r.get('industry', '')}",
                                f"https://www.ycombinator.com/companies/{slug}"))
    log(f"  yc: {len(rows)} companies, {len(picked)} picked today")


def _add_job(pool: Pool, company: str, title: str, desc: str, location: str, date, url: str,
             region="ANY", website=""):
    if not company or not is_eng(title):
        return
    c = pool.add(company, region, website)
    if not c:
        return
    c.job_texts.append(f"{title}\n{strip_html(desc)[:6000]}")
    c.job_locations.append(location or "")
    c.signals.append(Signal("job", parse_date(date), title, url))


def collect_jobs(pool: Pool) -> None:
    before = len(pool.by_key)

    n = 0
    for cat in ("software-dev", "devops"):
        data = try_fetch(f"https://remotive.com/api/remote-jobs?category={cat}", as_json=True)
        for j in (data or {}).get("jobs", []):
            _add_job(pool, j.get("company_name"), j.get("title"), j.get("description"),
                     j.get("candidate_required_location"), j.get("publication_date"), j.get("url"))
            n += 1
    log(f"  jobs: remotive: {n}")

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
    log(f"  jobs: himalayas: {offset + len(jobs)}")

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
    n = 0
    for cat in ("remote-back-end-programming-jobs", "remote-full-stack-programming-jobs", "remote-devops-sysadmin-jobs"):
        text = try_fetch(f"https://weworkremotely.com/categories/{cat}.rss")
        try:
            items = list(rss_items(text)) if text else []
        except ET.ParseError:
            items = []
        for title, link, date, desc in items:
            company, _, role = title.partition(":")
            if role:
                _add_job(pool, company, role.strip(), desc, "", date, link)
                n += 1
    log(f"  jobs: weworkremotely: {n}")

    data = try_fetch("https://jobicy.com/api/v2/remote-jobs?count=100", as_json=True)
    rows = (data or {}).get("jobs", [])
    for j in rows:
        _add_job(pool, j.get("companyName"), j.get("jobTitle"), j.get("jobDescription") or j.get("jobExcerpt"),
                 j.get("jobGeo"), j.get("pubDate"), j.get("url"))
    log(f"  jobs: jobicy: {len(rows)}")

    data = try_fetch("https://www.workingnomads.com/api/exposed_jobs/", as_json=True)
    rows = [j for j in (data or []) if isinstance(j, dict) and j.get("category_name") in ("Development", "System Administration")]
    for j in rows:
        _add_job(pool, j.get("company_name"), j.get("title"), j.get("description"),
                 j.get("location"), j.get("pub_date"), j.get("url"))
    log(f"  jobs: workingnomads: {len(rows)}")

    collect_hn(pool)
    log(f"  jobs: {len(pool.by_key) - before} new companies")


HN_URL = re.compile(r"https?://(?:www\.)?([a-z0-9.-]+\.[a-z]{2,})", re.I)


def hn_first_line(text: str) -> tuple[str, str, str]:
    """(company, website, headline) from a 'Who is hiring' comment."""
    first = re.split(r"<p>|\n", html.unescape(text or ""), maxsplit=1)[0]
    site = HN_URL.search(first)
    parts = [p.strip() for p in strip_html(first).split("|")]
    company = re.sub(r"\(.*?\)|https?://\S+", "", parts[0]).strip(" -–—:")
    website = f"https://{site.group(1).lower()}" if site and "ycombinator" not in site.group(1) else ""
    return company, website, " | ".join(parts[1:4])


def find_email(text: str, website: str = "") -> str:
    """One published address; a person's mailbox beats jobs@, the company's domain beats others."""
    found = []
    for user, domain in EMAIL.findall(text):
        domain = re.sub(r"\s*(?:\[dot\]|\(dot\)|\s+dot\s+)\s*", ".", domain, flags=re.I)
        domain = re.sub(r"\s+", "", domain).lower().strip(".")
        if "." not in domain or domain.split(".")[-1] not in EMAIL_TLDS or len(user) > 40:
            continue
        found.append(f"{user.lower()}@{domain}")
    if not found:
        return ""
    site = urllib.parse.urlparse(website).netloc.lower().removeprefix("www.") if website else ""
    found.sort(key=lambda e: (bool(GENERIC_MAILBOX.match(e.split("@")[0])), bool(site) and not e.endswith(site)))
    return found[0]


def find_contact(text: str) -> tuple[str, str]:
    """(name, title) of the person who posted, when they say who they are."""
    for rx in (CONTACT_INTRO, CONTACT_SIGNOFF):
        m = rx.search(text)
        if m:
            return m.group(1), m.group(2)
    m = CONTACT_REACH.search(text)
    if m and m.group(2) and not NOT_A_NAME.match(m.group(2)):
        return m.group(2), m.group(1) or ""
    return "", ""


def hn_about(text: str) -> str:
    """The HN post's own description: the first paragraph after the header line."""
    paras = [strip_html(p) for p in re.split(r"<p>", html.unescape(text or ""))[1:]]
    for p in paras:
        if len(p) > 40 and not p.lower().startswith(("apply", "email", "we're hiring", "we are hiring", "roles")):
            return p[:280]
    return ""


def collect_hn(pool: Pool) -> None:
    """Latest HN 'Ask HN: Who is hiring?' thread: small companies, often posted by founders."""
    data = try_fetch("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=10", as_json=True)
    story = next((h for h in (data or {}).get("hits", []) if "who is hiring" in (h.get("title") or "").lower()), None)
    if not story:
        log("  jobs: hn: thread not found")
        return
    sid = story["objectID"]
    data = try_fetch(f"https://hn.algolia.com/api/v1/search?tags=comment,story_{sid}&hitsPerPage=1000", as_json=True)
    n = 0
    for h in (data or {}).get("hits", []):
        if str(h.get("parent_id")) != str(sid):
            continue  # replies, not job posts
        text = h.get("comment_text") or ""
        plain = strip_html(text)
        if not re.search(r"\bremote\b", plain, re.I):
            continue
        company, website, headline = hn_first_line(text)
        if not company or len(company) > 40 or is_eng(company) or company.lower().startswith(("remote", "http")):
            continue
        c = pool.add(company, "ANY", website)
        if not c:
            continue
        post_url = f"https://news.ycombinator.com/item?id={h['objectID']}"
        c.job_texts.append(plain[:6000])
        c.job_locations.append(headline)
        c.signals.append(Signal("job", parse_date(h.get("created_at")), f"HN Who is hiring: {headline[:80]}", post_url))
        c.about = c.about or hn_about(text)
        if not c.contact:
            name, title = find_contact(plain)
            email = find_email(plain, c.website)
            if name or email:
                c.contact = {"name": name, "title": title, "email": email,
                             "source": post_url, "hn_user": h.get("author", "")}
        n += 1
    log(f"  jobs: hn who is hiring: {n}")


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


def ats_recruitee(slug: str):
    data = try_fetch(f"https://{slug}.recruitee.com/api/offers/", as_json=True)
    if not data or "offers" not in data:
        return None
    jobs = []
    for j in data["offers"]:
        loc = j.get("location", "") or ""
        jobs.append({
            "title": j.get("title", ""),
            "location": loc,
            "remote": bool(j.get("remote")) or "remote" in loc.lower(),
            "opened": parse_date(j.get("created_at") or j.get("published_at")),
            "text": strip_html((j.get("description") or "") + " " + (j.get("requirements") or ""))[:6000],
        })
    return f"https://{slug}.recruitee.com", jobs


def ats_personio(slug: str):
    text = try_fetch(f"https://{slug}.jobs.personio.de/xml")
    if not text or "<position" not in text:
        return None
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        return None
    jobs = []
    for pos in root.iter("position"):
        loc = pos.findtext("office") or ""
        desc = " ".join(v.text or "" for v in pos.iter("value"))
        jobs.append({
            "title": pos.findtext("name") or "",
            "location": loc,
            "remote": "remote" in (loc + " " + (pos.findtext("schedule") or "")).lower(),
            "opened": parse_date(pos.findtext("createdAt")),
            "text": strip_html(desc)[:6000],
        })
    return f"https://{slug}.jobs.personio.de", jobs


def ats_workable(slug: str):
    data = try_fetch(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", as_json=True)
    if not data or "jobs" not in data:
        return None
    jobs = []
    for j in data["jobs"]:
        loc = ", ".join(x for x in (j.get("city"), j.get("country")) if x)
        jobs.append({
            "title": j.get("title", ""),
            "location": loc,
            "remote": bool(j.get("telecommuting")),
            "opened": parse_date(j.get("created_at") or j.get("published_on")),
            "text": "",
        })
    return f"https://apply.workable.com/{slug}", jobs


def ats_smartrecruiters(slug: str):
    data = try_fetch(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", as_json=True)
    if not data or not data.get("content"):
        return None
    jobs = []
    for j in data["content"]:
        loc = j.get("location") or {}
        jobs.append({
            "title": j.get("name", ""),
            "location": ", ".join(x for x in (loc.get("city"), loc.get("country")) if x),
            "remote": bool(loc.get("remote")),
            "opened": parse_date(j.get("releasedDate")),
            "text": "",
        })
    return f"https://jobs.smartrecruiters.com/{slug}", jobs


# Career pages hosted by a vendor: the domain says nothing about the company's own slug.
HOSTED_CAREER_SITES = {"bamboohr", "greenhouse", "lever", "ashbyhq", "workable", "recruitee", "personio",
                       "smartrecruiters", "notion", "github", "google", "ycombinator", "wellfound", "breezy", "teamtailor"}

ATS_PROBES = (ats_ashby, ats_greenhouse, ats_lever, ats_recruitee, ats_personio, ats_workable, ats_smartrecruiters)


def enrich(c: Candidate) -> Candidate:
    names = slugs(c.name)
    if c.website:
        parts = urllib.parse.urlparse(c.website).netloc.lower().split(".")
        if len(parts) >= 2 and parts[-2] not in HOSTED_CAREER_SITES:
            names += [s for s in slugs(parts[-2]) if s not in names]
    for slug in names:
        for probe in ATS_PROBES:
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

    eng_jobs = [j for j in c.ats_jobs if is_eng(j["title"])]
    texts = [f"{j['title']}\n{j['text']}" for j in eng_jobs] + c.job_texts
    blob = "\n".join(texts)
    locations = [j["location"] for j in eng_jobs] + c.job_locations

    if STAFFING_NAME.search(c.name) or STAFFING_HINT.search(c.name) or len(STAFFING_HINT.findall(blob)) >= 2:
        c.kill = "G0: staffing/outsourcing wording"
        return

    if PUBLIC_NAME.search(c.name):
        c.kill = "G2/G5: public company"
        return
    if JUNK_NAME.search(c.name):
        c.kill = "not a company name (parsing junk)"
        return
    if NON_TARGET_NAME.search(c.name):
        c.kill = "not a product company (university/public body/bank)"
        return

    about = product_sentences(c)
    if not c.about and about:
        c.about = about[0][:280]
    own = " ".join(about[:3])  # the company's own words about itself
    infra = INFRA_PRODUCT.findall(" ".join(about[:15]))
    if len(infra) >= 2 or INFRA_PRODUCT.search(c.about) or INFRA_ABOUT.search(c.about):
        c.kill = "product is infrastructure/devtools/security"
        return
    if AGENCY_ABOUT.search(own):
        c.kill = "G0: agency/consultancy (own description)"
        return
    if NONPROFIT_ABOUT.search(own):
        c.kill = "non-profit/academic (own description)"
        return
    if GOV_ABOUT.search(own):
        c.kill = "revenue from government/insurers (own description)"
        return
    people = headcount(own) or c.team_size
    if people > 250:
        c.kill = f"G5: too big ({people} people)"
        return
    if c.team_size and c.team_size < 8:
        c.kill = f"G5: too small ({c.team_size} people)"
        return
    f["people"] = people or ""

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

    if len(c.ats_jobs) > 40 or len(eng_jobs) > 12:
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
    f["health"] = bool(HEALTH_HINT.search(own + " " + " ".join(s.text for s in c.signals if s.kind != "job")))

    kinds = {s.kind for s in c.signals}
    # Without a verifiable hiring page or a strong news trigger there is too little to go on.
    if not c.ats_url and not c.job_texts and not ({"acquirer", "cms", "yc"} & kinds):
        c.kill = "no ATS and weak signal"
        return

    score = 0
    score += 5 if "acquirer" in kinds else 0
    score += 3 if "cms" in kinds else 0
    score += 2 if "yc" in kinds else 0  # size, website and hiring status come from a directory
    score += 1 if 15 <= (people or 0) <= 120 else 0
    score += 1 if "funding" in kinds else 0
    score += 3 if isinstance(f["oldest_days"], int) and f["oldest_days"] > 60 else 0
    score += 2 if c.ats_url else 0
    score += 1 if c.website else 0
    score += 2 if c.contact.get("name") else 1 if c.contact.get("email") else 0
    where = " ".join(f.get("locations", [])) + " " + " ".join(s.text for s in c.signals)
    score += 2 if c.region == "EU" or EUROPE_HINT.search(where) else 0
    score += 1 if 1 <= f["eng_open"] <= 10 else 0
    score += 2 if f["integration"] else 0
    score += 1 if f["ai"] else 0
    score += 1 if f["security"] else 0
    score += 1 if sum(good.values()) >= 2 else 0
    score += 1 if len(kinds - {"job"}) >= 1 and (c.ats_url or c.job_texts) else 0
    c.score = score


def sentences(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+|\n+", text or "") if 25 <= len(x.strip()) <= 400]


def product_sentences(c: Candidate) -> list[str]:
    """Sentences that describe the company, from its posts and job ads."""
    first = c.name.split()[0].lower()
    out = [c.about] if c.about else []
    for t in c.job_texts + [j["text"] for j in c.ats_jobs]:
        for x in sentences(t):
            if ABOUT_SENTENCE.search(x) or x.lower().startswith(first + " "):
                out.append(x)
        if len(out) >= 25:
            break
    return list(dict.fromkeys(out))


def trigger_of(c: Candidate) -> Signal | None:
    rank = {"acquirer": 0, "cms": 1, "yc": 2, "funding": 3, "job": 4}
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


def load_registry() -> set[str]:
    """Companies the routine already judged (data/registry.csv); skipped until skip_until, or forever if empty."""
    path = DATA / "registry.csv"
    if not path.exists():
        return set()
    skip = set()
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if not r.get("company") or r["company"].startswith("#"):
                continue
            until = parse_date(r.get("skip_until"))
            if until is None or until > TODAY:
                skip.add(norm(r["company"]))
    return skip


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
    "stack_seen", "stack_red_flags", "eng_locations", "integration_wording", "to_verify", "website",
    "about", "contact_name", "contact_title", "contact_email", "contact_source",
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
        "to_verify": ("" if c.website else "website, ") + "size, ownership (G2), who pays (G0b)"
                     + ("" if c.contact.get("name") else ", named tech contact (G6)")
                     + ("" if c.ats_url else ", hiring page (no ATS found)"),
        "website": c.website,
        "about": c.about,
        "contact_name": c.contact.get("name", ""),
        "contact_title": c.contact.get("title", ""),
        "contact_email": c.contact.get("email", ""),
        "contact_source": (c.contact.get("source", "") + (f" (HN user {c.contact['hn_user']})" if c.contact.get("hn_user") else ""))
                          if c.contact else "",
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
    skip |= load_registry()

    pool = Pool()
    log("Collecting…")
    collect_news(pool)
    collect_cms(pool)
    collect_yc(pool)
    collect_jobs(pool)

    fresh = [c for c in pool.by_key.values() if c.key not in skip]
    log(f"{len(pool.by_key)} companies found, {len(fresh)} not seen before")

    # Strong news signals first, so the enrichment cap never drops them.
    prio = {"acquirer": 0, "cms": 1, "yc": 2, "funding": 3, "job": 4}
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
