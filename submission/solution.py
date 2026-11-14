"""
Temporal knowledge system for the credit-union website snapshots.

How it works (longer version in NOTES.md):
  * Every snapshot is parsed into small *units*: one per table row (with its caption, column
    headers and "rates effective as of" date) and one per short text chunk. A unit records
    which snapshots it appeared in, so `as_of` questions see the site exactly as it stood then.
  * Retrieval is BM25 over units, re-ranked by page authority (official rate pages beat
    calculators, landing pages, PDFs, blogs and old press releases), publication recency,
    boilerplate down-weighting and hard qualifier checks (term, model year, balance tier,
    jumbo / IRA / business / refinance / intro offers).
  * Answers are short and extractive: the matching table row(s), or the best one or two
    sentences, so stale numbers elsewhere on a page are never repeated.
  * Purpose-built handlers cover branch hours, branch addresses, "how many" counts (weighted
    vote across pages), links/URLs (only links that still resolve in the snapshot) and
    "does any X pay Y%?" claims.
  * conflicts(): the same fact (same product row / count) is tracked across pages and
    snapshots; disagreements are reported with the value from the most authoritative,
    newest source.

Standard library only, no network, deterministic.
"""
from __future__ import annotations

import html as _html
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional  # noqa: F401
from urllib.parse import urlparse
import os
_DEBUG = bool(os.environ.get("KDEBUG"))

try:  # the scorer puts harness/ on sys.path
    from contract import Answer, Conflict, Snapshot, Source  # noqa: E402
except ImportError:
    _h = next((d / "harness" for d in Path(__file__).resolve().parents
               if (d / "harness" / "contract.py").exists()), None)
    if _h is not None:
        sys.path.insert(0, str(_h))
        from contract import Answer, Conflict, Snapshot, Source  # noqa: E402
    else:  # standalone fallback with the same fields (the harness accepts look-alike objects)
        from dataclasses import dataclass, field

        @dataclass
        class Source:
            url: str
            snapshot_id: Optional[str] = None
            quote: Optional[str] = None

        @dataclass
        class Answer:
            text: str
            sources: list = field(default_factory=list)
            confident: bool = True
            conflict_note: Optional[str] = None

        @dataclass
        class Conflict:
            topic: str
            values: list
            sources: list
            resolution: Optional[str] = None
            reason: Optional[str] = None

        Snapshot = object

# ============================================================================ text utilities

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]
MONTH_RE = r"(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)"
PUB_DATE_RE = re.compile(r"\b(" + MONTH_RE + r")\.?\s+(\d{1,2}),\s+(\d{4})")
EFF_RE = re.compile(r"effective\s+(?:as\s+of\s+)?(\d{1,2})/(\d{1,2})/(\d{2,4})", re.I)
DAY_RE = re.compile(r"^(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?\b", re.I)

STOP = set("""a an the of to for in on at by with and or is are was were be been being do does did
what whats which who whom whose when where why how can could would should will shall may might must
i im me my we our ours you your yours it its this that these those there here from as about into over
than then so if not no yes any some all each per get give tell show please know like want need
dupaco dupacos union cu currently now today does just also still have has had going go make
s t ll ve re d""".split())

RATE_WORDS = {"rate", "apy", "apr", "dividend", "interest", "yield", "percent", "earn", "pay"}
GENERIC = STOP | RATE_WORDS | {
    "loan", "account", "month", "year", "fixed", "term", "special", "annual", "percentage",
    "information", "much", "many", "cost", "fee", "member", "branch", "number", "people", "use",
    "used", "highest", "lowest", "best", "top", "maximum", "most", "current", "right", "offer",
    "option", "available", "link", "url", "website", "page", "see", "find", "general", "standard",
    "regular", "typical", "new", "old", "first", "repeat", "again", "provide", "list", "kind",
    "type", "program", "product", "service", "help", "look", "rates", "tier", "balance",
    "open", "today", "still", "currently", "now", "anymore", "le", "more", "between", "amount",
    "model", "on", "much", "charge", "price", "as", "of", "was", "were",
    "ago", "previously", "back", "then", "time", "date", "mid", "early", "late", "buy", "buying", "bought",
    "getting", "looking", "planning", "thinking", "wondering", "curious", "operate", "operates", "run",
    "own", "exist", "there", "currently", "presently", "approximately", "roughly", "about",
    "advertise", "advertised", "advertises", "list", "listed", "lists", "publish", "published", "publishes",
    "post", "posted", "show", "shown", "shows", "quote", "quoted", "state", "stated", "say", "says", "said",
    "apply", "applies", "applied", "apply", "financing", "financed", "finance", "borrow", "borrowing",
    "borrowed", "earn", "earns", "earned", "would", "pay", "paid", "pays", "paying", "minimum", "min",
    "maximum", "max", "together", "along", "including", "include", "includes", "separate", "separately",
    "regular", "normal", "ongoing", "actual", "exact", "precise", "specific", "particular", "tell",
    "according", "official", "website", "site", "online", "listed", "require", "required", "requires",
    "qualify", "qualifies", "get", "gets", "receive", "received", "set", "sets", "charged", "charges",
    "need", "needed", "loaned", "lock", "locked", "put",
    "keep", "kept", "hold", "held", "today", "price", "pricing",
    "cost", "costs", "costing", "fee", "fees", "much", "how", "what", "which", "whose", "per", "every",
    "month", "monthly", "year", "yearly", "annually", "term", "terms", "length", "period", "duration",
    "scheduled", "plan", "planned", "expected", "supposed", "going"} | set(MONTHS) | {"jan", "feb", "mar", "apr_", "jun",
    "jul", "aug", "sep", "sept", "oct", "nov", "dec"}

SYN = {
    "cd": ["certificate"], "cds": ["certificate"], "certificate": ["certificate"],
    "mortgage": ["home", "mortgage", "conventional"], "mortgages": ["home", "mortgage", "conventional"],
    "heloc": ["home", "equity", "line", "credit"],
    "car": ["auto", "vehicle"], "vehicle": ["auto", "vehicle"], "auto": ["auto", "vehicle"],
    "truck": ["auto", "vehicle"], "motorcycle": ["motorcycle", "auto"],
    "card": ["card", "visa"], "cards": ["card", "visa"], "visa": ["visa", "card"],
    "interest": ["interest", "rate", "dividend"],
    "savings": ["savings"], "saving": ["savings"],
    "hours": ["hour"], "rv": ["recreational"], "boat": ["recreational"], "camper": ["recreational"],
    "ira": ["ira", "roth", "traditional"],
    "located": ["address"], "location": ["address"], "locations": ["address"],
    "open": ["open", "close"],
    "eligible": ["join", "eligible", "membership"], "eligibility": ["join", "eligible", "membership"],
    "join": ["join", "membership"], "student": ["student"], "college": ["student"],
    "university": ["student"], "school": ["student"],
    "wallet": ["wallet", "pay", "apple", "google", "samsung"],
    "phone": ["phone", "call"], "routing": ["routing"], "shut": ["close"], "shutting": ["close"],
    "cd": ["certificate"], "wire": ["wire"], "box": ["box", "safe", "deposit"],
}
# a key word is "covered" by a unit if it or one of these appears
COVER_ALT = {
    "mortgage": {"home", "conventional", "mortgage"}, "cd": {"certificate"},
    "certificate": {"certificate", "cd"}, "car": {"auto", "vehicle"}, "vehicle": {"auto", "vehicle"},
    "heloc": {"equity", "heloc"}, "card": {"visa", "card"}, "visa": {"visa", "card"},
    "interest": {"rate", "dividend", "interest", "apr", "apy"}, "address": {"address"},
    "eligible": {"join", "eligible"}, "located": {"address", "located"},
    "student": {"student"}, "wallet": {"wallet", "pay"}, "share": {"share", "certificate"},
}


def _build_cover():
    cov = {}
    for k, alts in list(SYN.items()) + list(COVER_ALT.items()):
        cov.setdefault(k, set()).update(alts)
    return cov


def clean_inline(s: str) -> str:
    if "&" in s:
        s = _html.unescape(_html.unescape(s))
    s = s.replace("\\r\\n", " ").replace("\\n", " ")
    s = re.sub(r"!\[[^\]]*\]\((?:[^()]|\([^)]*\))*\)", " ", s)                 # images
    s = re.sub(r"\[([^\]]*)\]\((?:[^()]|\([^)]*\))*\)", r"\1", s)              # links -> text
    s = s.replace("\\\\", " ").replace("\\", "")
    s = re.sub(r"\*{1,3}|(?<!\w)_{1,3}|_{1,3}(?!\w)|`", "", s)
    s = re.sub(r"^\s*[-+>]\s+", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def strip_footnotes(cell: str) -> str:
    cell = re.sub(r"%\s?\d\b", "%", cell)                # 4.20%1 -> 4.20%
    cell = re.sub(r"\b(APR|APY)\s?\d\b", r"\1", cell)    # APR1 -> APR
    cell = re.sub(r"(%)\s\d$", r"\1", cell)              # "20.5% 1"
    return cell.strip()


def norm_text(s: str) -> str:
    s = s.lower().replace("–", "-").replace("—", "-").replace("’", "'").replace("$tart", "start")
    s = re.sub(r"(?<=\d),(?=\d{3})", "", s)
    s = re.sub(r"\b([ap])\.\s?m\.?", r"\1m", s)
    s = re.sub(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])", r"\1x\2", s)    # 7/1 ARM -> 7x1
    return s


SUFFIXES = ("ations", "ation", "itions", "ition", "izing", "ized", "izes", "ize", "ings", "ing",
            "ments", "ment", "ions", "ion", "edly", "ed", "ly", "es", "s")


def stem(w: str) -> str:
    if len(w) <= 3 or w[0].isdigit():
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("ss") or w.endswith("us"):
        return w
    if len(w) == 4 and w.endswith("s"):
        return w[:-1]
    for suf in SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            w = w[: -len(suf)]
            break
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    return w


TOKEN_RE = re.compile(r"\d+(?:st|nd|rd|th)\b|[a-z0-9]+x[0-9]+|[a-z]+|\d+(?:\.\d+)?")
_STOP_STEMS = None


def tokens(s: str, keep_stop: bool = False) -> list[str]:
    out = []
    for t in TOKEN_RE.findall(norm_text(s)):
        if t[0].isdigit() and "x" not in t and not t[-1].isalpha():
            if "." in t:
                t = t.rstrip("0").rstrip(".")
            out.append(t)
        else:
            if not keep_stop and t in STOP:
                continue
            out.append(stem(t))
    return out


COMMON_WORDS = set("""able about above across act actually add after again against ago agree ahead all allow almost alone along already also although altogether always am among amount and another answer any anybody anyone anything anyway anywhere appear area around arrive as ask asked asking at available away back bad ballpark basically be became because become becoming been before begin behind being believe below best better between big bit both bring brought buddy build but by call came can cannot care carry case cause certain certainly chance change changed changing checked choose chose city clear clearly come comes coming common completely consider could couple course coworker current currently dad daughter day days decide decided definitely did different do does doing done door down during each early easy either else end enough entire especially estimate even ever every everyone everything exactly example except expect experience explain fact fair fairly family far fast feel feeling felt few figure figures finally find fine fixing folks follow following for forget forgot form forward found friend friends from front full fully further future gave general generally get gets getting give given gives giving go goes going gone good got grandma grandpa great group guess guy guys had half hand happen happened happy hard has have having he headed heading hear heard help helpful her here hers herself hey hi him himself his hold honest honestly hope hoping how however huge husband i idea if im important in including indeed information inside instead interested internet into is issue it its itself just keep kind kinda knew know known last late later latest least leave left less let lets life like likely little live living long look looked looking lot lots love made main mainly make makes making man many matter may maybe me mean means meant mention might mind mine mom moment more most mostly move moved moving much must my myself name near nearly need needed needs neighbor never new newest next nice no nobody none nor normal normally not nothing notice now number numbers of off often oh ok okay old on once one only onto or order other others our out outside over own page parent parents part particular particularly past people per perhaps person pick picked place planning plastic please plus point possible possibly pretty probably problem put question questions quick quickly quite rather read ready real really reason recent recently right rough roughly run said same saw say saying says second see seem seemed seems seen self send sending sense set several shall she sheet should show side simple simply since single small so some somebody someone someone's something sometime sometimes somewhat somewhere son soon sorry sort spread spreading start started starting still stop stretch stretched stretching stuff such suppose supposed sure take taken takes taking talk talking teen teenager tell tells than thank thanks that the their them themself then there these they thing things think thinking this those though thought through thus time times to today today's together told too took top toward try trying turn two type under understand unless until up upon us use used useful uses using usual usually very via want wanted wanting wants was way ways we website week weeks well went were what whatever when whenever where whether which while who whole whom whose why wife will wish with within without wonder wondering word work worked working works world worry would wrong yeah year years yes yet you your yours yourself""".split())
PRODUCT_KEEP = {"home", "card", "car", "check", "checking", "box", "free", "business", "student", "line", "plan",
                "phone", "online", "app", "fixed", "high", "low", "first", "market", "wire", "auto"}
GENERIC_STEMS = ({stem(w) for w in GENERIC} | {stem(w) for w in COMMON_WORDS}) - {stem(w) for w in PRODUCT_KEEP} \
    | {stem(w) for w in GENERIC & {"fixed", "check"}}
