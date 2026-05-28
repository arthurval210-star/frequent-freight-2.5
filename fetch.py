"""
Frequent Freight 2.5
====================
Freight / LTL shipper-prospect lead generation for the San Antonio metro.

Finds COMPANIES THAT SHIP physical goods (and therefore likely need LTL /
freight support), NOT logistics providers. It explicitly EXCLUDES any company
that is itself a logistics / 3PL / broker / freight / transportation business.

Hard rules enforced by this pipeline:
  - San Antonio + surrounding cities only.
  - Estimated revenue must land in the $500K - $10M band.
  - Exclude companies whose name OR website contains: logistics, 3PL, broker,
    freight, transportation (and close variants).
  - Target businesses that ship products / materials / equipment / inventory /
    supplies / packaged goods (manufacturers, wholesalers, distributors,
    building-material suppliers, fabricators, food producers, etc.).

Integrity rules (carried over from the original system):
  - NEVER invent emails or phone numbers. Only real contact data scraped from
    the company's own website is used. Unknown fields are left blank and the
    lead is flagged so a human can fill them.
  - Revenue is an ESTIMATE derived from real signals (employee-count bands,
    multi-location, category baselines). Every lead carries an
    `revenue_basis` string so the estimate is never mistaken for verified data.
  - Decision-maker name/title are only set when scraped from the site (team /
    about / leadership pages). Otherwise title is set to a prioritized target
    role and name is left blank with a NEEDS_DM flag.

Nothing is pushed to a CRM here. This script only produces leads.json for
review in the dashboard. Pipedrive sync lives in pipedrive_push.py and only
runs against human-approved leads.
"""

import json
import csv
import re
import time
import logging
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus, urlparse