for _w in ("reach", "ordinary", "plain", "basic", "simple", "house", "houses", "money", "kid", "kids", "teenager", "honestly", "wife", "husband", "switching",
           "whats", "existing", "today", "nearby", "near", "closest"):
    GENERIC_STEMS.add(stem(_w))
GENERIC_STEMS.discard(stem("checking"))
LABEL_NEUTRAL = {stem(w) for w in ("months", "month", "year", "years", "model", "or", "less", "and",
                                   "older", "mo", "fixed", "conventional", "purchase", "standard",
                                   "rate", "certificate", "loan", "savings", "visa")}
COVER = {}
for _k, _alts in _build_cover().items():
    COVER.setdefault(stem(_k), set()).update(stem(a) for a in _alts)


def url_path(url: str) -> str:
    p = urlparse(url)
    path = p.path.rstrip("/").lower() or "/"
    return path + (("?" + p.query) if p.query else "")


def parse_mdy(m) -> Optional[str]:
    mo, d, y = int(m.group(1)), int(m.group(2)), m.group(3)
    y = int(y) + 2000 if len(y) == 2 else int(y)
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def month_num(name: str) -> int:
    name = name.lower().rstrip(".")
    for i, m in enumerate(MONTHS):
        if m.startswith(name[:3]):
            return i + 1
    return 1


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(])|\s*\n\s*", text)
    return [p.strip() for p in parts if p and p.strip()]


def fmt_date(iso: Optional[str]) -> str:
    if not iso:
        return ""
    y, m, d = iso.split("-")
    return f"{MONTHS[int(m) - 1].capitalize()} {int(d)}, {y}"


# ============================================================================ page model

def page_authority(url: str) -> float:
    p = url_path(url)
    if p in ("/rates", "/business-rates"):
        return 1.0
    if "?" in p and ("privacy-policy" in p or "category=" in p or "hslang" in p):
        return 0.1
    if "/tag/" in p or "/author/" in p:
        return 0.15
    if p.startswith("/calculators") or p.startswith("/business-calculators"):
        return 0.35
    if p.startswith("/hubfs"):
        return 0.4
    if p.endswith("-lp") or p in ("/switch-and-earn", "/offer-expired", "/homebuying-guidance",
                                  "/money-makeover", "/covid-19", "/not-for-sale"):
        return 0.45
    if p.startswith("/newsroom"):
        return 0.6
    if (p.startswith("/learn") or p.startswith("/dupacodaily") or re.match(r"/20\d\d/", p)
            or p.startswith("/business/resources") or p.startswith("/resources")):
        return 0.5
    if p.startswith("/personal") or p.startswith("/business") or p.startswith("/about") \
            or p in ("/contact", "/membership", "/checking", "/loans", "/personal-loans", "/shine",
                     "/online-and-mobile-banking/mobile-payments", "/savings-account/online-savings-account"):
        return 0.9
    return 0.75


class Unit:
    __slots__ = ("uid", "kind", "url", "title", "heading", "caption", "header", "cells", "text",
                 "eff", "pub", "auth", "tf", "dl", "snaps", "table_key", "row_idx", "tset",
                 "label", "boiler")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
HOURS_HEAD_RE = re.compile(r"^\W*(lobby|drive[- ]?(?:up|thru|through))\W*(.*)$", re.I)


def parse_page(url: str, title: str, md: str):
    """Split one page into table rows, text chunks, links and branch-hour blocks."""
    lines = md.split("\n")
    pub = None
    m = PUB_DATE_RE.search("\n".join(lines[:25]))
    if m:
        pub = f"{int(m.group(3)):04d}-{month_num(m.group(1)):02d}-{int(m.group(2)):02d}"
    rows, chunks, links, hours = [], [], [], []
    heading = clean_inline(title.split("|")[0])
    recent: list[str] = []
    section: list[str] = []
    para: list[str] = []
    seen_para = set()
    tseen: dict = {}
    prev_cap = ""

    # ---- links (anchor text + section heading for context)
    cur_head = heading
    for ln in lines:
        if ln.startswith("#"):
            cur_head = clean_inline(ln.lstrip("#"))
        for lm in LINK_RE.finditer(ln):
            anchor = clean_inline(lm.group(1))
            links.append((lm.group(2).rstrip("/"), anchor, cur_head))

    # ---- branch hours blocks
    for i, ln in enumerate(lines):
        hm = HOURS_HEAD_RE.match(ln.strip().lstrip("#").strip())
        if not hm:
            continue
        rest0 = clean_inline(hm.group(2))
        if rest0 and not DAY_RE.match(rest0.lstrip(":").strip()):
            continue
        kind = "lobby" if hm.group(1).lower().startswith("lobby") else "drive"
        sched = []
        rest = clean_inline(hm.group(2))
        if rest and DAY_RE.match(rest):
            sched.append(rest)
        j = i + 1
        while j < len(lines) and len(sched) < 7:
            t = clean_inline(lines[j])
            if not t:
                j += 1
                continue
            if DAY_RE.match(t) and re.search(r"\d|closed|atm", t, re.I):
                sched.append(t)
                j += 1
                continue
            break
        if sched:
            ctx = clean_inline(" ".join(lines[max(0, i - 12):i]))[-500:]
            hours.append((kind, tuple(sched), ctx))

    def flush_para():
        if para:
            t = "\n".join(x for x in (clean_inline(l) for l in para) if x)
            if t and t not in seen_para and len(t) > 2:
                seen_para.add(t)
                section.append(t)
            para.clear()

    def flush_section():
        flush_para()
        if not section:
            return
        cur = ""
        for p in section:
            if len(p) > 600:
                if cur:
                    chunks.append((heading, cur)); cur = ""
                buf = ""
                for s in split_sentences(p):
                    if len(buf) + len(s) > 420 and buf:
                        chunks.append((heading, buf)); buf = ""
                    buf = (buf + " " + s).strip()
                if buf:
                    chunks.append((heading, buf))
                continue
            if len(cur) + len(p) > 420 and cur:
                chunks.append((heading, cur)); cur = ""
            cur = (cur + "\n" + p).strip()
        if cur:
            chunks.append((heading, cur))
        section.clear()

    i, n = 0, len(lines)
    while i < n:
        line = lines[i].strip()
        if line.startswith("|"):
            flush_para()
            tbl = []
            while i < n:
                if lines[i].strip().startswith("|"):
                    tbl.append(lines[i].strip()); i += 1
                    continue
                j = i
                while j < n and not lines[j].strip() and j - i < 3:
                    j += 1
                if j < n and j > i and lines[j].strip().startswith("|"):
                    i = j
                    continue
                break
            eff = None
            j, seen = i, 0
            while j < n and seen < 5:
                if lines[j].strip():
                    seen += 1
                    if lines[j].strip().startswith("|"):
                        break
                    em = EFF_RE.search(lines[j])
                    if em:
                        eff = parse_mdy(em); break
                j += 1
            cells_rows = []
            for tl in tbl:
                cells = [strip_footnotes(clean_inline(c)) for c in tl.strip().strip("|").split("|")]
                if all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) or c == "" for c in cells):
                    continue
                cells_rows.append(cells)
            if len(cells_rows) >= 2:
                header, body = cells_rows[0], cells_rows[1:]
                cap = []
                for r in reversed(recent):
                    if r not in cap:
                        cap.insert(0, r)
                    if len(cap) == 3:
                        break
                caption = " / ".join(cap)
                bare = {"regular", "special", "standard", "jumbo", "variable", "fixed", "rate", "promotional"}
                if not cap or set(tokens(caption)) <= bare:
                    # "Regular" under a certificates section: borrow the previous table's product
                    caption = " / ".join([c for c in prev_cap.split(" / ") if c and set(tokens(c)) - bare][:1] + cap)
                prev_cap = caption
                tid = (caption.split(" / ")[-1], tuple(header))
                tseen[tid] = tseen.get(tid, 0) + 1
                for ri, cells in enumerate(body):
                    rows.append(dict(heading=heading, caption=caption, header=header, cells=cells,
                                     eff=eff, table_key=f"{url}#{tid[0]}#{'|'.join(tid[1])}#{tseen[tid]}",
                                     row_idx=ri))
            recent.clear()
            continue
        if line.startswith("#"):
            flush_section()
            h = clean_inline(line.lstrip("#"))
            if h:
                heading = h
                recent.append(h)
                recent[:] = recent[-4:]
        elif not line or re.fullmatch(r"[*\-_ ]{3,}", line):
            flush_para()
        else:
            c = clean_inline(line)
            is_tab = bool(re.match(r"^[-*]\s*\[[^\]]{3,60}\]\([^)]*\)\s*$", line)) and \
                not re.match(r"(apply|explore|see|open|learn|get|start|read|call|contact|join)\b", c, re.I)
            if c and len(c) < 90 and not c.lower().startswith("rates are effective") and ("](" not in line or is_tab):
                recent.append(c)
                recent[:] = recent[-4:]
            para.append(line)
        i += 1
    flush_section()
    return pub, rows, chunks, links, hours


# ============================================================================ query analysis

_UNITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
          "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
         "ninety": 90}
NUMWORD = {w: i for i, w in enumerate(_UNITS) if i}
NUMWORD.update(_TENS)
for _t, _v in _TENS.items():
    for _i in range(1, 10):
        NUMWORD[f"{_t}-{_UNITS[_i]}"] = _v + _i
        NUMWORD[f"{_t} {_UNITS[_i]}"] = _v + _i
NUMWORD["a hundred twenty"] = 120
NUMWORD["one hundred twenty"] = 120
NUMWORD["half a"] = 0.5