import requests
from bs4 import BeautifulSoup

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("frequent_freight")

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DASHBOARD_DIR = BASE_DIR / "dashboard"
EXPORTS_DIR = BASE_DIR / "exports"
for d in (DATA_DIR, DASHBOARD_DIR, EXPORTS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ── Geography: San Antonio + surrounding cities ────────────────────────────────
# Cities considered "in territory". Used both to drive searches and to validate
# scraped addresses. Add/remove freely.
SA_METRO_CITIES = [
    "San Antonio", "Schertz", "Cibolo", "Universal City", "Converse",
    "Live Oak", "Selma", "New Braunfels", "Seguin", "Boerne", "Helotes",
    "Leon Valley", "Castle Hills", "Alamo Heights", "Windcrest", "Kirby",
    "Pleasanton", "Floresville", "Hondo", "Castroville", "Bulverde",
    "Fair Oaks Ranch", "Marion", "La Vernia", "Lytle", "Devine", "Adkins",
    "Macdona", "Von Ormy", "China Grove", "Elmendorf", "Atascosa",
]
SA_METRO_CITIES_LC = {c.lower() for c in SA_METRO_CITIES}

# Primary search hubs (we don't search every tiny town; we search the hubs and
# validate that scraped addresses fall in the metro set above).
SEARCH_HUBS = [
    "San-Antonio-TX", "New-Braunfels-TX", "Seguin-TX",
    "Schertz-TX", "Boerne-TX",
]

STATE = "TX"

# ── Shipper categories (companies that SHIP goods) ─────────────────────────────
# Each maps to a YellowPages slug. label = clean category shown in UI.
# typical_low/high = baseline annual-revenue band ($) for a SMB in this niche,
# used as a fallback when no headcount signal is available.
# freight_reason = why this category typically needs LTL/freight.
SHIPPER_CATEGORIES = {
    "manufacturers": {
        "label": "Manufacturer", "low": 1_000_000, "high": 9_000_000,
        "freight_reason": "Ships finished goods and receives raw materials on pallets — recurring outbound LTL.",
    },
    "wholesale-distributors": {
        "label": "Wholesale Distributor", "low": 1_500_000, "high": 10_000_000,
        "freight_reason": "Moves inventory to regional retailers/contractors — steady palletized LTL volume.",
    },
    "building-materials": {
        "label": "Building Materials", "low": 1_000_000, "high": 9_000_000,
        "freight_reason": "Heavy, bulky materials to job sites — LTL and flatbed needs almost daily.",
    },
    "industrial-equipment-supplies": {
        "label": "Industrial Supply", "low": 800_000, "high": 8_000_000,
        "freight_reason": "Equipment and MRO supplies shipped to facilities — frequent crated LTL.",
    },
    "metal-fabricators": {
        "label": "Metal Fabricator", "low": 700_000, "high": 8_000_000,
        "freight_reason": "Fabricated steel/parts shipped to clients — palletized and oversized LTL.",
    },
    "furniture-manufacturers": {
        "label": "Furniture Maker", "low": 600_000, "high": 7_000_000,
        "freight_reason": "Bulky finished furniture to dealers/customers — protected LTL shipments.",
    },
    "food-products-manufacturers": {
        "label": "Food Producer", "low": 800_000, "high": 9_000_000,
        "freight_reason": "Packaged food to distributors/retailers — regular palletized (sometimes temp) LTL.",
    },
    "plastics-manufacturers": {
        "label": "Plastics Maker", "low": 800_000, "high": 8_000_000,
        "freight_reason": "Molded/extruded product shipped in bulk — recurring outbound LTL.",
    },
    "printing-services-commercial": {
        "label": "Commercial Printer", "low": 600_000, "high": 6_000_000,
        "freight_reason": "Bulk printed materials on skids to clients — frequent LTL runs.",
    },
    "wholesale-nurseries": {
        "label": "Wholesale Nursery", "low": 500_000, "high": 6_000_000,
        "freight_reason": "Plants/landscape stock to retailers and contractors — palletized LTL.",
    },
    "electrical-supplies-wholesale": {
        "label": "Electrical Supply", "low": 800_000, "high": 9_000_000,
        "freight_reason": "Gear and fixtures to contractors/sites — steady palletized LTL.",
    },
    "plumbing-fixtures-supplies": {
        "label": "Plumbing Supply", "low": 800_000, "high": 8_000_000,
        "freight_reason": "Fixtures and pipe to contractors — recurring heavy LTL.",
    },
    "auto-parts-wholesale": {
        "label": "Auto Parts Wholesale", "low": 700_000, "high": 8_000_000,
        "freight_reason": "Parts to shops across the region — frequent boxed/palletized LTL.",
    },
    "packaging-materials": {
        "label": "Packaging Supplier", "low": 700_000, "high": 8_000_000,
        "freight_reason": "Cartons/film/pallets shipped in volume — high-cube recurring LTL.",
    },
    "chemical-suppliers": {
        "label": "Chemical Supplier", "low": 1_000_000, "high": 10_000_000,
        "freight_reason": "Drummed/packaged chemicals to industrial buyers — regulated LTL freight.",
    },
}

# ── Exclusion logic (the most important rule) ──────────────────────────────────
# A company is excluded if name OR website host contains any of these tokens.
# Word-boundary matching avoids false hits like "Transportationless" edge cases,
# but we also catch common compounds.
EXCLUSION_TOKENS = [
    "logistics", "3pl", "3-pl", "broker", "brokerage", "freight",
    "transportation", "transport", "trucking", "carrier", "carriers",
    "expedite", "expediting", "courier", "drayage", "intermodal",
    "forwarder", "forwarding", "haulage", "hauling", "moving company",
    "movers", "warehousing services", "fulfillment services",
]
EXCLUSION_RE = re.compile(
    r"(?<![a-z])(" + "|".join(re.escape(t) for t in EXCLUSION_TOKENS) + r")(?![a-z])",
    re.IGNORECASE,
)

# ── Decision-maker target titles (priority order) ──────────────────────────────
DM_TARGET_TITLES = [
    "Owner", "Operations Manager", "Warehouse Manager", "Shipping Manager",
    "Supply Chain Manager", "Purchasing Manager", "General Manager",
]
# Regex used when scraping team/about pages for a real person + title.
DM_TITLE_RE = re.compile(
    r"\b(owner|founder|president|ceo|operations manager|warehouse manager|"
    r"shipping manager|logistics manager|supply chain manager|purchasing manager|"
    r"procurement manager|general manager|plant manager|gm)\b",
    re.IGNORECASE,
)
# Map a scraped raw title to one of our prioritized buckets for sorting.
TITLE_PRIORITY = {t.lower(): i for i, t in enumerate(DM_TARGET_TITLES)}

# ── Revenue band (hard qualification gate) ─────────────────────────────────────
REV_MIN = 500_000
REV_MAX = 10_000_000

# ── HTTP ───────────────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2

EXCLUDED_EMAIL_DOMAINS = [
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "sentry.io", "w3.org", "schema.org", "example.com", "wixpress.com",
    "squarespace.com", "wordpress.com", "sentry-cdn.com",
    "png", "jpg", "jpeg", "gif", "svg", "webp",
]

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")


# ── Helpers ────────────────────────────────────────────────────────────────────
def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return (raw or "").strip()


def business_key(name: str, phone: str) -> str:
    slug = re.sub(r"\W+", "", (name + phone).lower())
    return hashlib.md5(slug.encode()).hexdigest()[:12]


def retry_get(url: str, timeout: int = 15, **kwargs) -> Optional[requests.Response]:
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout, **kwargs)
            r.raise_for_status()
            return r
        except Exception as e:
            log.warning(f"Attempt {attempt}/{RETRY_ATTEMPTS} failed for {url}: {e}")
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_DELAY * attempt)
    return None


def is_excluded_company(name: str, website: str) -> Optional[str]:
    """Return the offending token if the company is a logistics-type business,
    else None. Checks name and website host."""
    if name and EXCLUSION_RE.search(name):
        return EXCLUSION_RE.search(name).group(1).lower()
    if website:
        host = urlparse(website if website.startswith("http") else "http://" + website).netloc.lower()
        # In hostnames there are no spaces, so check substring presence of each
        # token (logistics/freight/3pl etc. embedded in the domain).
        for t in EXCLUSION_TOKENS:
            if t.replace(" ", "") in host.replace("-", ""):
                return t
    return None


def in_metro(city: str) -> bool:
    return (city or "").strip().lower() in SA_METRO_CITIES_LC


# ── Email + decision-maker scraping (REAL data only) ───────────────────────────
def scrape_site_contacts(website_url: str) -> dict:
    """
    Scrape the company's own site for a real email, a real person + title
    (decision maker), and a direct phone if present. Returns blanks when not
    found — never guesses.
    """
    out = {"email": "", "dm_name": "", "dm_title": "", "dm_phone": "", "dm_email": ""}
    if not website_url:
        return out
    if not website_url.startswith("http"):
        website_url = "https://" + website_url

    pages = ["", "/contact", "/contact-us", "/about", "/about-us",
             "/team", "/our-team", "/leadership", "/staff", "/management"]
    emails_found, dm_candidates = [], []

    for page in pages:
        try:
            url = website_url.rstrip("/") + page
            r = requests.get(url, headers=HEADERS, timeout=8, allow_redirects=True)
            if r.status_code >= 400:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            text = soup.get_text(" ", strip=True)

            # mailto links (most reliable)
            for a in soup.find_all("a", href=True):
                if a["href"].startswith("mailto:"):
                    e = a["href"].replace("mailto:", "").split("?")[0].strip().lower()
                    if e and not any(d in e for d in EXCLUDED_EMAIL_DOMAINS):
                        emails_found.append(e)

            for e in EMAIL_RE.findall(r.text):
                e = e.lower()
                if not any(d in e for d in EXCLUDED_EMAIL_DOMAINS):
                    emails_found.append(e)

            # Decision-maker: look for "Name — Title" / "Name, Title" patterns
            # near a target title keyword.
            for m in DM_TITLE_RE.finditer(text):
                window = text[max(0, m.start() - 60): m.end() + 10]
                # Try to grab a capitalized 2-3 word name preceding the title
                name_m = re.search(
                    r"([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+)\s*[,\-–—]?\s*$",
                    text[max(0, m.start() - 60): m.start()],
                )
                if name_m:
                    dm_candidates.append((name_m.group(1).strip(), m.group(1).strip()))

            if page in ("/team", "/our-team", "/leadership", "/staff", "/management") and dm_candidates:
                break
        except Exception:
            continue

    # pick first real email
    seen = set()
    for e in emails_found:
        if e not in seen:
            seen.add(e)
            out["email"] = e
            break

    # pick highest-priority decision maker
    if dm_candidates:
        def rank(c):
            return TITLE_PRIORITY.get(c[1].lower(), 99)
        dm_candidates.sort(key=rank)
        out["dm_name"], raw_title = dm_candidates[0]
        out["dm_title"] = raw_title.title()
        # if the scraped email looks tied to this person, treat as DM email
        if out["email"]:
            first = out["dm_name"].split()[0].lower()
            if first and first in out["email"]:
                out["dm_email"] = out["email"]

    return out


# ── Revenue estimation (signal-based, clearly labeled) ─────────────────────────
EMPLOYEE_BAND_RE = re.compile(
    r"(\d{1,4})\s*(?:\+|to|–|-)?\s*(\d{1,4})?\s*(?:employees|staff|team members|people)",
    re.IGNORECASE,
)