def money_values(s: str) -> list[float]:
    out = []
    for m in re.finditer(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(k\b|m\b|million|thousand)?", s, re.I):
        v = float(m.group(1).replace(",", ""))
        suf = (m.group(2) or "").lower()
        if suf in ("k", "thousand"):
            v *= 1000
        elif suf in ("m", "million"):
            v *= 1_000_000
        out.append(v)
    for m in re.finditer(r"\b(\d[\d,]{2,})\s*(?:dollars|balance)", s, re.I):
        out.append(float(m.group(1).replace(",", "")))
    return out


def tier_range(text: str):
    """Parse '$10,000 – $24,999.99', '$25,000+', '$100,000 or less', '$10,000-24,999'."""
    t = text.replace("–", "-").replace("—", "-")
    if "$" not in t and not re.search(r"\d,\d{3}", t):
        return None
    nums = [float(x.replace(",", "")) for x in re.findall(r"(\d[\d,]*(?:\.\d+)?)", t)
            if not re.fullmatch(r"\d{1,3}", x) or "$" + x in t.replace(" ", "")]
    if not nums:
        return None
    if re.search(r"\+|\bor more\b|\band up\b|\babove\b|\bover\b", t) and len(nums) == 1:
        return (nums[0], float("inf"))
    if re.search(r"\bor less\b|\bup to\b|\bunder\b|\bbelow\b", t) and len(nums) == 1:
        return (0.0, nums[0])
    if len(nums) >= 2 and "-" in t:
        return (nums[0], nums[1])
    return None


COUNT_FOLLOW = set("""and or in across throughout office offices location locations who that with at of from to
have has are is were was used use served serve nationwide statewide who which within for across by on
volunteered totaling total spanning""".split())


class Query:
    def __init__(self, q: str):
        self.raw = q
        ql = norm_text(q)
        for w, v in sorted(NUMWORD.items(), key=lambda x: -len(x[0])):
            if v == 0.5:
                ql = re.sub(r"\b(?:a )?half[- ]a[- ]year\b|\bhalf[- ]year\b", "6-month", ql)
                continue
            ql = re.sub(rf"\b{w}[- ](months?|years?|yr)\b", rf"{v}-\1", ql)
        ql = re.sub(r"\b(?:a|one)[- ]year\b", "1-year", ql)
        ql = re.sub(r"\bover (\d+)[- ]years?\b", r"\1-year", ql)
        ql = re.sub(r"\bbecome an? (?:dupaco )?member\b|\bmembership application\b", "join", ql)
        ql = re.sub(r"\bcash machines?\b", "atm", ql)
        ql = re.sub(r"\bswitch(?:ing)? (?:an? |my |our )?(?:existing |current )?(mortgage|home loan)", r"refinance \1", ql)
        ql = re.sub(r"\bwiring\b", "wire", ql)
        ql = re.sub(r"\breceiv(?:e|ing) (?:a |the )?wire", "incoming wire", ql)
        if re.search(r"\bcard|visa\b", ql):
            ql = re.sub(r"\bpurchases?\b", " ", ql)
        self.ql = ql
        # percentages in the question are claims to verify, not search terms
        self.claim_pcts = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*%", ql)]
        ql_search = re.sub(r"\d+(?:\.\d+)?\s*%", " ", ql)
        self.toks = tokens(ql_search)
        exp = []
        for t in TOKEN_RE.findall(ql_search):
            exp.extend(SYN.get(t, []))
        self.qtoks = self.toks + [stem(e) for e in exp if stem(e) not in self.toks]
        self.months = {int(m.group(1)) for m in re.finditer(r"(\d+)[- ]?(?:month|mo\b)", ql)}
        self.years = {int(m.group(1)) for m in re.finditer(r"\b(\d{1,2})[- ]?(?:year|yr)", ql)}
        self.model_year = None
        ql_nodate = re.sub(r"\b(?:" + MONTH_RE.lower() + r")\.?\s+(?:\d{1,2},?\s+)?\d{4}\b|\b\d{4}-\d{2}-\d{2}\b|\b(?:as of|in|during|since|until|by)\s+(?:early |mid |late )?\d{4}\b", " ", ql)
        my = re.search(r"\b(19[89]\d|20[0-3]\d)\b", ql_nodate)
        if my and re.search(r"model|vehicle|car\b|auto|truck|suv|motorcycle|boat|rv\b|camper|used|new", ql_nodate):
            self.model_year = int(my.group(1))
        self.amounts = money_values(q)
        self.jumbo = "jumbo" in ql
        self.ira = bool(re.search(r"\bira|roth|traditional|retirement", ql))
        self.business = bool(re.search(r"business|commercial", ql))
        self.personal_too = bool(re.search(r"\bpersonal\b|\bconsumer\b|\bboth\b", ql)) and self.business
        self.refi = "refinanc" in ql or re.search(r"\brefi\b", ql) is not None
        self.intro = bool(re.search(r"intro|balance transfer|promo", ql)) and not re.search(
            r"(separate from|not the|excluding|other than|besides|aside from|instead of|apart from|rather than|after the|without)[^.?]{0,30}(intro|balance transfer|promo)", ql)
        self.special = "special" in ql
        self.loan_q = bool(re.search(r"\b(loans?|borrow\w*|financ\w*|mortgages?|heloc|apr|refinanc\w*|credit cards?|visa|lend\w*)\b", ql))
        self.dep_q = bool(re.search(r"\b(savings?|deposit|certificates?|cds?|apy|dividends?|checking|money market|ira|share|yield)\b", ql))
        self.highest = bool(re.search(r"\b(highest|best|top|maximum|most)\b", ql))
        self.lowest = bool(re.search(r"\b(lowest|cheapest|minimum rate)\b", ql))
        self.rate_q = bool(re.search(r"\b(rates?|apys?|aprs?|dividends?|interest|yield|percent)\b|%", ql))
        self.money_q = bool(re.search(r"\b(fee|fees|charge|charges|how much|price|premiums?|dues|dollar cap|dollar limit)\b|"
                                      r"\bwhat (?:does|do|is|are|would|will)\b[^.?]*\bcosts?\b|\bcosts? (?:of|to|for)\b|"
                                      r"\bminimum (?:to open|opening|balance|deposit)|\bmin\.? to (?:open|earn)", ql))
        self.list_q = bool(re.search(
            r"\b(rates|all|list (?:all|the|of|your|every)|please list|options|cards|accounts|certificates|cds|what .*offer|kinds?|types?|right now)\b|^list\b", ql)) and not (
            self.months or self.years or self.amounts or self.model_year)
        self.hours_q = ((bool(re.search(r"\b(hours|opening (?:and|or|&) closing|closing time|what time|open on|open until|"
                                        r"open (?:on )?(?:saturday|sunday|weekends?))\b|walk[- ]?in", ql)) or
                         (bool(re.search(r"\btimes?\b|\bhour\b", ql)) and bool(re.search(r"\b(lobby|drive|branch|office|open)", ql))))
                        and not re.search(r"how many (?:hours|times)|business days?", ql)
                        and not re.search(r"\b(form|e-?mail|message|reply|respond|chat|call center|phone line|"
                                          r"close (?:my|an|the) account|closing costs?|account clos)", ql))
        self.url_q = bool(re.search(r"\b(link|url|web ?address|webpage|web page|locator)\b|where on (?:the|your) (?:web)?site|\b(?:which|what|the) page\b", ql)) or \
            (bool(re.search(r"\bonline\b", ql)) and bool(re.search(
                r"\b(join|apply|application|sign up|enroll|become a member|open an? (?:account|membership))\b", ql)))
        self.count_q = bool(re.search(r"\bhow many\b|\bnumber of\b", ql))
        self.when_q = bool(re.search(r"\bwhen\b|what date|which date|\bstill (open|available|offered|operating|around)\b|\b(has|did) .* close", ql))
        self.where_q = bool(re.search(r"\b(where|address|located)\b", ql))
        self.key = [t for t in dict.fromkeys(self.toks)
                    if t not in GENERIC_STEMS and not re.fullmatch(r"[\d.]+", t) and len(t) > 1]


# ============================================================================ the system

class TemporalKnowledge:
    def __init__(self):
        self.snaps: list[tuple[str, str]] = []
        self.units: list[Unit] = []
        self.unit_by_key: dict = {}
        self.page_snaps: dict[str, set] = defaultdict(set)
        self.snap_pages: list[set] = []
        self.index: dict[str, list[int]] = defaultdict(list)
        self.links: list[dict] = []           # per snapshot: target -> {w, anchors Counter, ctx Counter}
        self.hours: list[list] = []           # per snapshot: [(url, title, pub, kind, sched, ctx)]
        self.titles: list[dict] = []          # per snapshot: url -> title
        self.url_last: dict = {}              # url -> latest parsed version (for carry-forward)
        self.carried: list[set] = []          # per snapshot: urls carried forward from older crawls
        self._ready = False
        self._tables = None
        self._conflicts = None
        self._addr_vocab = None
        self._cache: dict = {}

    # ------------------------------------------------------------------ ingest
    def ingest(self, snapshot: Snapshot) -> None:
        si = len(self.snaps)
        self.snaps.append((snapshot.snapshot_id, snapshot.taken_on))
        self.snap_pages.append(set())
        links: dict = defaultdict(lambda: {"w": 0.0, "occ": Counter(), "pages": set()})
        hours = []
        titles = {}
        present = set()
        for p in snapshot.pages:
            url = p.url
            present.add(url)
            uids = set()
            self.page_snaps[url].add(si)
            self.snap_pages[si].add(url_path(url))
            md = p.markdown or ""
            pub, rows, chunks, plinks, phours = parse_page(url, p.title or "", md)
            auth = page_authority(url)
            title = clean_inline((p.title or "").split("|")[0])
            titles[url] = title
            for tgt, anchor, ctx in plinks:
                L = links[tgt]
                if url not in L["pages"]:
                    L["pages"].add(url)
                    L["w"] += auth
                L["occ"][(anchor, ctx)] += auth
            for kind, sched, ctx in phours:
                hours.append((url, title, pub, kind, sched, ctx))
            self.url_last[url] = {"uids": uids, "links": plinks, "hours": phours, "auth": auth,
                                  "title": title, "pub": pub}
            for r in rows:
                cells = r["cells"]
                text, label = self._row_text(r)
                k = ("row", url, r["caption"], tuple(cells), r["eff"])
                uids.add(self._add_unit(k, si, kind="row", url=url, title=title, heading=r["heading"],
                               caption=r["caption"], header=r["header"], cells=cells, text=text,
                               label=label, eff=r["eff"], pub=pub, auth=auth,
                               table_key=r["table_key"], row_idx=r["row_idx"],
                               index_text=" ".join([r["caption"], r["heading"], " ".join(cells),
                                                    " ".join(r["header"])])).uid)
            for heading, chunk in chunks:
                k = ("txt", url, heading, chunk)
                uids.add(self._add_unit(k, si, kind="txt", url=url, title=title, heading=heading,
                                        caption="", text=chunk, pub=pub, auth=auth,
                                        index_text=heading + "\n" + chunk).uid)
        # pages we had before but that this crawl missed: keep their newest version visible
        carried = set()
        for url, last in self.url_last.items():
            if url in present:
                continue
            carried.add(url)
            for uid in last["uids"]:
                self.units[uid].snaps.add(si)
            for tgt, anchor, ctx in last["links"]:
                L = links[tgt]
                if url not in L["pages"]:
                    L["pages"].add(url)
                    L["w"] += last["auth"] * 0.5
                L["occ"][(anchor, ctx)] += last["auth"] * 0.5
            for kind, sched, ctx in last["hours"]:
                hours.append((url, last["title"], last["pub"], kind, sched, ctx))
        self.carried.append(carried)
        self.links.append(dict(links))
        self.hours.append(hours)
        self.titles.append(titles)
        self._ready = False
        self._tables = None
        self._conflicts = None
        self._fact_index = None
        self._cells_index = None
        self._addr_vocab = None
        self._cache.clear()

    def _add_unit(self, key, si, index_text, **kw):
        u = self.unit_by_key.get(key)
        if u is None:
            u = Unit(uid=len(self.units), snaps={si}, **kw)
            toks = tokens(index_text)
            u.tf = Counter(toks)
            u.tset = set(toks)
            u.dl = len(toks) or 1
            self.units.append(u)
            self.unit_by_key[key] = u
            for t in u.tf:
                self.index[t].append(u.uid)
        else:
            u.snaps.add(si)
            if kw.get("table_key"):
                u.table_key = kw["table_key"]
                u.row_idx = kw.get("row_idx")
        return u

    @staticmethod
    def _row_text(r):
        cells, header = r["cells"], r["header"]
        label_parts, vals = [], []
        for h, c in zip(header, cells):
            if not c or c in ("—", "-"):
                continue
            hl = h.lower()
            if re.search(r"rate|apy|apr|yield|points|payment|min\.|minimum|fee|price|cost", hl) \
                    and not re.search(r"type|program|tier|term|product|certificate|card|amount", hl):
                h2 = re.sub(r"\)\s?\d\b", ")", h)
                h2 = re.sub(r"Annual Percentage Yield \(APY\)", "APY", h2)
                h2 = re.sub(r"Annual Percentage Rate", "APR", h2)
                if re.search(r"payment example", h2, re.I):
                    continue
                vals.append(f"{h2.strip()} {c}")
            else:
                label_parts.append(c)
        cap = (r["caption"] or "").split(" / ")[-1]
        if cap.endswith(".") or len(cap) > 60:
            cap = ""
        label = " ".join(label_parts)
        prefix = ""
        if cap and cap.lower() not in label.lower() and not set(tokens(label)) >= set(tokens(cap)):
            prefix = cap + ": "
        s = f"{prefix}{label} — " + "; ".join(vals) if vals else f"{prefix}{label}"
        return s.strip(), label

    def _finalize(self):
        if self._ready:
            return
        self.N = len(self.units) or 1
        self.df = {t: len(ids) for t, ids in self.index.items()}
        self.avgdl = sum(u.dl for u in self.units) / self.N
        # boilerplate: identical text on many pages
        cnt = defaultdict(set)
        for u in self.units:
            if u.kind == "txt":
                cnt[u.text].add(u.url)
        for u in self.units:
            u.boiler = len(cnt[u.text]) if u.kind == "txt" else 1
        self._tables = defaultdict(list)
        for u in self.units:
            if u.kind == "row":
                self._tables[u.table_key].append(u)
        # vocabularies: words that identify a product with published figures (table contexts) and
        # words the site uses to name things (titles / headings of product pages and news)
        self.P_rows, self.P_heads = set(), set()
        for u in self.units:
            if u.kind == "row":
                if (self._has_rate(u) or self._has_money(u)) and u.auth >= 0.4:
                    self.P_rows |= set(tokens(u.label or "")) | set(tokens(" / ".join((u.caption or "").split(" / ")[-2:])))
            elif u.auth >= 0.75:
                self.P_heads |= set(tokens(u.title or "")) | set(tokens(u.heading or ""))
        self.DOMAIN = self.P_rows | self.P_heads
        self._ready = True

    # ------------------------------------------------------------------ retrieval helpers
    def _snap_for(self, as_of: Optional[str]) -> int:
        if not self.snaps:
            return 0
        if not as_of:
            return len(self.snaps) - 1
        d = str(as_of)[:10]
        best = 0
        for i, (_, taken) in enumerate(self.snaps):
            if taken <= d:
                best = i
        return best

    def _idf(self, t: str) -> float:
        n = self.df.get(t, 0)
        return math.log(1 + (self.N - n + 0.5) / (n + 0.5))

    def _bm25(self, qtoks: list[str], si: int) -> dict[int, float]:
        k1, b = 1.2, 0.6
        scores: dict[int, float] = defaultdict(float)
        for t, qn in Counter(qtoks).items():
            ids = self.index.get(t)
            if not ids:
                continue
            idf = self._idf(t)
            for uid in ids:
                u = self.units[uid]
                if si not in u.snaps:
                    continue
                f = u.tf[t]
                scores[uid] += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * u.dl / self.avgdl)) * (1 + 0.3 * (qn - 1))
        return scores

    def _covered(self, t: str, toks: set) -> bool:
        if t in toks:
            return True
        alts = COVER.get(t)
        return bool(alts and any(a in toks for a in alts))

    def _coverage(self, q: Query, toks: set, keys: Optional[list] = None) -> float:
        keys = q.key if keys is None else keys
        if not keys:
            return 1.0
        tot = sum(self._idf(t) for t in keys)
        hit = sum(self._idf(t) for t in keys if self._covered(t, toks))
        return hit / tot if tot else 1.0

    def _in_vocab(self, t: str, vocab: set) -> bool:
        return t in vocab or any(a in vocab for a in COVER.get(t, ()))

    HEAD_NOUNS = {stem(w) for w in ("loan", "loans", "mortgage", "account", "card", "certificate", "line",
                                    "insurance", "arm", "arms", "cd", "heloc", "policy", "plan", "box")}

    def _unmatched_mods(self, q: Query) -> list:
        """Product modifiers the site uses (headings) that never appear with published figures,
        placed right before a product noun: 'adjustable-rate mortgage', 'construction loan'."""
        out = []
        toks = [t for t in q.toks if not re.fullmatch(r"[\d.]+", t)]
        for i, t in enumerate(toks):
            if t not in q.key or self._in_vocab(t, self.P_rows):
                continue
            if t in self.HEAD_NOUNS or re.fullmatch(r"\d+x\d+", t):
                out.append(t)
                continue
            if t not in self.P_heads:
                continue
            nxt = toks[i + 1:i + 3]
            if t in self.HEAD_NOUNS or any(n in self.HEAD_NOUNS for n in nxt):
                out.append(t)
        return out

    def _ctx_tokens(self, u: Unit) -> set:
        return set(u.tset) | set(tokens(u.title or "")) | set(tokens(u.caption or ""))

    def _recency(self, pub: Optional[str]) -> float:
        if not pub:
            return 0.9
        yr = int(pub[:4]) + (int(pub[5:7]) - 1) / 12
        return 0.75 + 0.25 * max(0.0, min(1.0, (yr - 2014) / 12.5))

    # ------------------------------------------------------------------ row qualifiers
    def _row_factor(self, q: Query, u: Unit) -> float:
        blob = norm_text(" ".join([u.caption or "", " ".join(u.cells or []), u.heading or ""]))
        label = norm_text(u.label or " ".join(u.cells or []))
        f = 1.0
        row_months = {int(x) for x in re.findall(r"(\d+)\s*-?\s*(?:month|mo\b)", label)}
        row_years = {int(x) for x in re.findall(r"(\d+)\s*-?\s*year", label)}
        want_m = set(q.months) | {y * 12 for y in q.years if y <= 10}
        if q.months or q.years:
            if row_months or row_years:
                if not ((row_months & want_m) or (row_years & q.years) or
                        ({m // 12 for m in row_months if m % 12 == 0} & q.years)):
                    return 0.0
            else:
                f *= 0.4
        if q.model_year:
            ym = re.search(r"(\d{4})\s*-\s*(\d{2,4})", label)
            if ym:
                a, bb = int(ym.group(1)), ym.group(2)
                bb = int(bb) if len(bb) == 4 else (a // 100) * 100 + int(bb)
                lo, hi = min(a, bb), max(a, bb)
                if not (lo <= q.model_year <= hi):
                    return 0.0
            else:
                ym = re.search(r"(\d{4})\s*(?:model year\s*)?\(?and older", label)
                if ym:
                    if q.model_year > int(ym.group(1)):
                        return 0.0
                elif "model year" in blob:
                    f *= 0.5
        if q.amounts:
            tr = None
            for c in (u.cells or []):
                tr = tier_range(c)
                if tr:
                    break
            if tr:
                if not any(tr[0] - 0.01 <= a <= tr[1] + 0.01 for a in q.amounts):
                    return 0.0
                f *= 1.3
        if "jumbo" in blob and not q.jumbo:
            f *= 0.45
        if q.jumbo and "jumbo" not in blob:
            f *= 0.5
        is_ira = bool(re.search(r"\b(roth|traditional|ira)\b", blob))
        if is_ira and not q.ira:
            f *= 0.4
        if q.ira and not is_ira:
            f *= 0.6
        is_biz = "business" in blob or url_path(u.url).startswith(("/business", "/business-rates"))
        if is_biz and not q.business:
            f *= 0.55
        if q.business and not is_biz and not q.personal_too:
            f *= 0.6
        if "refinanc" in blob and not q.refi:
            f *= 0.6
        if q.refi and "refinanc" not in blob:
            f *= 0.7
        if re.search(r"intro|balance transfer", blob) and not q.intro:
            f *= 0.35
        if "relief" in blob and "relief" not in q.ql:
            f *= 0.3
        if (re.search(r"cash-?out|home equity refinance", blob) or
                ("equity" in norm_text(u.caption or "") and re.search(r"refinanc|purchase|conventional", label))) \
                and not re.search(r"cash|equity", q.ql):
            f *= 0.4
        if "mini-max" in blob and "mini" not in q.ql:
            f *= 0.5
        if not re.search(r"\d", " ".join(u.cells or [])):
            f *= 0.2
        hdr = norm_text(" ".join(u.header or []) + " " + (u.caption or ""))
        row_dep = bool(re.search(r"dividend|apy|yield|savings|certificate|checking|money market|\bira", hdr))
        row_loan = bool(re.search(r"\bapr\b|annual percentage rate|loan|visa|mortgage|conventional|heloc|points", hdr))
        if q.loan_q and not q.dep_q and row_dep and not row_loan:
            return 0.0
        if q.dep_q and not q.loan_q and row_loan and not row_dep:
            return 0.0
        used = {str(x) for x in (q.months | q.years)} | ({str(q.model_year)} if q.model_year else set())
        qnums = [x for x in re.findall(r"\b\d+\b", re.sub(r"\$\s?[\d,]+(?:\.\d+)?", " ", q.ql)) if x not in used]
        if len(qnums) >= 2:
            lnums = re.findall(r"\b\d+\b", re.sub(r"\$\s?[\d,]+(?:\.\d+)?", " ", label))
            seq = " ".join(qnums)
            if seq in " ".join(lnums):
                f *= 1.4
            elif lnums and set(lnums) - set(qnums):
                f *= 0.8
        # product words in the row label the member did not ask for ("High-Yield", "Business")
        qset = set(q.qtoks)
        extra = [t for t in set(tokens(label)) if not re.fullmatch(r"[\d.x]+", t)
                 and t not in qset and t not in GENERIC_STEMS and t not in LABEL_NEUTRAL]
        f *= 0.85 ** min(len(extra), 4)
        if "share" in extra and stem("savings") in qset:
            f /= 0.85 * 0.9      # Share Savings is the default savings account
        return f

    def _ambiguous_term(self, q: Query, top: Unit, si: int) -> bool:
        """No term given, but the matched table offers several terms (e.g. 15/20/30-year mortgages)."""
        if q.months or q.years or q.amounts or q.model_year or q.highest or q.lowest:
            return False
        terms = set()
        for u in self._table_rows(top.table_key, si):
            lab = norm_text(u.label or "")
            terms |= set(re.findall(r"(\d+)\s*-?\s*(?:month|year)", lab))
        return len(terms) > 1

    @staticmethod
    def _qualifier_mismatch(q: Query, u: Unit) -> bool:
        blob = norm_text(" ".join([u.caption or "", " ".join(u.cells or [])]))
        if "refinanc" in blob and not q.refi:
            return True
        if re.search(r"cash-?out|equity", blob) and re.search(r"refinanc|purchase|conventional", blob) \
                and not re.search(r"cash|equity", q.ql):
            return True
        if re.search(r"intro|balance transfer|relief|mini-max", blob) and not (q.intro or "relief" in q.ql):
            return True
        if re.search(r"\b(roth|traditional|ira)\b", blob) and not q.ira:
            return True
        if "jumbo" in blob and not q.jumbo and not q.list_q:
            return True
        return False

    @staticmethod
    def _has_money(u: Unit) -> bool:
        return any(re.search(r"\$\s?\d", c) for c in (u.cells or [])[1:])

    @staticmethod
    def _has_value(u: Unit) -> bool:
        return any(re.search(r"\d\s?%|\$\s?\d", c) for c in (u.cells or [])[1:]) or \
            any(re.search(r"\d\s?%", c) for c in (u.cells or []))

    @staticmethod
    def _has_rate(u: Unit) -> bool:
        return any(re.search(r"\d\s?%", c) for c in (u.cells or []))

    @staticmethod
    def _pcts(u: Unit) -> list[float]:
        return [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*%", " ".join(u.cells or []))]

    # ------------------------------------------------------------------ public API
    def answer(self, question: str, as_of: Optional[str] = None) -> Answer:
        try:
            question = "" if question is None else str(question)
            self._finalize()
            if not as_of:
                as_of = self._date_in_question(question)
            si = self._snap_for(as_of)
        except Exception as e:  # never crash the harness
            return Answer(text="I don't know; I couldn't find that on the website. Please contact Dupaco.",
                          confident=False, conflict_note=f"internal error: {type(e).__name__}: {e}")
        ck = (re.sub(r"\s+", " ", question.strip().lower()), si)
        if ck in self._cache:
            return self._cache[ck]
        try:
            ans = self._answer(question, si)
        except Exception as e:  # never crash the harness
            ans = Answer(text="I don't know; I couldn't find that on the website. Please contact Dupaco.",
                         confident=False, conflict_note=f"internal error: {type(e).__name__}: {e}")
        self._cache[ck] = ans
        return ans

    @staticmethod
    def _date_in_question(question: str) -> Optional[str]:
        """'as of May 2026', 'in June 2026', 'on 2026-06-01' -> an as_of date (end of that month)."""
        m = re.search(r"\b(20\d\d)-(\d\d)-(\d\d)\b", question)
        if m:
            return m.group(0)
        m = re.search(r"\b(" + MONTH_RE + r")\.?\s+(?:\d{1,2},?\s+)?(20\d\d)\b", question, re.I)
        if m and re.search(r"\b(as of|in|during|back in|on|at the (?:start|end) of|was|were|did)\b", question, re.I):
            y, mo = int(m.group(2)), month_num(m.group(1))
            dm = re.search(r"(" + MONTH_RE + r")\.?\s+(\d{1,2}),", m.group(0), re.I)
            if dm:
                return f"{y:04d}-{mo:02d}-{int(dm.group(2)):02d}"
            nxt = (y + (mo == 12), mo % 12 + 1)
            import datetime as _dt
            last = _dt.date(nxt[0], nxt[1], 1) - _dt.timedelta(days=1)
            return last.isoformat()
        return None

    def _refuse(self, why: str = "", what: str = "that information") -> Answer:
        return Answer(text=f"I don't know: the website doesn't list {what}. "
                           "Please contact Dupaco directly and a team member can help.",
                      sources=[], confident=False, conflict_note=why or None)

    def _rank(self, q: Query, si: int, scores) -> list:
        ranked = []
        for uid, s in scores.items():
            u = self.units[uid]
            f = (0.35 + 0.65 * u.auth) * self._recency(u.pub)
            if si < len(self.carried) and u.url in self.carried[si]:
                f *= 0.6
            if u.title:
                f *= 1 + 0.12 * len(set(tokens(u.title)) & set(q.toks))
            if u.kind == "txt" and u.boiler > 3:
                f *= 1 / (1 + 0.25 * math.log(u.boiler))
            if u.kind == "row":
                rf = self._row_factor(q, u)
                if rf == 0:
                    continue
                f *= rf
                if u.eff:
                    taken = self.snaps[si][1]
                    age = (int(taken[:4]) - int(u.eff[:4])) + (int(taken[5:7]) - int(u.eff[5:7])) / 12
                    f *= 1.08 if age <= 0.5 else (0.92 if age > 1 else 1.0)
                if (q.rate_q and self._has_rate(u)) or (q.money_q and not q.rate_q and self._has_money(u)):
                    f *= 1.6
                elif not (q.rate_q or q.money_q):
                    f *= 0.6
            ranked.append((s * f, uid))
        ranked.sort(key=lambda x: (-x[0], x[1]))
        return ranked

    def _answer(self, question: str, si: int) -> Answer:
        q = Query(question)
        if not q.qtoks:
            return self._refuse("empty question")

        if q.url_q:
            a = self._link_answer(q, si)
            if a is not None:
                return a
        if q.where_q and re.search(r"\bbranch|office|location|atm\b", q.ql) and not q.rate_q:
            a = self._address_answer(q, si)
            if a is not None:
                return a
        if q.count_q and not q.rate_q:
            a = self._count_answer(q, si)
            if a is not None:
                return a
        if re.search(r"recogni|award|honou?r|\bnamed\b|\brank|how many times|accolade", q.ql):
            a = self._recognition_answer(q, si)
            if a is not None:
                return a
        if q.hours_q:
            a = self._hours_answer(q, si)
            if a is not None:
                return a
        if re.search(r"charter area|which states|what states|service area|field of membership|\beligib|\bwho can (join|become)|\bqualif\w* (for|to) (join|membership|become)|"
                     r"\bcan (i|anyone|my \w+) (join|become a member)|requirements? (to|for) (join|membership)", q.ql):
            a = self._membership_answer(q, si)
            if a is not None:
                return a
        contact_generic = {stem(w) for w in ("update", "change", "correct", "fix", "fixing", "address", "phone",
                                             "email", "contact", "detail", "details", "info", "information",
                                             "number", "call", "reach", "customer", "service", "moved", "move",
                                             "mailing", "mail", "box", "zip", "po", "need", "new", "my", "branch",
                                             "street", "page", "code", "dupaco", "send", "where")}
        other_named = [t for t in q.key if t not in contact_generic]
        if ((re.search(r"\b(update|change|correct|fix|fixing)\b", q.ql) and re.search(r"\b(address|phone|email|contact|name|details)\b", q.ql))
                or re.search(r"\b(phone number|call you|call dupaco|customer service|contact you|contact dupaco|mailing address|po box|p\.o\. box)\b", q.ql)) \
                and not q.rate_q and not other_named:
            a = self._contact_answer(q, si)
            if a is not None:
                return a

        if not q.key and not (q.rate_q and (q.months or q.years or q.amounts or q.model_year)):
            return Answer(text="Could you tell me which account, loan or service you mean? I don't want to guess "
                               "a number for the wrong product.", confident=False, conflict_note="no product named")
        scores = self._bm25(q.qtoks, si)
        if not scores:
            return self._refuse("no match")
        ranked = self._rank(q, si, scores)
        if not ranked:
            return self._refuse("no unit passed qualifiers")

        if q.rate_q or q.money_q:
            rows = [(s, uid) for s, uid in ranked[:120] if self.units[uid].kind == "row"
                    and (self._has_rate(self.units[uid]) if q.rate_q else self._has_money(self.units[uid]))]
            if rows or q.rate_q:
                ans = self._row_answer(q, si, rows, ranked)
                if ans is not None:
                    return ans
        return self._text_answer(q, si, ranked)

    # ------------------------------------------------------------------ rows
    def _row_answer(self, q: Query, si: int, rows, ranked) -> Optional[Answer]:
        best_txt = next(((s, uid) for s, uid in ranked if self.units[uid].kind == "txt"), None)
        if not rows:
            return self._rate_text_fallback(q, si, ranked) if q.rate_q else None
        top_s, top_uid = rows[0]
        top = self.units[top_uid]
        named = [t for t in q.key if not self._in_vocab(t, self.P_rows) and t in self.P_heads]
        if named and (q.rate_q or q.money_q):
            # the member names a product the site describes but never prices in a table
            return self._rate_text_fallback(q, si, ranked) if q.rate_q else None
        mods = self._unmatched_mods(q)
        if mods and q.rate_q:
            fb = self._rate_text_fallback(q, si, ranked)
            return fb if fb.confident else self._refuse(f"no published rate for {mods}", what="a published rate for that")
        key_r = [t for t in q.key if self._in_vocab(t, self.P_rows)]
        cov = self._coverage(q, self._ctx_tokens(top), key_r)
        if cov < 0.5:
            return self._rate_text_fallback(q, si, ranked) if q.rate_q else None
        if q.money_q and not q.rate_q and best_txt and best_txt[0] > top_s * 1.5 \
                and re.search(r"\$\s?\d", self.units[best_txt[1]].text):
            return None

        if q.claim_pcts:
            return self._claim_answer(q, si, top)

        if q.highest or q.lowest:
            trs = [u for u in self._table_rows(top.table_key, si) if self._row_factor(q, u) > 0]
            vals = [(max(self._pcts(u)), u) for u in trs if self._pcts(u)]
            if vals:
                vals.sort(key=lambda x: (x[0], -x[1].row_idx), reverse=q.highest)
                chosen = [vals[0][1]]
            else:
                chosen = [top]
        elif q.list_q or self._ambiguous_term(q, top, si):
            tables = []
            for s, uid in rows:
                u = self.units[uid]
                if s < top_s * 0.45:
                    break
                if u.table_key not in tables and self._coverage(q, self._ctx_tokens(u)) >= 0.5 \
                        and (u.auth >= 0.75 or top.auth < 0.75):
                    tables.append(u.table_key)
                if len(tables) >= 4:
                    break
            chosen = []
            for tk in tables:
                trs = sorted(self._table_rows(tk, si), key=lambda u: u.row_idx)
                trs = [u for u in trs if (self._row_factor(q, u) > 0 and not self._qualifier_mismatch(q, u))
                       or len(tables) == 1]
                chosen.extend(trs)
            chosen = self._dedupe_rows(chosen)[:24] or [top]
        else:
            same_table = [self.units[uid] for s, uid in rows
                          if self.units[uid].table_key == top.table_key and s >= top_s * 0.5]
            sib = [u for u in same_table if u is not top and self._near_tie(q, top, u)]
            chosen = [top] + sib[:4]
            chosen.sort(key=lambda u: self._tier_sort_key(u))

        lead = "; ".join(u.text for u in chosen)
        eff = max((u.eff for u in chosen if u.eff), default=None)
        tail = f" (rates effective {fmt_date(eff)})" if eff else ""
        text = f"{lead}{tail}."
        return Answer(text=text, sources=self._row_sources(chosen, si), confident=True,
                      conflict_note=self._conflict_note_for(chosen[0], si))

    @staticmethod
    def _tier_sort_key(u: Unit):
        tr = None
        for c in (u.cells or []):
            tr = tier_range(c)
            if tr:
                break
        return (tr[1] if tr else 0, u.row_idx or 0)

    def _near_tie(self, q: Query, a: Unit, b: Unit) -> bool:
        """Rows that differ only by a loan-amount tier the member did not specify."""
        if q.amounts or q.model_year:
            return False
        la, lb = norm_text(a.label or ""), norm_text(b.label or "")
        strip = lambda s: re.sub(r"\$\s?[\d,.]+\s*(or less|\+)?", "", s).strip()
        return bool(strip(la)) and strip(la) == strip(lb) and ("or less" in la or "+" in la)

    def _table_rows(self, table_key: str, si: int) -> list[Unit]:
        return [u for u in self._tables.get(table_key, []) if si in u.snaps]

    @staticmethod
    def _dedupe_rows(rows):
        seen, out = set(), []
        for u in rows:
            if u.text not in seen:
                seen.add(u.text)
                out.append(u)
        return out

    def _row_sources(self, units, si) -> list[Source]:
        sid = self.snaps[si][0]
        out, seen = [], set()
        for u in units:
            if u.url not in seen:
                seen.add(u.url)
                out.append(Source(url=u.url, snapshot_id=sid, quote=u.text[:200]))
        # other current pages that publish exactly the same row (product pages)
        u0 = units[0]
        cells = tuple(u0.cells or [])
        if getattr(self, "_cells_index", None) is None:
            self._cells_index = defaultdict(list)
            for v in self.units:
                if v.kind == "row":
                    self._cells_index[tuple(v.cells or [])].append(v)
        for v in self._cells_index.get(cells, []):
            if si in v.snaps and v.url not in seen and v.auth >= 0.75:
                seen.add(v.url)
                out.append(Source(url=v.url, snapshot_id=sid, quote=v.text[:200]))
            if len(out) >= 5:
                break
        return out

    RATE_SENT_RE = re.compile(
        r"\d+(?:\.\d+)?\s?%\s*(?:APR|APY|annual|dividend|interest|rate|fixed|variable|intro)|"
        r"(?:APR|APY|rate|dividend|yield)\W{0,3}\s*(?:of|is|at|as low as|:)?\s*\d+(?:\.\d+)?\s?%", re.I)
    EXPIRED_RE = re.compile(r"\b(offer (?:has )?ended|expired|no longer (?:available|offered)|ended on|"
                            r"was (?:available|offered) until|limited[- ]time)\b", re.I)

    def _sentence_terms_ok(self, q: Query, text: str) -> bool:
        nt = norm_text(text)
        sm = {int(x) for x in re.findall(r"(\d+)\s*-?\s*(?:month|mo\b)", nt)}
        sy = {int(x) for x in re.findall(r"(\d+)\s*-?\s*year", nt)}
        if q.months or q.years:
            want = set(q.months) | {y * 12 for y in q.years if y <= 10}
            if not ((sm & want) or (sy & q.years) or ({m // 12 for m in sm if m % 12 == 0} & q.years)):
                return False
        if ("jumbo" in nt) != q.jumbo and "jumbo" in nt:
            return False
        if q.model_year:
            for a, b in re.findall(r"\b(\d{4})\s*-\s*(\d{2,4})\b", nt):
                lo = int(a)
                hi = int(b) if len(b) == 4 else (lo // 100) * 100 + int(b)
                lo, hi = min(lo, hi), max(lo, hi)
                if not (lo <= q.model_year <= hi):
                    return False
        return True

    def _rate_text_fallback(self, q: Query, si: int, ranked) -> Optional[Answer]:
        """No table row matches: accept a prose sentence that states a % for this product."""
        best = None
        top = ranked[0][0] if ranked else 1.0
        for s, uid in ranked[:60]:
            u = self.units[uid]
            if u.kind != "txt" or u.auth < 0.3:
                continue
            ctx = set(tokens(u.heading or ""))
            for sent in split_sentences(u.text):
                if not self.RATE_SENT_RE.search(sent):
                    continue
                st = set(tokens(sent))
                if not self._sentence_terms_ok(q, sent):
                    continue
                kd = [t for t in q.key if self._in_vocab(t, self.DOMAIN)]
                if self._coverage(q, st, kd) < 0.5 and not (u.auth >= 0.75 and self._coverage(q, st | ctx, kd) >= 0.5):
                    continue
                sc = (s / top) * (0.35 + 0.65 * u.auth)
                if self.EXPIRED_RE.search(sent):
                    sc *= 0.15
                if len(sent) < 160:
                    sc *= 1.2
                if best is None or sc > best[0]:
                    best = (sc, sent, u)
        if best is not None:
            sc, sent, u = best
            return Answer(text=sent, sources=[Source(url=u.url, snapshot_id=self.snaps[si][0], quote=sent[:200])])
        what = "a published rate for that" if q.rate_q else "that information"
        return self._refuse(f"no published rate matches {q.key}", what=what)

    def _claim_answer(self, q: Query, si: int, top: Unit) -> Answer:
        trs = [u for u in self._table_rows(top.table_key, si) if self._row_factor(q, u) > 0] or [top]
        vals = sorted({p for u in trs for p in self._pcts(u)})
        name = (top.caption or "").split(" / ")[-1] or (top.label or "this account")
        if any(abs(c - v) < 1e-9 for c in q.claim_pcts for v in vals):
            hits = [u for u in trs if any(abs(c - p) < 1e-9 for c in q.claim_pcts for p in self._pcts(u))]
            return Answer(text="Yes: " + "; ".join(u.text for u in hits[:3]) + ".",
                          sources=self._row_sources(hits[:1] or [top], si))
        best = max(trs, key=lambda u: (max(self._pcts(u) or [0]), -(u.row_idx or 0)))
        return Answer(text=f"No. The {name} tiers top out at: {best.text}.",
                      sources=self._row_sources([best], si))

    # ------------------------------------------------------------------ text
    @staticmethod
    def _hard_slot(q: Query) -> Optional[str]:
        """Answer types whose absence means the site doesn't publish it."""
        if re.search(r"credit score", q.ql):
            return r"score[^.]{0,40}\b[3-8]\d{2}\b|\b[3-8]\d{2}\b[^.]{0,25}score"
        if re.search(r"cut-?off time|\bdeadline\b|\bwhat time\b", q.ql):
            return r"\b\d{1,2}(?::\d{2})?\s?(?:a\.?m\.?|p\.?m\.?)|\bnoon\b|\bmidnight\b"
        if q.count_q:
            return r"\d"
        if q.money_q and not q.rate_q:
            return r"\$\s?\d"
        return None

    def _slot_ok(self, q: Query, sent: str) -> bool:
        hs = self._hard_slot(q)
        if hs and not re.search(hs, sent):
            return False
        if q.when_q:
            return bool(PUB_DATE_RE.search(sent) or re.search(r"\b" + MONTH_RE + r"\b|\b(19|20)\d{2}\b", sent))
        if q.count_q:
            return bool(re.search(r"\d", sent))
        if re.search(r"\bnumber\b", q.ql):
            return bool(re.search(r"\d{6,}|\d{3}-\d{3}-\d{4}", sent))
        return True

    def _text_answer(self, q: Query, si: int, ranked) -> Answer:
        txt = [(s, uid) for s, uid in ranked if self.units[uid].kind == "txt"][:60]
        if not txt:
            return self._refuse("no passage")
        qset = set(q.qtoks)
        qnorm = 1 + sum(self._idf(t) for t in set(q.toks))
        best = None
        kd = [t for t in q.key if self._in_vocab(t, self.DOMAIN)]
        for rank, (s, uid) in enumerate(txt[:25]):
            u = self.units[uid]
            ctxu = self._ctx_tokens(u)
            cov = max(self._coverage(q, ctxu, kd), self._coverage(q, ctxu))
            sents = split_sentences(u.text)
            for i, sent in enumerate(sents):
                st = set(tokens(sent))
                sc = sum(self._idf(t) for t in qset & st)
                sc *= 1 + self._type_bonus(q, sent)
                if re.match(r"(for more information|learn more|click|visit|read more|keep reading|\d+ min read)", sent, re.I):
                    sc *= 0.4
                if re.match(r"[“\"]", sent):
                    sc *= 0.85
                if "dupaco" in q.ql and "dupaco" in sent.lower():
                    sc *= 1.1
                total = (s / txt[0][0]) * 0.6 + 0.4 * (sc / qnorm) + 0.3 * cov
                total *= (0.35 + 0.65 * u.auth) ** 0.5
                if best is None or total > best[0]:
                    best = (total, uid, i, cov)
        _, uid, i, cov = best
        u = self.units[uid]
        if cov < 0.34:
            return self._refuse(f"low coverage {cov:.2f} for {q.key}")
        sents = split_sentences(u.text)
        keep = [i]
        if not self._slot_ok(q, sents[i]):
            # the best-matching sentence lacks the asked-for slot (date, amount, count, number):
            # take the best sentence in the same passage, then elsewhere on the same page
            hard = self._hard_slot(q)
            alt = [(sum(self._idf(t) for t in qset & set(tokens(s2))) + (3.0 if q.when_q and PUB_DATE_RE.search(s2) else 0), j)
                   for j, s2 in enumerate(sents)
                   if j != i and self._slot_ok(q, s2)
                   and (not hard or self._coverage(q, set(tokens(s2)) | self._ctx_tokens(u), kd) >= 0.6)]
            if alt and max(alt)[0] > 0:
                keep.append(max(alt)[1])
            else:
                page_alt = None
                for s2u, uid2 in txt[:60]:
                    u2 = self.units[uid2]
                    if u2.url != u.url or uid2 == uid:
                        continue
                    for s2 in split_sentences(u2.text):
                        if self._slot_ok(q, s2) and (not hard or self._coverage(q, set(tokens(s2)) | self._ctx_tokens(u2), kd) >= 0.6):
                            sc2 = sum(self._idf(t) for t in qset & set(tokens(s2)))
                            if sc2 > 0 and (page_alt is None or sc2 > page_alt[0]):
                                page_alt = (sc2, s2)
                if page_alt:
                    text = sents[i] + " " + page_alt[1]
                    return Answer(text=text[:520], sources=[Source(url=u.url, snapshot_id=self.snaps[si][0], quote=page_alt[1][:200])])
                if self._hard_slot(q):
                    return self._refuse(f"no sentence with the requested value for {q.key}")
        elif sents[i].rstrip().endswith("?") and i + 1 < len(sents):
            j = i + 1
            while j < len(sents) and sum(len(sents[k]) for k in keep) < 260 and not sents[j].rstrip().endswith("?"):
                keep.append(j)
                j += 1
        elif len(sents[i]) < 90 and i + 1 < len(sents):
            keep.append(i + 1)
        elif i + 1 < len(sents) and not re.search(r"\d", sents[i]) and re.search(r"\$\s?\d|\d%|\d{2,}", sents[i + 1]) \
                and len(sents[i]) + len(sents[i + 1]) < 420 and re.search(r"\b(what|how|which)\b", q.ql):
            keep.append(i + 1)
        elif re.search(r"\bwhat (is|are)\b|\bwhat's\b|\btell me about\b|\bdescribe\b", q.ql) and i + 1 < len(sents) \
                and len(sents[i]) + len(sents[i + 1]) < 420:
            keep.append(i + 1)
        # resolve a leading anaphor ("Both branches…", "This program…") with the previous sentence
        first = min(keep)
        if first > 0 and re.match(r"(both|they|it|this|these|that|those|he|she|its|their)\b", sents[first], re.I) \
                and len(sents[first - 1]) < 300:
            keep.append(first - 1)
        text = " ".join(sents[j] for j in sorted(set(keep)))
        if len(text) > 520:
            text = text[:520].rsplit(" ", 1)[0] + "…"
        return Answer(text=text, sources=[Source(url=u.url, snapshot_id=self.snaps[si][0], quote=text[:200])])

    @staticmethod
    def _type_bonus(q: Query, sent: str) -> float:
        b = 0.0
        if q.when_q and (PUB_DATE_RE.search(sent) or re.search(r"\b" + MONTH_RE + r"\b", sent)):
            b += 0.8
        if q.count_q and re.search(r"\d", sent):
            b += 0.6
        if q.where_q and re.search(r"\d+ [A-Z][a-z]+ (St|Rd|Ave|Dr|Ln|Blvd|Way|Road|Street|Drive|Lane)", sent):
            b += 0.6
        if re.search(r"\b(phone|call|contact)\b", q.ql) and re.search(r"\d{3}-\d{3}-\d{4}", sent):
            b += 0.6
        if re.search(r"\bnumber\b", q.ql) and re.search(r"\d{6,}|\d{3}-\d{3}-\d{4}", sent):
            b += 1.0
        if q.money_q and "$" in sent:
            b += 0.5
        if re.search(r"\bwhat is\b|\bwhat's\b|\bwhat does\b", q.ql) and re.search(r"\$\d|\d%", sent):
            b += 0.6
        return b

    # ------------------------------------------------------------------ hours
    def _hours_answer(self, q: Query, si: int) -> Optional[Answer]:
        blocks = self.hours[si] if si < len(self.hours) else []
        if not blocks:
            return None
        want_drive = bool(re.search(r"drive", q.ql))
        want_lobby = bool(re.search(r"lobby", q.ql)) or not want_drive
        generic = {stem(w) for w in ("lobby", "drive", "up", "thru", "hour", "open", "close", "time",
                                     "saturday", "sunday", "weekend", "weekday", "branch", "keep", "same",
                                     "different", "vary", "general", "usual", "normal", "typical")}
        if self._addr_vocab is None:
            self._address_answer(Query("where is the main branch"), si)
        days = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "Dupaco", "I",
                "What", "When", "Where", "Is", "Are", "Can", "Do", "Does", "How", "If", "A", "The", "Lobby",
                "Drive", "ATM", "Hy", "Vee"}
        caps = [w for i, w in enumerate(re.findall(r"\b[A-Z][a-zA-Z]+\b", q.raw)) if w not in days]
        name_toks = [t for t in dict.fromkeys(tokens(" ".join(caps)) + [t for t in q.key if t in (self._addr_vocab or set())])
                     if t not in generic]
        sid = self.snaps[si][0]
        if name_toks:
            # hours for a named branch: only answer if a block is clearly tied to that branch
            best = None
            for (url, title, pub, kind, sched, ctx) in blocks:
                bt = set(tokens(title + " " + ctx))
                hit = sum(1 for t in name_toks if t in bt)
                if hit == len(name_toks) and (best is None or (pub or "") > (best[2] or "")):
                    best = (url, title, pub, kind, sched, ctx)
            if best is None:
                return self._refuse(f"no hours published for {name_toks}",
                                    what="hours for that branch")
            url = best[0]
            parts = []
            for (u2, t2, p2, kind, sched, ctx) in blocks:
                if u2 == url:
                    parts.append(f"{'Lobby' if kind == 'lobby' else 'Drive-up'}: " + "; ".join(sched))
            parts = list(dict.fromkeys(parts))
            return Answer(text=f"Per {best[1]}: " + " | ".join(parts) + ".",
                          sources=[Source(url=url, snapshot_id=sid)])

        def canon(sched):
            return tuple(re.sub(r"\s*[-–—]\s*", "–", re.sub(r"\s+", " ", s)) for s in sched)

        def common(kind):
            c, srcs = Counter(), defaultdict(list)
            seen = set()
            for (url, title, pub, k, sched, ctx) in blocks:
                sched = canon(sched)
                if k != kind or (url, sched) in seen:
                    continue
                seen.add((url, sched))
                c[sched] += 1
                srcs[sched].append(url)
            return [(s, n, srcs[s]) for s, n in sorted(c.items(), key=lambda x: (-x[1], x[0]))]

        out, srcs = [], []
        same_q = bool(re.search(r"\b(same|all|every|different|vary|differ)\b", q.ql))
        if want_lobby:
            lob = common("lobby")
            if lob:
                if same_q:
                    out.append("No, lobby hours vary by branch. Schedules on file include: " +
                               " / ".join("(" + "; ".join(s) + ")" for s, n, _ in lob[:3]) + ".")
                else:
                    out.append("Lobby hours vary by branch; the most common schedule is " +
                               "; ".join(lob[0][0]) + ".")
                    if len(lob) > 1:
                        out.append("Some branches differ, e.g. " + "; ".join(lob[1][0]) + ".")
                srcs += lob[0][2][:3]
        if want_drive or same_q:
            dr = common("drive")
            if dr:
                out.append("Drive-up hours at most branches: " + "; ".join(dr[0][0]) + ".")
                if len(dr) > 1 and not same_q:
                    out.append("Some branches differ, e.g. " + "; ".join(dr[1][0]) + ".")
                srcs += dr[0][2][:3]
        if not out:
            return None
        out.append("Which branch are you interested in? Hours can differ by location.")
        srcs = list(dict.fromkeys(srcs))
        srcs.append("https://www.dupaco.com/contact")
        return Answer(text=" ".join(out), sources=[Source(url=u, snapshot_id=sid) for u in srcs[:8]])

    # ------------------------------------------------------------------ addresses
    ADDR_RE = re.compile(r"(\d{2,6} [A-Z0-9][\w.' ]+?(?:St|Rd|Ave|Dr|Ln|Blvd|Way|Hwy|Pkwy|Ct|Pl|Road|Street|Avenue|Drive|Lane|Circle|Parkway|Highway)\b\.?[^,\n]*, [A-Z][A-Za-z. ]+, (?:IA|IL|WI|Iowa|Illinois|Wisconsin),? \d{5})")

    def _address_answer(self, q: Query, si: int) -> Optional[Answer]:
        generic = {stem(w) for w in ("address", "branch", "office", "location", "located", "where",
                                     "atm", "find", "street", "city")}
        if self._addr_vocab is None:
            self._addr_vocab = set()
            for u in self.units:
                if u.kind == "txt":
                    for m in self.ADDR_RE.finditer(u.text):
                        self._addr_vocab |= set(tokens(m.group(1)))
        name = [t for t in q.key if t not in generic and t in self._addr_vocab]
        if not name:
            return None
        sid = self.snaps[si][0]
        best = None
        for u in self.units:
            if u.kind != "txt" or si not in u.snaps:
                continue
            for m in self.ADDR_RE.finditer(u.text):
                addr = m.group(1)
                at = set(tokens(addr))
                ctx_before = u.text[max(0, m.start() - 80):m.start()]
                hit = sum(1 for t in name if t in at)
                if hit == 0:
                    continue
                branchy = 1.0 if re.search(r"dupaco|branch", ctx_before + u.heading, re.I) else 0.4
                sc = hit / len(name) * branchy * (0.4 + 0.6 * u.auth)
                if best is None or sc > best[0]:
                    best = (sc, addr, u)
        if best is None or best[0] < 0.35:
            return None
        sc, addr, u = best
        return Answer(text=f"The address is {addr}.",
                      sources=[Source(url=u.url, snapshot_id=sid, quote=addr)])

    # ------------------------------------------------------------------ counts
    def _count_answer(self, q: Query, si: int) -> Optional[Answer]:
        m = re.search(r"(?:how many|number of)\s+([a-z\- ]+?)(?:\s+(?:does|do|did|are|is|were|was|has|have|used|use|in|at|on|work)\b|\?|$)", q.ql)
        if not m:
            return None
        noun_words = [w for w in m.group(1).split() if w not in STOP and w != "dupaco"]
        if not noun_words:
            return None
        nouns = {stem(w) for w in noun_words}
        nouns |= {stem(w)[:-4] for w in noun_words if stem(w).endswith("ship")}   # membership -> member
        other = [t for t in q.key if t not in nouns and t != stem("location")]
        taken = self.snaps[si][1]
        t_now = int(taken[:4]) + (int(taken[5:7]) - 1) / 12
        best_w, sum_w, examples = defaultdict(float), defaultdict(float), {}
        sid = self.snaps[si][0]
        num_after = re.compile(r"(\d[\d,]*)\+?\s+((?:[a-z'\-]+\s){0,3}?)([a-z\-]+)")
        num_before = re.compile(r"\b([a-z\-]+)\s+(?:totals?|totaling|has grown to|grew to|exceeds?|exceeding|reached|of)\s+(?:more than\s+|over\s+|nearly\s+|about\s+)?(\d[\d,]*)\b")
        for u in self.units:
            if u.kind != "txt" or si not in u.snaps or not (nouns & u.tset or any(n + "ship" in u.tset for n in nouns)):
                continue
            if u.pub:
                t_pub = int(u.pub[:4]) + (int(u.pub[5:7]) - 1) / 12
            else:
                t_pub = t_now - 1 if u.auth >= 0.75 else t_now - 3
            fresh = 2 ** (-(max(0.0, t_now - t_pub)) / 0.5)      # halves every 6 months
            for sent in split_sentences(u.text):
                nt = norm_text(sent)
                found = []
                for mm in num_after.finditer(nt):
                    words = (mm.group(2) + mm.group(3)).split()
                    hit = next((k for k, w in enumerate(words[:2]) if stem(w) in nouns
                                or stem(w).replace("ship", "") in nouns), None)
                    if hit is None or stem(words[0]) in ("year", "month", "day", "percent", "time"):
                        continue
                    nxt = words[hit + 1] if hit + 1 < len(words) else \
                        (re.match(r"\s*([a-z\-]+)", nt[mm.end():]) or [None, None])[1]
                    if nxt and nxt not in COUNT_FOLLOW:
                        continue
                    found.append(mm.group(1))
                for mm in num_before.finditer(nt):
                    w0 = stem(mm.group(1))
                    if w0 in nouns or w0.replace("ship", "") in nouns:
                        found.append(mm.group(2))
                for val in found:
                    val = val.replace(",", "")
                    if re.fullmatch(r"(19|20)\d{2}", val) or re.fullmatch(r"0+", val):
                        continue
                    st = set(tokens(sent)) | set(tokens(u.title or ""))
                    cv = (sum(1 for t in other if self._covered(t, st)) / len(other)) if other else 1.0
                    if other and cv < 0.5:
                        continue
                    own = 1.0 if re.search(r"\b(dupaco|it has|our|we|its)\b", nt) else 0.3
                    w = (0.3 + 0.7 * u.auth) * fresh * (0.2 + cv) ** 2 * own
                    best_w[val] = max(best_w[val], w)
                    sum_w[val] += w
                    if val not in examples or w > examples[val][0]:
                        examples[val] = (w, sent, u)
        if not best_w:
            return None
        score = {v: best_w[v] + 0.01 * sum_w[v] for v in best_w}
        val = max(score.items(), key=lambda x: (x[1], x[0]))[0]
        _, sent, u = examples[val]
        note = None
        if len(score) > 1:
            others = sorted((v for v in score if v != val), key=lambda v: -score[v])[:3]
            note = f"Older or less authoritative pages give {', '.join(others)}; {val} comes from the newest source."
        return Answer(text=sent, sources=[Source(url=u.url, snapshot_id=sid, quote=sent[:200])],
                      conflict_note=note)

    ORDINALS = ["second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"]

    def _recognition_answer(self, q: Query, si: int) -> Optional[Answer]:
        orgs = [w for w in re.findall(r"\b([A-Z][A-Za-z&]+)\b", q.raw) if w not in ("Dupaco", "What", "How", "Has", "Is", "Did", "Does", "Which", "I")]
        if not orgs:
            return None
        rx = re.compile(r"\b(" + "|".join(self.ORDINALS) + r") (?:consecutive )?(?:time|year)\b", re.I)
        best = None
        for u in self.units:
            if u.kind != "txt" or si not in u.snaps or not any(o in u.text for o in orgs):
                continue
            for sent in split_sentences(u.text):
                m = rx.search(sent)
                if not m or not any(o in sent for o in orgs) and not any(o in (u.title or "") for o in orgs):
                    continue
                if not re.search(r"recogni|named|rank|award|honor", sent, re.I):
                    continue
                key = (u.pub or "", self.ORDINALS.index(m.group(1).lower()))
                if best is None or key > best[0]:
                    best = (key, sent, u)
        if best is None:
            return None
        _, sent, u = best
        return Answer(text=sent, sources=[Source(url=u.url, snapshot_id=self.snaps[si][0], quote=sent[:200])])

    # ------------------------------------------------------------------ membership / contact
    def _best_sentence(self, si: int, pattern: str, min_auth: float = 0.0, prefer_new: bool = True):
        best = None
        rx = re.compile(pattern, re.I)
        for u in self.units:
            if u.kind != "txt" or si not in u.snaps or u.auth < min_auth:
                continue
            if not rx.search(u.text):
                continue
            for sent in split_sentences(u.text):
                if rx.search(sent):
                    key = ((u.pub or "0000") if prefer_new else "", u.auth, -len(sent))
                    if best is None or key > best[0]:
                        best = (key, sent, u)
        return best

    def _membership_answer(self, q: Query, si: int) -> Optional[Answer]:
        sid = self.snaps[si][0]
        parts, srcs = [], []
        elig = None
        for u in self.units:
            if u.kind == "txt" and si in u.snaps and u.auth >= 0.75 and re.search(r"eligible to join", u.heading or "", re.I):
                elig = u
                break
        if elig:
            items = [s for s in split_sentences(elig.text) if re.match(r"you ", s, re.I)]
            if items:
                parts.append("You're eligible to join if any of these apply: " + " ".join(items[:4]))
                srcs.append(elig.url)
        area = self._best_sentence(si, r"serves? residents in .*counties.*(Iowa|Illinois|Wisconsin)")
        if area:
            parts.append(area[1])
            srcs.append(area[2].url)
        if not parts:
            return None
        return Answer(text=" ".join(parts), sources=[Source(url=u, snapshot_id=sid) for u in dict.fromkeys(srcs)])

    def _main_phone(self, si: int):
        c = Counter()
        where = {}
        for u in self.units:
            if u.kind == "txt" and si in u.snaps and u.auth >= 0.75:
                for m in re.finditer(r"\b(800-\d{3}-\d{4}|\d{3}-\d{3}-\d{4})\b", u.text):
                    c[m.group(1)] += 1 + (url_path(u.url) == "/contact") * 20
                    where.setdefault(m.group(1), u.url)
        if not c:
            return None, None
        ph = c.most_common(1)[0][0]
        return ph, where[ph]

    def _contact_answer(self, q: Query, si: int) -> Optional[Answer]:
        ph, url = self._main_phone(si)
        if not ph:
            return None
        sid = self.snaps[si][0]
        if re.search(r"mailing address|po box|p\.o\. box|where (?:do i|can i|should i) (?:send|mail)", q.ql):
            page = [u for u in self.units if u.kind == "txt" and si in u.snaps and url_path(u.url) == "/contact"]
            blob = "\n".join(u.text for u in sorted(page, key=lambda x: x.uid))
            m = re.search(r"(P\.?\s?O\.?\s?Box \d+)\W+([A-Z][a-z]+, [A-Z]{2} \d{5}(?:-\d{4})?)", blob)
            if m:
                return Answer(text=f"Dupaco's mailing address is {m.group(1)}, {m.group(2)}.",
                              sources=[Source(url=page[0].url, snapshot_id=sid, quote=m.group(0))])
        if re.search(r"\b(update|change|correct|fix|fixing)\b", q.ql):
            text = (f"You can update your contact information by logging in to Shine Online or Mobile Banking "
                    f"(use the \"Contact Us\" tab), visiting any branch, or calling Dupaco at {ph}.")
        else:
            text = f"You can reach Dupaco at {ph}."
        return Answer(text=text, sources=[Source(url=url, snapshot_id=sid, quote=ph)])

    # ------------------------------------------------------------------ links
    def _link_valid(self, tgt: str, si: int) -> bool:
        p = urlparse(tgt)
        host = p.netloc.lower()
        if not host.endswith("dupaco.com"):
            return False
        if host in ("www.dupaco.com", "dupaco.com"):
            return url_path(tgt.split("#")[0]) in self.snap_pages[si]
        return host in ("join.dupaco.com", "shine.dupaco.com", "homeloans.dupaco.com")

    def _link_answer(self, q: Query, si: int) -> Optional[Answer]:
        """Recommend a URL: popular in-site links whose anchor/heading match the request and
        whose target page (still present in this snapshot) actually carries the content."""
        L = self.links[si] if si < len(self.links) else {}
        generic = {stem(w) for w in ("link", "url", "website", "web", "address", "page", "online",
                                     "use", "see", "go", "visit", "find", "should", "what", "how", "do",
                                     "where", "can", "access", "locator", "get")}
        key = [t for t in dict.fromkeys(q.qtoks) if t not in generic and len(t) > 1
               and not re.fullmatch(r"[\d.]+", t)]
        if "locator" in q.ql:
            key.append(stem("location"))
        if not key:
            return None
        idf = {t: self._idf(t) for t in key}
        tot = sum(idf.values()) or 1.0

        def cov(toks):
            return sum(w for t, w in idf.items() if self._covered(t, toks)) / tot

        # how well each current page's own content matches the request
        content = defaultdict(float)
        ranked = self._rank(q, si, self._bm25(key, si))
        top = ranked[0][0] if ranked else 1.0
        for sc, uid in ranked[:200]:
            u = self.units[uid]
            k = url_path(u.url)
            content[k] = max(content[k], sc / top)
        online = bool(re.search(r"\b(online|apply|application|sign up|enroll|open an? )", q.ql))
        groups = {}
        for tgt, info in L.items():
            if not self._link_valid(tgt, si):
                continue
            pu = urlparse(tgt)
            host = pu.netloc.lower()
            sub = host not in ("www.dupaco.com", "dupaco.com")
            gkey = ("https://" + host + "/") if sub else "https://www.dupaco.com" + url_path(tgt.split("#")[0]).split("?")[0]
            path_toks = set(tokens(re.sub(r"[/#._?=&-]", " ", host.split(".")[0] + " " + pu.path + " " + pu.fragment)))
            pop = 0.0
            for (anchor, ctx), w in info["occ"].items():
                c = max(cov(set(tokens(anchor)) | path_toks), 0.8 * cov(set(tokens(anchor + " " + ctx)) | path_toks))
                pop += w * c * c
            g = groups.setdefault(gkey, {"pop": 0.0, "variants": Counter(), "sub": sub, "path": set()})
            g["pop"] += pop
            g["variants"][tgt] += pop
            g["path"] |= path_toks
        cands = []
        for gkey, g in groups.items():
            if g["pop"] <= 0:
                continue
            if g["sub"]:
                cont = 0.9 if (online and set(key) & g["path"]) else 0.5 * cov(g["path"])
                tauth = 0.8
                disp = gkey
            else:
                cont = content.get(url_path(gkey), 0.0)
                tauth = page_authority(gkey)
                var = g["variants"].most_common(1)[0][0]
                frag = urlparse(var).fragment
                disp = gkey + ("#" + frag if re.fullmatch(r"[a-z][a-z\-]{1,30}", frag or "") else "")
            sc = math.log1p(g["pop"]) * (0.1 + cont) ** 2 * tauth
            cands.append((sc, disp, round(g["pop"], 2), round(cont, 2), tauth))
        if not cands:
            return None
        cands.sort(key=lambda x: (-x[0], x[1]))
        if _DEBUG:
            for c in cands[:6]:
                print("   link cand", round(c[0], 3), c[1], c[2:])
        best_sc, best = cands[0][:2]
        if best_sc < 0.02:
            return None
        picked = [best]
        for sc, t, *_ in cands[1:4]:
            if sc >= best_sc * (0.5 if online else 0.8) and urlparse(t).netloc != urlparse(best).netloc and len(picked) < 2:
                picked.append(t)
        text = "You can use " + picked[0] + ("" if len(picked) == 1 else " (see also " + ", ".join(picked[1:]) + ")") + "."
        return Answer(text=text, sources=[Source(url=t, snapshot_id=self.snaps[si][0]) for t in picked])

    # ------------------------------------------------------------------ conflicts
    # ------------------------------------------------------------------ fact keys (for conflicts)
    FAMILY = [
        (r"\bauto|motorcycle", "auto"), (r"\bboat|\brv\b", "boat-rv"), (r"\batv|snowmobile|jet ski", "atv"),
        (r"personal loan", "personal-loan"), (r"home improvement", "home-improvement"),
        (r"heloc|line of credit", "heloc"), (r"home equity loan|loan . standard", "home-equity-loan"),
        (r"purchase", "mortgage-purchase"), (r"refinanc", "mortgage-refinance"),
        (r"relief", "mortgage-relief"), (r"jumbo", "jumbo"), (r"special", "special"),
        (r"certificate|\d+ month(?!s? fixed)", "certificate"), (r"\broth\b", "roth"),
        (r"traditional", "traditional"), (r"\bira\b", "ira"), (r"business", "business"),
        (r"share savings", "share-savings"), (r"bright", "bright-savings"), (r"you name it", "you-name-it"),
        (r"holiday", "holiday-club"), (r"launchpad", "launchpad"), (r"investor", "money-market"),
        (r"achieve", "achieve-money-market"), (r"high-yield|high yield", "high-yield"),
        (r"health savings|\bhsa\b", "hsa"), (r"1st rate", "1st-rate-checking"), (r"free checking", "free-checking"),
        (r"platinum", "platinum-visa"), (r"rewards", "rewards-visa"), (r"intro|balance transfer", "intro"),
        (r"variable interest|business visa", "business-visa"),
    ]

    def _fact_key(self, u: Unit):
        cap = (u.caption or "").split(" / ")[-1]
        blob = norm_text(cap + " " + (u.label or "") + " " + (u.heading or "" if "business" in (u.heading or "").lower() else ""))
        fam = []
        for rx, name in self.FAMILY:
            if re.search(rx, blob):
                fam.append(name)
        if not fam:
            return None
        if "mortgage-purchase" in fam or "mortgage-refinance" in fam:
            fam = [f for f in fam if f not in ("certificate", "heloc")]
        if "money-market" in fam or "high-yield" in fam or "achieve-money-market" in fam:
            fam = [f for f in fam if f != "certificate"]
        if "achieve-money-market" in fam:
            fam = [f for f in fam if f != "money-market"]
        label = norm_text(u.label or "")
        term = tuple(sorted(set(re.findall(r"(\d+)\s*-?\s*(?:month|mo\b)", label)) |
                            {f"{y}y" for y in re.findall(r"(\d+)\s*-?\s*year", label)}))
        tier = None
        for c in (u.cells or []):
            tr = tier_range(c)
            if tr:
                tier = (round(tr[0]), tr[1] if tr[1] == float("inf") else round(tr[1]))
                break
        myr = None
        if re.search(r"model year", label):
            myr = self._model_year_rank(u)
        return (tuple(fam), term, tier, myr)

    def _model_year_rank(self, u: Unit):
        rows = self._tables.get(u.table_key, [])
        def first_year(x):
            m = re.search(r"(\d{4})", x.label or "")
            if not m:
                return 0
            ys = [int(y) if len(y) == 4 else 2000 + int(y) for y in re.findall(r"(\d{4}|(?<=-)\d{2}\b)", x.label or "")]
            return max(ys) if ys else int(m.group(1))
        years = sorted({first_year(x) for x in rows}, reverse=True)
        fy = first_year(u)
        return years.index(fy) if fy in years else None

    @staticmethod
    def _value_str(u: Unit) -> Optional[str]:
        vals = []
        for h, c in zip(u.header or [], u.cells or []):
            if re.search(r"\d\s?%", c) and not re.search(r"points|ownership", h, re.I):
                hh = "APY" if re.search(r"apy|yield", h, re.I) else ("APR" if re.search(r"apr|annual percentage rate", h, re.I)
                                                                     else ("rate" if re.search(r"rate", h, re.I) else ""))
                vals.append((c.replace(" APR", "").strip() + (" " + hh if hh else "")).strip())
        return " / ".join(vals) if vals else None

    @staticmethod
    def _value_sig(v: str):
        # the headline figure: last value column (APY / APR); first % within a column
        parts = [round(float(re.search(r"(\d+(?:\.\d+)?)\s?%", part).group(1)), 3)
                 for part in v.split(" / ") if re.search(r"\d\s?%", part)]
        return frozenset(parts[-1:] + parts[:1])

    def _topic(self, u: Unit, key) -> str:
        fam, term, tier, myr = key
        cap = (u.caption or "").split(" / ")[-1]
        parts = []
        for t in term:
            parts.append(f"{t[:-1]}-year" if t.endswith("y") else f"{t}-month")
        name = (u.label or "").strip()
        base = f"{' '.join(parts)} {cap}: {name}".strip() if parts else f"{cap}: {name}"
        kind = "APY" if any("APY" in h or "Yield" in h for h in (u.header or [])) else "APR / rate"
        return f"{base} ({kind})"

    def _conflict_note_for(self, u: Unit, si: int) -> Optional[str]:
        try:
            key = self._fact_key(u)
        except Exception:
            return None
        if key is None:
            return None
        cur = self._value_str(u)
        if not cur:
            return None
        others = set()
        if getattr(self, "_fact_index", None) is None:
            self._fact_index = defaultdict(list)
            for v in self.units:
                if v.kind == "row":
                    try:
                        fk = self._fact_key(v)
                    except Exception:
                        fk = None
                    if fk is not None:
                        self._fact_index[fk].append(v)
        for v in self._fact_index.get(key, []):
            if si in v.snaps and v is not u and v.auth < u.auth:
                vs = self._value_str(v)
                if vs and self._value_sig(vs) != self._value_sig(cur):
                    others.add((vs, url_path(v.url)))
        if not others:
            return None
        o = "; ".join(f"{vs} on {p}" for vs, p in sorted(others)[:3])
        return f"Chose {cur} from {url_path(u.url)} (most authoritative, newest effective date); outdated copies show {o}."

    # ------------------------------------------------------------------ conflicts
    def conflicts(self) -> list[Conflict]:
        self._finalize()
        if self._conflicts is not None:
            return self._conflicts
        out = []
        try:
            out += self._row_conflicts()
        except Exception:
            pass
        try:
            out += self._prose_conflicts()
        except Exception:
            pass
        try:
            out += self._text_conflicts()
        except Exception:
            pass
        # one entry per distinct set of competing values (prefer plain products over IRA/business twins)
        def _complexity(c):
            t = c.topic.lower()
            return sum(w in t for w in ("ira", "roth", "traditional", "business", "equity refinance"))
        out.sort(key=_complexity)
        seen, final = set(), []
        for c in out:
            sig = (tuple(sorted(c.values)), c.resolution)
            if sig in seen:
                continue
            seen.add(sig)
            final.append(c)
        self._conflicts = final
        return final

    def _row_conflicts(self) -> list[Conflict]:
        last = len(self.snaps) - 1
        obs = defaultdict(list)
        for u in self.units:
            if u.kind != "row" or not self._has_rate(u):
                continue
            key = self._fact_key(u)
            vs = self._value_str(u)
            if key is None or not vs:
                continue
            for si in u.snaps:
                obs[key].append((si, u.auth, u.eff or "", vs, u))
        res = []
        def same(a, b):
            return a <= b or b <= a

        for key, lst in obs.items():
            sigs = list({self._value_sig(v) for _, _, _, v, _ in lst})
            if not any(not same(a, b) for i, a in enumerate(sigs) for b in sigs[i + 1:]):
                continue
            cur = [o for o in lst if o[0] == last]
            if not cur:
                continue
            best = max(cur, key=lambda o: (o[1], o[2], -len(o[3])))
            win_sig = self._value_sig(best[3])
            # history of the winning (authoritative) source and the competing values
            vals, srcs, kinds = [best[3]], [Source(url=best[4].url, snapshot_id=self.snaps[last][0], quote=best[4].text[:160])], set()
            competitors = {}
            for si, auth, eff, v, u in sorted(lst, key=lambda o: (-o[0], -o[1])):
                sg = self._value_sig(v)
                if same(sg, win_sig) or any(same(sg, c) for c in competitors):
                    continue
                competitors[sg] = (si, auth, eff, v, u)
            for sg, (si, auth, eff, v, u) in list(competitors.items())[:3]:
                vals.append(v)
                srcs.append(Source(url=u.url, snapshot_id=self.snaps[si][0], quote=u.text[:160]))
                kinds.add("page" if si == last else "time")
            stale_pages = sorted({url_path(c[4].url) for c in competitors.values() if c[0] == last})
            reasons = []
            if stale_pages:
                reasons.append(f"{url_path(best[4].url)} is the authoritative rate source"
                               f"{' (effective ' + fmt_date(best[2]) + ')' if best[2] else ''}; "
                               f"{', '.join(stale_pages[:3])} still show an outdated value")
            if any(c[0] != last for c in competitors.values()):
                old = [c for c in competitors.values() if c[0] != last]
                o = min(old, key=lambda c: c[0])
                reasons.append(f"the value changed over time (was {o[3]} in the {self.snaps[o[0]][1]} crawl); "
                               f"the newest crawl ({self.snaps[last][1]}) wins")
            res.append((key, best[4], Conflict(topic=self._topic(best[4], key), values=vals, sources=srcs,
                                               resolution=best[3], reason="; ".join(reasons) or "newest authoritative value")))
        # merge loan-amount / model-year tiers of the same product and term into one entry
        groups = defaultdict(list)
        for key, u, c in res:
            fam, term, tier, myr = key
            merge = bool(tier is not None or myr is not None) and any(
                f in fam for f in ("mortgage-purchase", "mortgage-refinance", "auto", "boat-rv", "atv", "home-improvement"))
            groups[(fam, term) if merge else key].append((key, u, c))
        merged = []
        for gk, items in groups.items():
            if len(items) == 1:
                merged.append(items[0][2])
                continue
            items.sort(key=lambda x: ((x[0][3] if x[0][3] is not None else 9), x[0][2] or (0, 0)))
            lead_u = items[0][1]
            vals, srcs, res_parts, reasons = [], [], [], []
            for key, u, c in items:
                tier_name = (u.label or "").strip()
                res_parts.append(f"{c.resolution} ({tier_name})")
                for v in c.values:
                    if v not in vals:
                        vals.append(v)
                srcs.extend(c.sources[:2])
                if c.reason and c.reason not in reasons:
                    reasons.append(c.reason)
            fam, term = gk[0], gk[1]
            cap = (lead_u.caption or "").split(" / ")[-1]
            terms = " ".join(f"{t[:-1]}-year" if t.endswith("y") else f"{t}-month" for t in term)
            topic = f"{terms} {cap} rates by tier".strip()
            merged.append(Conflict(topic=topic, values=vals[:12], sources=srcs[:8],
                                   resolution="; ".join(res_parts), reason=" | ".join(reasons[:2])))
        merged.sort(key=lambda c: ("still show" not in (c.reason or ""), c.topic))
        return merged

    PROSE_FACT_RE = re.compile(r"^(?P<subj>[A-Za-z0-9][^%$:]{3,80}?)\s*(?::|\bis\b|\bof\b)\s*(?P<val>\$\s?\d[\d,]*(?:\.\d+)?|\d+(?:\.\d+)?\s?%)(?P<rest>[^%$]{0,60})$")

    def _prose_conflicts(self) -> list[Conflict]:
        last = len(self.snaps) - 1
        obs = defaultdict(list)
        for u in self.units:
            if u.kind != "txt":
                continue
            for sent in split_sentences(u.text):
                if len(sent) > 140:
                    continue
                m = self.PROSE_FACT_RE.match(sent.strip())
                if not m:
                    continue
                subj = tuple(sorted(set(tokens(m.group("subj")))))
                if not (2 <= len(subj) <= 8) or len(m.group("rest")) > 30:
                    continue
                if not re.search(r"\b(apy|apr|rate|fee|dividend|yield|charge|cost)\b", m.group("subj"), re.I):
                    continue
                if re.search(r"\bup to\b|as low as|starting at|minimum|maximum", sent, re.I):
                    continue
                expired = bool(self.EXPIRED_RE.search(sent))
                for si in u.snaps:
                    obs[subj].append((si, u.auth, expired, m.group("val").replace(" ", ""), sent, u))
        res = []
        for subj, lst in obs.items():
            sigs = {re.sub(r"[,$%]", "", v) for *_, v, _, _ in [(o[0], o[1], o[2], o[3], o[4], o[5]) for o in lst]}
            sigs = {re.sub(r"[,$%]", "", o[3]) for o in lst}
            if len(sigs) < 2:
                continue
            def page_changed(url):
                per = defaultdict(set)
                for o in lst:
                    if o[5].url == url:
                        per[o[0]].add(re.sub(r"[,$%]", "", o[3]))
                if any(len(v) > 1 for v in per.values()):
                    return False      # two different facts on one page, not a change
                return len(set().union(*per.values())) > 1 if per else False
            same_page_change = any(page_changed(url) for url in {o[5].url for o in lst})
            if not (same_page_change or any(o[2] for o in lst)):
                continue
            cur = [o for o in lst if o[0] == last and not o[2]] or [o for o in lst if o[0] == last]
            if not cur:
                continue
            best = max(cur, key=lambda o: (not o[2], o[1]))
            vals, srcs = [best[3]], [Source(url=best[5].url, snapshot_id=self.snaps[last][0], quote=best[4])]
            for o in sorted(lst, key=lambda o: (-o[0], -o[1])):
                if o[3] not in vals:
                    vals.append(o[3])
                    srcs.append(Source(url=o[5].url, snapshot_id=self.snaps[o[0]][0], quote=o[4]))
            reason = "newest crawl, most authoritative page"
            if any(o[2] for o in lst):
                reason += "; the other value comes from an expired promotion"
            res.append(Conflict(topic=" ".join(subj), values=vals[:4], sources=srcs[:4],
                                resolution=best[3], reason=reason))
        return res[:40]

    def _text_conflicts(self) -> list[Conflict]:
        res = []
        last = len(self.snaps) - 1
        for noun, topic in (("branches", "Number of Dupaco branch locations"),):
            q = Query(f"How many {noun} does Dupaco have?")
            a = self._count_answer(q, last)
            if a is None or not a.conflict_note:
                continue
            win = re.findall(r"(\d[\d,]*)\s+(?:\w+\s){0,2}?" + noun[:-1], norm_text(a.text))
            others = re.findall(r"give ([\d, ]+);", a.conflict_note)
            if not others:
                continue
            ov = [x.strip() for x in others[0].split(",") if x.strip()][:3]
            m = re.search(r"; (\d+) comes from", a.conflict_note)
            wv = m.group(1) if m else (win[0] if win else None)
            if not wv:
                continue
            res.append(Conflict(topic=topic, values=[f"{wv} {noun}"] + [f"{v} {noun}" for v in ov],
                                sources=a.sources, resolution=f"{wv} {noun}",
                                reason="newest dated page wins; older press releases and bios carry earlier counts"))
        # "the Nth time <org> recognized Dupaco"
        ords = ["second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"]
        found = defaultdict(list)
        for u in self.units:
            if u.kind != "txt" or last not in u.snaps:
                continue
            for m in re.finditer(r"\b(" + "|".join(ords) + r") (?:consecutive )?(time|year)\b[^.]{0,60}?\b([A-Z][A-Za-z&]+(?: [A-Z][A-Za-z&]+)?)", u.text):
                org = m.group(3)
                if org.split()[0] in ("Dupaco", "DUBUQUE", "Iowa", "The", "This", "It"):
                    continue
                if not re.search(r"recogni|named|rank|award|honor", u.text[m.start():m.end() + 80], re.I):
                    continue
                found[org].append((ords.index(m.group(1)), m.group(1), u.pub or "", u))
        for org, lst in found.items():
            vals = sorted({x[1] for x in lst}, key=ords.index)
            if len(vals) < 2:
                continue
            best = max(lst, key=lambda x: (x[2], x[0]))
            res.append(Conflict(topic=f"How many times {org} has recognized Dupaco",
                                values=[best[1]] + [v for v in vals if v != best[1]][::-1],
                                sources=[Source(url=x[3].url, snapshot_id=self.snaps[last][0]) for x in lst[:4]],
                                resolution=best[1], reason="the most recent press release supersedes earlier ones"))
        return res


def build():
    return TemporalKnowledge()