def estimate_revenue(biz: dict, site_text: str, cat: dict) -> tuple:
    """
    Estimate annual revenue from real signals.
    Returns (estimate:int, basis:str, confidence:str).

    Priority of signals:
      1. Explicit employee count on the site  -> ~$180K revenue/employee SMB heuristic
      2. Multiple locations mentioned         -> scale category baseline up
      3. Category baseline midpoint           -> fallback
    Estimate is then clamped/reported; qualification gate applied later.
    """
    low, high = cat["low"], cat["high"]
    baseline = (low + high) // 2

    # 1. employee count
    if site_text:
        m = EMPLOYEE_BAND_RE.search(site_text)
        if m:
            n1 = int(m.group(1))
            n2 = int(m.group(2)) if m.group(2) else n1
            emp = max(n1, n2)
            est = emp * 180_000  # SMB revenue-per-employee heuristic
            return est, f"~{emp} employees on site x $180K/employee", "medium"

    # 2. multi-location signal
    if site_text and re.search(r"\b(locations|two locations|three locations|multiple locations|branches)\b",
                               site_text, re.IGNORECASE):
        est = min(high, int(baseline * 1.5))
        return est, f"multi-location signal x {cat['label']} baseline", "low"

    # 3. category baseline
    return baseline, f"{cat['label']} category baseline (no headcount found)", "low"


# ── Pitch generator (10-sec cold-open, pattern interrupt first 3s) ─────────────
def build_pitch(biz: dict, cat: dict) -> str:
    """
    One ~10-second cold pitch. First clause is a pattern interrupt (an
    unexpected/disarming opener), then a company-specific freight hook.
    """
    name = biz["company_name"]
    label = cat["label"].lower()
    city = biz.get("city", "San Antonio")

    # Pattern interrupts (first 3 seconds) — disarming, not "Hi my name is..."
    interrupt = {
        "Manufacturer": "I'll keep this to ten seconds, then you decide",
        "Wholesale Distributor": "Quick one — not a sales call, a math question",
        "Building Materials": "Ten seconds, then hang up on me if it's dumb",
        "Industrial Supply": "I promise I'm not another insurance guy",
        "Metal Fabricator": "One question and I'm gone",
        "Furniture Maker": "Bear with me, this is worth ten seconds",
        "Food Producer": "Quick — and I already know you're slammed",
        "Plastics Maker": "Ten seconds, scout's honor",
        "Commercial Printer": "Not selling printers, promise",
        "Wholesale Nursery": "Quick one before your morning loads out",
        "Electrical Supply": "Ten seconds, then your call",
        "Plumbing Supply": "I'll talk fast — one freight question",
        "Auto Parts Wholesale": "Quick question about your outbound, that's it",
        "Packaging Supplier": "Ten seconds — kind of ironic given what you ship",
        "Chemical Supplier": "One question, fully aware you're busy",
    }.get(cat["label"], "I'll keep this to ten seconds")

    hook = (
        f"You're a {label} in {city} moving product out the door every week, "
        f"and I help shippers your size stop overpaying on LTL. "
        f"If I can't beat what you're paying now, I'll tell you straight. "
        f"Worth a two-minute look at one of your recent bills?"
    )
    return f"{interrupt} — {name}, {hook}"


# ── Build the freight reason (company-specific) ────────────────────────────────
def build_freight_reason(biz: dict, cat: dict, site_text: str) -> str:
    base = cat["freight_reason"]
    extra = ""
    if site_text:
        if re.search(r"\bnationwide|across the (u\.?s\.?|country|state)|ship anywhere\b",
                     site_text, re.IGNORECASE):
            extra = " Site advertises wide-area shipping — confirmed outbound volume."
        elif re.search(r"\bwholesale|distributor|bulk orders\b", site_text, re.IGNORECASE):
            extra = " Site confirms wholesale/bulk model — palletized outbound."
    return base + extra


# ── Short description ──────────────────────────────────────────────────────────
def build_description(biz: dict, cat: dict, site_text: str) -> str:
    if site_text:
        # try meta-description-like sentence
        snippet = site_text.strip().split(". ")
        for s in snippet:
            if 30 <= len(s) <= 160 and not EMAIL_RE.search(s):
                return s.strip().rstrip(".") + "."
    return f"{cat['label']} based in {biz.get('city','San Antonio')}, {STATE}."


# ── YellowPages scraper (shipper niches) ───────────────────────────────────────
def scrape_yellowpages(slug: str, hub: str, cat: dict) -> list:
    out = []
    url = f"https://www.yellowpages.com/{hub.lower()}/{slug}"
    log.info(f"[YP] {cat['label']} @ {hub}")
    r = retry_get(url)
    if not r:
        return out
    soup = BeautifulSoup(r.text, "lxml")
    for card in soup.select(".result .info")[:20]:
        try:
            name_el = card.select_one(".business-name")
            name = name_el.text.strip() if name_el else ""
            if not name:
                continue

            phone_el = card.select_one(".phones")
            phone = normalize_phone(phone_el.text if phone_el else "")

            site_el = card.select_one("a.track-visit-website")
            website = site_el.get("href", "") if site_el else ""
            if website and "yellowpages.com" in website:
                website = ""

            street_el = card.select_one(".street-address")
            city_el = card.select_one(".locality")  # often "San Antonio, TX 78201"
            raw_city = city_el.text.strip() if city_el else ""
            city = raw_city.split(",")[0].strip() if raw_city else "San Antonio"
            zip_m = re.search(r"\b(\d{5})\b", raw_city)
            zipc = zip_m.group(1) if zip_m else ""
            street = street_el.text.strip() if street_el else ""
            address = ", ".join([p for p in [street, f"{city}, {STATE} {zipc}".strip()] if p]).strip(", ")

            out.append({
                "company_name": name,
                "category": cat["label"],
                "category_slug": slug,
                "phone": phone,
                "website": website,
                "street": street,
                "city": city,
                "state": STATE,
                "zip": zipc,
                "address": address,
                "source": "YellowPages",
            })
        except Exception as e:
            log.debug(f"[YP] card parse error: {e}")
    return out


# ── Dedup ──────────────────────────────────────────────────────────────────────
def deduplicate(leads: list) -> list:
    seen = {}
    for l in leads:
        k = business_key(l["company_name"], l.get("phone", ""))
        if k not in seen:
            seen[k] = l
        elif not seen[k].get("website") and l.get("website"):
            seen[k] = l
    return list(seen.values())


# ── Quality gate ───────────────────────────────────────────────────────────────
def passes_quality(l: dict) -> tuple:
    """Return (ok:bool, reasons_failed:list)."""
    fails = []
    # revenue band
    rev = l.get("est_revenue", 0)
    if not (REV_MIN <= rev <= REV_MAX):
        fails.append(f"revenue ${rev:,} outside ${REV_MIN:,}-${REV_MAX:,}")
    # geography
    if not in_metro(l.get("city", "")):
        fails.append(f"city '{l.get('city')}' not in SA metro")
    # address + phone present
    if not l.get("address"):
        fails.append("missing address")
    if not l.get("phone"):
        fails.append("missing phone")
    # not a logistics company
    tok = is_excluded_company(l.get("company_name", ""), l.get("website", ""))
    if tok:
        fails.append(f"excluded keyword '{tok}'")
    # decision-maker info present (name OR at minimum a targeted title + a way to reach)
    if not (l.get("dm_name") or l.get("dm_title")):
        fails.append("missing decision-maker info")
    # pitch present and short
    if not l.get("pitch") or len(l["pitch"]) > 320:
        fails.append("pitch missing/too long")
    return (len(fails) == 0, fails)


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 64)
    log.info("FREQUENT FREIGHT 2.5 — SA-metro LTL/freight shipper prospecting")
    log.info(f"Revenue band: ${REV_MIN:,}-${REV_MAX:,} | hubs: {len(SEARCH_HUBS)} | niches: {len(SHIPPER_CATEGORIES)}")
    log.info("=" * 64)

    raw = []
    for slug, cat in SHIPPER_CATEGORIES.items():
        for hub in SEARCH_HUBS:
            try:
                raw.extend(scrape_yellowpages(slug, hub, cat))
                time.sleep(1.3)
            except Exception as e:
                log.error(f"scrape error {slug}@{hub}: {e}")

    log.info(f"Raw records: {len(raw)}")
    unique = deduplicate(raw)
    log.info(f"After dedup: {len(unique)}")

    enriched = []
    for i, biz in enumerate(unique, 1):
        cat = SHIPPER_CATEGORIES[biz["category_slug"]]
        log.info(f"[{i}/{len(unique)}] {biz['company_name']}")

        # EARLY EXCLUSION — skip logistics-type companies before any enrichment
        tok = is_excluded_company(biz["company_name"], biz.get("website", ""))
        if tok:
            log.info(f"   ✗ excluded ('{tok}')")
            continue

        # scrape the site once for text we reuse (desc, revenue signal, freight reason)
        site_text = ""
        if biz.get("website"):
            try:
                wurl = biz["website"] if biz["website"].startswith("http") else "https://" + biz["website"]
                r = requests.get(wurl, headers=HEADERS, timeout=10, allow_redirects=True)
                if r.status_code < 400:
                    site_text = BeautifulSoup(r.text, "lxml").get_text(" ", strip=True)[:8000]
            except Exception:
                pass

        # contacts (real only)
        contacts = scrape_site_contacts(biz.get("website", ""))
        biz["email"] = contacts["email"]
        biz["dm_name"] = contacts["dm_name"]
        biz["dm_title"] = contacts["dm_title"] or DM_TARGET_TITLES[0]  # default target = Owner
        biz["dm_phone"] = contacts["dm_phone"]
        biz["dm_email"] = contacts["dm_email"] or contacts["email"]

        # revenue estimate
        est, basis, conf = estimate_revenue(biz, site_text, cat)
        biz["est_revenue"] = est
        biz["revenue_basis"] = basis
        biz["revenue_confidence"] = conf

        # description + freight reason + pitch
        biz["description"] = build_description(biz, cat, site_text)
        biz["freight_reason"] = build_freight_reason(biz, cat, site_text)
        biz["pitch"] = build_pitch(biz, cat)

        # flags for human reviewer
        flags = []
        if not biz["dm_name"]:
            flags.append("NEEDS_DM_NAME")
        if not biz["dm_email"]:
            flags.append("NEEDS_DM_EMAIL")
        if conf == "low":
            flags.append("REVENUE_EST_LOW_CONF")
        biz["flags"] = flags

        enriched.append(biz)

    # quality gate
    qualified, rejected = [], []
    for l in enriched:
        ok, fails = passes_quality(l)
        l["qc_fail_reasons"] = fails
        (qualified if ok else rejected).append(l)

    # sort: revenue desc, then DM title priority
    qualified.sort(
        key=lambda x: (-x.get("est_revenue", 0),
                       TITLE_PRIORITY.get((x.get("dm_title", "") or "").lower(), 99))
    )

    log.info(f"Qualified: {len(qualified)} | Rejected: {len(rejected)}")

    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "fetched_at": now,
        "system": "Frequent Freight 2.5",
        "location": "San Antonio metro, TX",
        "revenue_band": {"min": REV_MIN, "max": REV_MAX},
        "total_scanned": len(enriched),
        "qualified": len(qualified),
        "rejected": len(rejected),
        "approved": False,  # set true per-lead by the reviewer / dashboard
        "leads": qualified,
        "rejected_leads": rejected,
    }

    for path in (DATA_DIR / "leads.json", DASHBOARD_DIR / "leads.json"):
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
        log.info(f"saved {path}")

    # review CSV (matches the collect-fields spec)
    csv_path = EXPORTS_DIR / "freight_leads_for_review.csv"
    fields = [
        "Company Name", "Website", "Address", "Phone", "Estimated Revenue",
        "Revenue Basis", "Description", "Freight Reason",
        "Decision-Maker Name", "Decision-Maker Title",
        "Decision-Maker Email", "Decision-Maker Phone", "Pitch", "Flags",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for l in qualified:
            w.writerow({
                "Company Name": l["company_name"],
                "Website": l.get("website", ""),
                "Address": l.get("address", ""),
                "Phone": l.get("phone", ""),
                "Estimated Revenue": f"${l['est_revenue']:,}",
                "Revenue Basis": l.get("revenue_basis", ""),
                "Description": l.get("description", ""),
                "Freight Reason": l.get("freight_reason", ""),
                "Decision-Maker Name": l.get("dm_name", ""),
                "Decision-Maker Title": l.get("dm_title", ""),
                "Decision-Maker Email": l.get("dm_email", ""),
                "Decision-Maker Phone": l.get("dm_phone", ""),
                "Pitch": l.get("pitch", ""),
                "Flags": "; ".join(l.get("flags", [])),
            })
    log.info(f"saved {csv_path}")
    log.info("=" * 64)
    log.info("DONE. Review leads.json in the dashboard, mark approvals,")
    log.info("then run: python pipedrive_push.py --approved-only")
    log.info("=" * 64)


if __name__ == "__main__":
    main()
