import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests


# ---------------------------------------------------------
# CIEL HR Job Fair Dashboard V2
#
# Event maintenance modes:
#
# 1. Full refresh:
#       python scripts/research.py
#
#    Performs:
#    - expired event cleanup
#    - registration deadline cleanup
#    - Tavily research
#    - validation
#    - duplicate detection
#    - merge
#    - nearest-date sorting
#    - events.json update
#
# 2. Cleanup only:
#       python scripts/research.py --cleanup-only
#
#    Performs:
#    - expired event cleanup
#    - registration deadline cleanup
#    - nearest-date sorting
#    - events.json update
#
#    IMPORTANT:
#    Cleanup-only mode makes ZERO Tavily API calls.
# ---------------------------------------------------------


ROOT = Path(__file__).resolve().parents[1]
EVENTS_FILE = ROOT / "events.json"

TAVILY_API_URL = "https://api.tavily.com/search"
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()

TODAY_DATE = date.today()
TODAY = TODAY_DATE.isoformat()


ALLOWED_CATEGORIES = [
    "District Employment Exchange Fairs",
]

# District-employment-only discovery gate.
# An event must clearly be run through official district employment machinery.
DISTRICT_EMPLOYMENT_TERMS = (
    "district employment exchange",
    "district employment office",
    "district employment officer",
    "district employment and career guidance centre",
    "district employment & career guidance centre",
    "district employment and career guidance center",
    "district employment & career guidance center",
    "district career centre",
    "district career center",
    "model career centre",
    "model career center",
    "employment exchange",
    "employment office",
    "career centre",
    "career center",
    "sewayojan",
    "rojgar sangam",
    "rojgaar sangam",
)

# These official systems commonly host district Employment Exchange/Career Centre fairs.
# Presence of a domain alone is not enough: the event metadata must still carry
# district/employment-office evidence.
OFFICIAL_EMPLOYMENT_DOMAINS = (
    "ncs.gov.in",
    "dge.gov.in",
    "sewayojan.up.nic.in",
    "rojgaarsangam.up.gov.in",
)



ALLOWED_SKILLS = [
    "BE /B Tech and Post Graduate",
    "Graduates",
    "ITI / Diploma",
    "10th Pass and Above",
    "Below 10th",
]


# Social-media-only pages are too weak to be the sole source
# for automatically adding a job fair to the dashboard.
BLOCKED_SOURCE_DOMAINS = {
    "facebook.com",
    "m.facebook.com",
    "instagram.com",
    "linkedin.com",
    "x.com",
    "twitter.com",
    "youtube.com",
    "youtu.be",
    "t.me",
}


# Employer/recruiter registration-link verification
URL_CHECK_TIMEOUT_SECONDS = 20
URL_CHECK_MAX_BYTES = 1500000

EMPLOYER_PHRASES = (
    "employer registration", "employer participation", "register as employer",
    "register as an employer", "recruiter registration", "recruiter participation",
    "register as recruiter", "for employers", "for recruiters", "hire candidates",
    "hire talent", "exhibitor registration", "exhibitor participation",
    "become an exhibitor", "register as exhibitor", "book a booth",
    "reserve a booth", "company registration", "corporate participation",
)
EMPLOYER_ROLE_TERMS = (
    "employer", "recruiter", "hiring", "exhibitor", "sponsor",
    "company", "corporate", "industry partner", "booth", "stall",
)
ACTION_TERMS = (
    "register", "registration", "participate", "participation",
    "apply", "application", "sign up", "signup", "book", "reserve",
)
CANDIDATE_PHRASES = (
    "job seeker registration", "jobseeker registration", "candidate registration",
    "student registration", "register as job seeker", "register as candidate",
    "apply for jobs", "find a job", "upload resume", "submit resume",
)
CANDIDATE_URL_HINTS = (
    "jobseeker", "job-seeker", "candidate", "student", "resume",
    "apply-job", "apply-for-job",
)
EMPLOYER_URL_HINTS = (
    "employer", "recruiter", "exhibitor", "sponsor", "company",
    "corporate", "hire", "booth", "stall", "participat", "register",
    "registration", "signup", "form",
)


SEARCH_QUERIES = [
    (
        "site:ncs.gov.in job fair district employment exchange career centre "
        f"India upcoming after {TODAY} employer participation"
    ),
    (
        "upcoming District Employment Exchange job fair India "
        f"after {TODAY} employer participation"
    ),
    (
        "upcoming District Employment Office rojgar mela India "
        f"after {TODAY} employers"
    ),
    (
        "upcoming District Employment and Career Guidance Centre job fair India "
        f"after {TODAY} employers"
    ),
    (
        "upcoming Model Career Centre district job fair India "
        f"after {TODAY} employer participation"
    ),
    (
        "site:gov.in district employment exchange job fair rojgar mela "
        f"2026 2027 after {TODAY} employer"
    ),
    (
        "site:nic.in district employment office job fair rojgar mela "
        f"2026 2027 after {TODAY} employer"
    ),
]



def load_existing_events():
    print("Loading existing events...")

    if not EVENTS_FILE.exists():
        print("events.json does not exist yet.")
        return []

    with EVENTS_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise RuntimeError(
            "events.json must contain a JSON array."
        )

    print(f"Existing events: {len(data)}")
    return data


def normalize_url(url):
    if not url:
        return ""

    url = str(url).strip()

    try:
        parts = urlsplit(url)

        scheme = parts.scheme.lower() or "https"
        netloc = parts.netloc.lower().replace("www.", "")
        path = parts.path.rstrip("/")

        return urlunsplit(
            (
                scheme,
                netloc,
                path,
                "",
                "",
            )
        )

    except Exception:
        return url.lower().rstrip("/")


def get_domain(url):
    if not url:
        return ""

    try:
        domain = urlsplit(url).netloc.lower()

        if domain.startswith("www."):
            domain = domain[4:]

        return domain

    except Exception:
        return ""


def normalize_text(value):
    return re.sub(
        r"\s+",
        " ",
        str(value or ""),
    ).strip().lower()


def parse_iso_date(value):
    """
    Convert YYYY-MM-DD into a date object.

    Returns None for anything else.
    """

    value = str(value or "").strip()

    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}",
        value,
    ):
        return None

    try:
        return date.fromisoformat(value)

    except ValueError:
        return None


def is_recurring_date(value):
    """
    Accept explicit recurring/rolling schedule descriptions.
    """

    text = normalize_text(value)

    return (
        text.startswith("recurring")
        or text.startswith("rolling")
    )


def normalize_event_date(value):
    """
    Allowed dashboard event-date forms:

    YYYY-MM-DD

    OR

    Recurring — description

    If Tavily gives a range such as:

    2026-10-09 to 2026-10-10

    we store the first date:

    2026-10-09
    """

    value = str(value or "").strip()

    if not value:
        return None

    if is_recurring_date(value):
        return value

    exact_date = parse_iso_date(value)

    if exact_date:
        return exact_date.isoformat()

    # Normalize date ranges to the first/start date.
    range_match = re.match(
        r"^\s*(\d{4}-\d{2}-\d{2})"
        r"\s*(?:to|–|—|-)\s*"
        r"(\d{4}-\d{2}-\d{2})\s*$",
        value,
        flags=re.I,
    )

    if range_match:
        first_date = parse_iso_date(
            range_match.group(1)
        )

        second_date = parse_iso_date(
            range_match.group(2)
        )

        if (
            first_date
            and second_date
            and second_date >= first_date
        ):
            print(
                "Date range normalized: "
                f"{value} → "
                f"{first_date.isoformat()}"
            )

            return first_date.isoformat()

    return None


def registration_is_open(reg_close):
    """
    Reject newly discovered events whose explicit registration
    deadline is already in the past.

    Rolling or blank deadlines remain allowed.
    """

    value = str(
        reg_close or ""
    ).strip()

    if not value:
        return True

    if normalize_text(value) == "rolling":
        return True

    deadline = parse_iso_date(value)

    if deadline is None:
        # Do not invent or reinterpret an unknown deadline.
        # Preserve it if Tavily supplied a non-date value.
        return True

    return deadline >= TODAY_DATE


def event_is_upcoming(event_date):
    """
    Fixed-date events must be today or later.

    Explicitly recurring events are allowed.
    """

    if is_recurring_date(event_date):
        return True

    parsed = parse_iso_date(event_date)

    if parsed is None:
        return False

    return parsed >= TODAY_DATE


def source_is_allowed(url):
    """
    Reject social-media-only sources.
    """

    domain = get_domain(url)

    if not domain:
        return False

    for blocked in BLOCKED_SOURCE_DOMAINS:
        if (
            domain == blocked
            or domain.endswith("." + blocked)
        ):
            return False

    return True


def is_district_employment_event(name, org, region, category, url):
    """
    Strict scope gate for this dashboard.

    Accept only events whose supplied event metadata clearly identifies an
    Employment Exchange / District Employment Office / Career Centre style
    organizer. An official-looking domain by itself is never enough.
    """
    text = normalize_text(
        " ".join(
            [
                str(name or ""),
                str(org or ""),
                str(region or ""),
                str(category or ""),
            ]
        )
    )

    matched_terms = [
        term for term in DISTRICT_EMPLOYMENT_TERMS
        if term in text
    ]

    if not matched_terms:
        return False, "no District Employment Exchange/Career Centre evidence in event metadata"

    # Reject generic use of 'career centre' unless the record also carries
    # employment/district/government context.
    generic_career_only = all(
        term in ("career centre", "career center")
        for term in matched_terms
    )
    if generic_career_only and not any(
        x in text for x in (
            "district", "employment", "government", "govt",
            "ncs", "national career service", "model career",
        )
    ):
        return False, "generic career-centre wording is not enough"

    return True, "district employment evidence: " + ", ".join(matched_terms[:2])


def cleanup_existing_events(events):
    """
    Remove events from the active events.json when they
    are no longer actionable.

    Rules:

    - Fixed event date before today -> remove
    - Explicit ISO registration deadline before today -> remove
    - Event date today -> keep
    - Registration deadline today -> keep
    - Blank/unknown registration deadline -> keep until event passes
    - Recurring/rolling event dates -> keep unless an explicit
      ISO registration deadline has passed

    This function performs NO Tavily calls.
    """

    kept = []
    removed = []

    for event in events:
        event_name = str(
            event.get(
                "name",
                "Unnamed event",
            )
        ).strip()

        event_date = str(
            event.get(
                "date",
                "",
            )
            or ""
        ).strip()

        reg_close = str(
            event.get(
                "regClose",
                "",
            )
            or ""
        ).strip()

        remove_reason = None

        # -------------------------------------------------
        # Rule 1:
        # Remove fixed-date events whose event date passed.
        # -------------------------------------------------

        if not is_recurring_date(event_date):
            parsed_event_date = parse_iso_date(
                event_date
            )

            if (
                parsed_event_date
                and parsed_event_date < TODAY_DATE
            ):
                remove_reason = (
                    "event date passed "
                    f"({event_date})"
                )

        # -------------------------------------------------
        # Rule 2:
        # Remove events whose explicit registration
        # deadline passed.
        # -------------------------------------------------

        if (
            remove_reason is None
            and reg_close
        ):
            if normalize_text(reg_close) != "rolling":
                parsed_deadline = parse_iso_date(
                    reg_close
                )

                if (
                    parsed_deadline
                    and parsed_deadline < TODAY_DATE
                ):
                    remove_reason = (
                        "registration deadline passed "
                        f"({reg_close})"
                    )

        # -------------------------------------------------
        # Keep or remove
        # -------------------------------------------------

        if remove_reason:
            removed.append(event)

            print(
                "REMOVED EXPIRED EVENT: "
                f"{event_name} — "
                f"{remove_reason}"
            )

        else:
            kept.append(event)

    print()

    print(
        f"Existing events kept: "
        f"{len(kept)}"
    )

    print(
        "Expired/unactionable events removed: "
        f"{len(removed)}"
    )

    return kept, removed


def tavily_search(query):
    if not TAVILY_API_KEY:
        raise RuntimeError(
            "TAVILY_API_KEY is missing. "
            "Add it as a GitHub Actions repository secret."
        )

    # Keep the complete Tavily query below its 1,500-character API limit.
    # Detailed district-scope and employer-link checks are enforced again in Python.
    structured_instruction = (
        " Return ONLY a JSON array of genuine upcoming India job fairs tied to a "
        "District Employment Exchange, District Employment Office, District Employment "
        "and Career Guidance Centre, Model Career Centre, or equivalent government "
        "district employment office. Exclude private, university, chamber, commercial, "
        "media, DEI and generic career fairs. Only include events on or after "
        + TODAY
        + ". Prefer official government/district/NCS/state employment sources. "
        "Set category to 'District Employment Exchange Fairs'. Never invent the "
        "district-employment connection. url must be an EMPLOYER/RECRUITER participation "
        "or registration page for the fair, never a candidate/job-seeker link. Omit an "
        "event if no employer route can be verified. Return fields: "
        "name,date,region,category,org,fmt,fee,regClose,est,skills,url. "
        "Use null/empty for unverified optional values. Return plain https URLs. "
        "No markdown or explanation."
    )

    payload = {
        "query": (
            query
            + structured_instruction
        ),
        "search_depth": "advanced",
        "topic": "general",
        "max_results": 10,
        "include_answer": "advanced",
        "include_raw_content": False,
        "include_images": False,
        "include_favicon": False,
        "safe_search": True,
    }

    headers = {
        "Authorization": (
            f"Bearer {TAVILY_API_KEY}"
        ),
        "Content-Type": "application/json",
    }

    print()
    print("Searching Tavily:")
    print(query)

    response = requests.post(
        TAVILY_API_URL,
        headers=headers,
        json=payload,
        timeout=90,
    )

    if response.status_code != 200:
        raise RuntimeError(
            "Tavily API error "
            f"{response.status_code}: "
            f"{response.text}"
        )

    return response.json()


def extract_json_array(answer):
    if not answer:
        return []

    text = str(answer).strip()

    # Remove Markdown fences if returned.
    text = re.sub(
        r"^```(?:json)?\s*",
        "",
        text,
        flags=re.I,
    )

    text = re.sub(
        r"\s*```$",
        "",
        text,
    )

    try:
        parsed = json.loads(text)

        return (
            parsed
            if isinstance(parsed, list)
            else []
        )

    except json.JSONDecodeError:
        pass

    # Fallback:
    # locate first JSON array-looking block.
    match = re.search(
        r"\[[\s\S]*\]",
        text,
    )

    if not match:
        print(
            "No JSON array found in Tavily answer."
        )
        return []

    try:
        parsed = json.loads(
            match.group(0)
        )

        return (
            parsed
            if isinstance(parsed, list)
            else []
        )

    except json.JSONDecodeError as exc:
        print(
            "Could not parse Tavily JSON answer: "
            f"{exc}"
        )

        return []


def clean_source_url(value):
    """
    Convert Tavily/LLM URL output into a plain URL.

    Also repairs accidental Markdown-link strings such as:

    [https://example.com](https://example.com)
    """

    text = str(
        value or ""
    ).strip()

    markdown_match = re.fullmatch(
        r"\[[^\]]+\]\((https?://[^)]+)\)",
        text,
    )

    if markdown_match:
        text = markdown_match.group(1).strip()

    plain_match = re.search(
        r"https?://[^\s)\]]+",
        text,
    )

    if plain_match:
        return plain_match.group(0).rstrip(
            ".,;"
        )

    return text



def _page_text(html):
    text = re.sub(r"(?is)<(script|style|noscript|template)\b[^>]*>.*?</\1>", " ", str(html or ""))
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    for a, b in {"&nbsp;":" ", "&amp;":"&", "&quot;":"\"", "&#39;":"'"}.items():
        text = text.replace(a, b)
    return normalize_text(text)


def _url_has_hint(url, hints):
    try:
        parts = urlsplit(str(url or ""))
        text = normalize_text(parts.path.replace("/", " ") + " " + parts.query.replace("&", " "))
    except Exception:
        return False
    return any(h in text for h in hints)


def _fetch_page(url):
    """Fetch one page and return final URL + HTML without trusting redirects alone."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; CIEL-HR-JobFair-LinkVerifier/1.1)",
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
    }
    try:
        r = requests.get(url, headers=headers, timeout=URL_CHECK_TIMEOUT_SECONDS, allow_redirects=True, stream=True)
    except requests.RequestException as exc:
        return False, url, "", "request failed: " + type(exc).__name__
    try:
        final_url = clean_source_url(r.url or url)
        if r.status_code < 200 or r.status_code >= 400:
            return False, final_url, "", f"HTTP {r.status_code}"
        if not source_is_allowed(final_url):
            return False, final_url, "", "redirected to blocked/social domain"
        ctype = normalize_text(r.headers.get("Content-Type", ""))
        if ctype and not any(x in ctype for x in ("text/html", "application/xhtml+xml", "text/plain")):
            return False, final_url, "", "non-page content type: " + ctype
        chunks = []
        total = 0
        for chunk in r.iter_content(32768):
            if not chunk:
                continue
            left = URL_CHECK_MAX_BYTES - total
            if left <= 0:
                break
            chunk = chunk[:left]
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)
        try:
            html = raw.decode(r.encoding or "utf-8", errors="replace")
        except LookupError:
            html = raw.decode("utf-8", errors="replace")
        return True, final_url, html, "ok"
    finally:
        r.close()


def _extract_employer_registration_links(html, base_url):
    """Find direct employer/recruiter registration links embedded in a listing/event page."""
    candidates = []
    pattern = re.compile(
        r"(?is)<a\b[^>]*?href\s*=\s*([\"'])(.*?)\1[^>]*>(.*?)</a>"
    )
    for match in pattern.finditer(str(html or "")):
        href = str(match.group(2) or "").strip()
        anchor_html = match.group(3) or ""
        anchor_text = _page_text(anchor_html)
        absolute = clean_source_url(urljoin(base_url, href))
        if not absolute.lower().startswith(("http://", "https://")):
            continue
        if not source_is_allowed(absolute):
            continue
        signal = normalize_text(anchor_text + " " + href + " " + absolute)
        candidate_signal = any(x in signal for x in CANDIDATE_PHRASES) or _url_has_hint(absolute, CANDIDATE_URL_HINTS)
        employer_signal = any(x in signal for x in EMPLOYER_PHRASES) or any(x in signal for x in EMPLOYER_ROLE_TERMS) or _url_has_hint(absolute, EMPLOYER_URL_HINTS)
        action_signal = any(x in signal for x in ACTION_TERMS) or "reg" in signal
        if employer_signal and action_signal and not candidate_signal:
            score = 0
            if "register as employer" in signal or "employer registration" in signal:
                score += 100
            if "recruiter" in signal or "employer" in signal:
                score += 40
            if "jobfair-emp-reg" in signal or "emp-reg" in signal:
                score += 40
            if _url_has_hint(absolute, EMPLOYER_URL_HINTS):
                score += 20
            candidates.append((score, absolute, anchor_text))
    candidates.sort(key=lambda x: x[0], reverse=True)
    seen = set()
    result = []
    for item in candidates:
        normalized = normalize_url(item[1])
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(item)
    return result[:10]


def _employer_evidence(final_url, html):
    text = normalize_text(_page_text(html) + " " + html[:250000] + " " + final_url)
    employer_strong = [x for x in EMPLOYER_PHRASES if x in text]
    roles = [x for x in EMPLOYER_ROLE_TERMS if x in text]
    actions = [x for x in ACTION_TERMS if x in text]
    candidate = [x for x in CANDIDATE_PHRASES if x in text]
    employer_url = _url_has_hint(final_url, EMPLOYER_URL_HINTS)
    candidate_url = _url_has_hint(final_url, CANDIDATE_URL_HINTS)
    if candidate and not employer_strong:
        return False, "candidate/job-seeker registration signals: " + ", ".join(candidate[:2])
    if candidate_url and not employer_url and not employer_strong:
        return False, "URL path appears candidate/job-seeker facing"
    if employer_strong:
        return True, "explicit employer-facing evidence: " + ", ".join(employer_strong[:2])
    if roles and actions:
        return True, "employer role + participation action verified"
    return False, "no verifiable employer/recruiter registration or participation evidence"


def verify_employer_registration_url(url):
    """Verify employer-side participation and prefer the deepest direct employer form."""
    ok, final_url, html, reason = _fetch_page(url)
    if not ok:
        return False, final_url, reason

    page_ok, page_reason = _employer_evidence(final_url, html)

    # A listing/event page may be employer-capable but still not be the final form.
    # Prefer a direct employer-registration destination when one is explicitly linked.
    deep_links = _extract_employer_registration_links(html, final_url)
    for _score, deep_url, anchor_text in deep_links:
        if normalize_url(deep_url) == normalize_url(final_url):
            continue
        deep_ok, deep_final_url, deep_html, deep_fetch_reason = _fetch_page(deep_url)
        if not deep_ok:
            print(
                "Employer registration deep link skipped: "
                f"{deep_url} ({deep_fetch_reason})"
            )
            continue
        verified, deep_reason = _employer_evidence(deep_final_url, deep_html)
        if verified:
            label = anchor_text or "employer registration link"
            return True, deep_final_url, (
                "direct employer registration page resolved from "
                f"'{label}'; {deep_reason}"
            )

    if page_ok:
        return True, final_url, page_reason + "; no deeper verified employer form found"

    return False, final_url, page_reason


def infer_category(
    name,
    org,
    region,
    fmt,
):
    """
    Infer only when there is a strong textual signal.

    Never default every unknown event to
    District Employment Exchange Fairs.
    """

    text = normalize_text(
        " ".join(
            [
                name,
                org,
                region,
                fmt,
            ]
        )
    )

    if any(
        term in text
        for term in (
            "hackerx",
            "tech job fair",
            "startup",
            "developer",
        )
    ):
        return (
            "Sector-Specific Tech & Startup "
            "Aggregators"
        )

    if any(
        term in text
        for term in (
            "ieee",
            "returning mother",
            "returning women",
            "diversity",
            "dei",
        )
    ):
        return (
            "Equity, Diversity & Inclusion "
            "(DEI) Foundations"
        )

    if any(
        term in text
        for term in (
            "naukri",
            "media",
            "publication",
        )
    ):
        return (
            "Media House & Publication "
            "Job Fairs"
        )

    if any(
        term in text
        for term in (
            "nielit",
            "sector skill council",
            "skill council",
            "bfsissc",
            "tssc",
        )
    ):
        return (
            "Skill Sector Councils (SSCs) "
            "Job Fairs"
        )

    if any(
        term in text
        for term in (
            "chamber of commerce",
            "industrial association",
            "cii",
            "ficci",
            "assocham",
        )
    ):
        return (
            "Chamber of Commerce & "
            "Industrial Association Fairs"
        )

    if any(
        term in text
        for term in (
            "ncs",
            "nsdc",
            "msde",
            "national career service",
        )
    ):
        return (
            "NSDC & MSDE Flagship "
            "Rozgar Melas"
        )

    if any(
        term in text
        for term in (
            "rojgar mela",
            "rozgar mela",
            "rojgaar mela",
            "rozgar melava",
            "employment exchange",
            "district employment",
            "sewayojan",
            "rojgar sangam",
            "rojgaar sangam",
        )
    ):
        return (
            "District Employment Exchange "
            "Fairs"
        )

    # Unknown category is not enough evidence
    # for safe auto-publishing.
    return ""


def infer_format(
    name,
    category,
    org,
    region,
    url,
):
    """
    Infer an event format only when there is a strong signal.

    Rules:

    - Explicit virtual/online wording -> Virtual
    - Explicit hybrid wording -> Hybrid
    - Government/district Rozgar Mela / Employment Exchange
      style events with a physical city/district location
      -> In-person
    - HackerX city tech fairs -> In-person
    - Otherwise return "" so the candidate is rejected
      rather than guessed.
    """

    text = normalize_text(
        " ".join(
            [
                str(name or ""),
                str(category or ""),
                str(org or ""),
                str(region or ""),
                str(url or ""),
            ]
        )
    )

    # Strong virtual signals.
    if any(
        term in text
        for term in (
            "virtual",
            "online",
            "remote",
            "webinar",
        )
    ):
        return "Virtual"

    # Strong hybrid signals.
    if "hybrid" in text:
        return "Hybrid"

    # HackerX city fairs are physical events
    # unless explicitly marked otherwise.
    if (
        "hackerx" in text
        and any(
            term in text
            for term in (
                "mumbai",
                "hyderabad",
                "pune",
                "chennai",
                "bengaluru",
                "bangalore",
                "delhi",
                "gurugram",
                "noida",
            )
        )
    ):
        return "In-person"

    # Government / district job-fair style events
    # are typically physical when the record names
    # a concrete district/city and is not virtual.
    govt_job_fair_terms = (
        "rojgar mela",
        "rozgar mela",
        "rojgaar mela",
        "rozgar melava",
        "employment exchange",
        "district employment",
        "rojgar sangam",
        "rojgaar sangam",
        "sewayojan",
        "job drive",
    )

    if any(
        term in text
        for term in govt_job_fair_terms
    ):
        if not any(
            term in text
            for term in (
                "pan-india",
                "pan india",
                "all india",
            )
        ):
            return "In-person"

    return ""


def clean_event(raw):
    if not isinstance(raw, dict):
        return None

    name = str(
        raw.get(
            "name",
            "",
        )
    ).strip()

    raw_event_date = str(
        raw.get(
            "date",
            "",
        )
    ).strip()

    region = str(
        raw.get(
            "region",
            "",
        )
    ).strip()

    category = str(
        raw.get(
            "category",
            "",
        )
    ).strip()

    org = str(
        raw.get(
            "org",
            "",
        )
    ).strip()

    fmt = str(
        raw.get(
            "fmt",
            "",
        )
    ).strip()

    fee = str(
        raw.get(
            "fee",
            "",
        )
    ).strip()

    reg_close = str(
        raw.get(
            "regClose",
            "",
        )
        or ""
    ).strip()

    if normalize_text(reg_close) in {
        "none",
        "null",
        "unknown",
        "n/a",
        "na",
    }:
        reg_close = ""

    url = clean_source_url(
        raw.get(
            "url",
            "",
        )
    )

    # -----------------------------------------------------
    # Basic required-field validation
    # -----------------------------------------------------

    if not name:
        print(
            "Rejected candidate: "
            "missing event name."
        )
        return None

    if not url:
        print(
            f"Rejected candidate: {name} — "
            "missing source URL."
        )
        return None

    if not url.lower().startswith(
        (
            "http://",
            "https://",
        )
    ):
        print(
            f"Rejected candidate: {name} — "
            "invalid source URL."
        )
        return None

    # -----------------------------------------------------
    # Source-quality validation
    # -----------------------------------------------------

    if not source_is_allowed(url):
        print(
            f"Rejected candidate: {name} — "
            "social-media-only/blocked source: "
            f"{url}"
        )

        return None

    # -----------------------------------------------------
    # District Employment Exchange / Career Centre scope gate
    # -----------------------------------------------------
    district_ok, district_reason = is_district_employment_event(
        name, org, region, category, url
    )
    if not district_ok:
        print(
            f"Rejected candidate: {name} — outside District Employment "
            f"Exchange/Career Centre scope: {district_reason}"
        )
        return None

    # The dashboard now has one deliberate event category only.
    category = "District Employment Exchange Fairs"
    print(f"District employment scope verified: {name} ({district_reason})")

    # -----------------------------------------------------
    # Employer/recruiter registration-link verification
    # PRESERVED: HTTP check -> redirects -> employer-vs-candidate
    # -> deeper employer registration link resolution.
    # -----------------------------------------------------
    print(f"Verifying employer registration URL: {name}")
    link_ok, verified_url, link_reason = verify_employer_registration_url(url)
    if not link_ok:
        print(
            f"Rejected candidate: {name} — employer registration URL not verified: "
            f"{link_reason} | {verified_url}"
        )
        return None
    if verified_url != url:
        print(f"Employer registration redirect resolved: {url} → {verified_url}")
    print(f"Employer registration URL verified: {verified_url} ({link_reason})")
    url = verified_url

    # -----------------------------------------------------
    # Event date validation / normalization
    # -----------------------------------------------------

    event_date = normalize_event_date(
        raw_event_date
    )

    if not event_date:
        print(
            f"Rejected candidate: {name} — "
            "invalid event date: "
            f"{raw_event_date}"
        )

        return None

    if not event_is_upcoming(event_date):
        print(
            f"Rejected candidate: {name} — "
            "event date already passed: "
            f"{event_date}"
        )

        return None

    # -----------------------------------------------------
    # Registration deadline validation
    # -----------------------------------------------------

    if not registration_is_open(
        reg_close
    ):
        print(
            f"Rejected candidate: {name} — "
            "registration already closed: "
            f"{reg_close}"
        )

        return None

    # -----------------------------------------------------
    # Category normalization
    # -----------------------------------------------------
    # Scope was already verified above. Keep one category only.
    category = "District Employment Exchange Fairs"

    # -----------------------------------------------------
    # Format normalization
    # -----------------------------------------------------

    fmt_lookup = {
        "in person": "In-person",
        "in-person": "In-person",
        "physical": "In-person",
        "offline": "In-person",
        "virtual": "Virtual",
        "online": "Virtual",
        "hybrid": "Hybrid",
    }

    fmt = fmt_lookup.get(
        fmt.lower(),
        fmt,
    )

    if fmt not in {
        "In-person",
        "Virtual",
        "Hybrid",
    }:
        inferred_fmt = infer_format(
            name,
            category,
            org,
            region,
            url,
        )

        if inferred_fmt:
            fmt = inferred_fmt

            print(
                "Format inferred: "
                f"{name} → {fmt}"
            )

        else:
            print(
                f"Rejected candidate: {name} — "
                "unknown event format: "
                f"{fmt or 'blank'}"
            )

            return None

    # -----------------------------------------------------
    # Fee normalization
    # -----------------------------------------------------

    fee_text = normalize_text(fee)

    if fee_text in {
        "free",
        "no fee",
        "no fees",
        "free participation",
        "₹0",
        "rs 0",
        "rs. 0",
    }:
        fee = "Free"

    else:
        # Never infer Free from a missing/unclear fee.
        fee = "On request"

    # -----------------------------------------------------
    # Footfall normalization
    # -----------------------------------------------------

    raw_est = raw.get(
        "est",
        None,
    )

    if raw_est in (
        None,
        "",
        "None",
        "null",
        "unknown",
        "Unknown",
        "N/A",
        "n/a",
    ):
        est = None

    else:
        try:
            est = int(
                str(raw_est)
                .replace(",", "")
                .strip()
            )

            if est <= 0:
                est = None

        except (
            TypeError,
            ValueError,
        ):
            est = None

    # -----------------------------------------------------
    # Skills normalization
    # -----------------------------------------------------

    raw_skills = raw.get(
        "skills",
        [],
    )

    if raw_skills is None:
        raw_skills = []

    elif isinstance(
        raw_skills,
        str,
    ):
        raw_skills = [
            raw_skills
        ]

    elif not isinstance(
        raw_skills,
        (
            list,
            tuple,
            set,
        ),
    ):
        raw_skills = []

    skills = [
        skill
        for skill in raw_skills
        if skill in ALLOWED_SKILLS
    ]

    if not skills:
        # Do not silently label an event
        # as graduate-only.
        skills = []

    return {
        "name": name,
        "date": event_date,
        "region": region,
        "category": category,
        "org": org,
        "fmt": fmt,
        "fee": fee,
        "regClose": reg_close,
        "est": est,
        "skills": skills,
        "url": url,
    }


def looks_duplicate(
    candidate,
    existing,
):
    """
    A shared organizer landing page alone is NOT enough
    to mark two different city/date events as duplicates.

    Duplicate when:

    1. normalized name + normalized date match

    OR

    2. normalized URL + normalized date match.
    """

    candidate_url = normalize_url(
        candidate.get("url")
    )

    candidate_name = normalize_text(
        candidate.get("name")
    )

    candidate_date = normalize_text(
        candidate.get("date")
    )

    for event in existing:
        existing_url = normalize_url(
            event.get("url")
        )

        existing_name = normalize_text(
            event.get("name")
        )

        existing_date = normalize_text(
            event.get("date")
        )

        same_name_and_date = (
            candidate_name
            and existing_name
            and candidate_name == existing_name
            and candidate_date == existing_date
        )

        same_url_and_date = (
            candidate_url
            and existing_url
            and candidate_url == existing_url
            and candidate_date == existing_date
        )

        if (
            same_name_and_date
            or same_url_and_date
        ):
            return True

    return False


def next_auto_number(events):
    highest = 0

    for event in events:
        event_id = str(
            event.get(
                "id",
                "",
            )
        )

        match = re.fullmatch(
            r"auto-(\d+)",
            event_id,
        )

        if match:
            highest = max(
                highest,
                int(
                    match.group(1)
                ),
            )

    return highest + 1


def research_new_events(existing_events):
    """
    Perform the full Tavily discovery process.

    This function is ONLY called during full refresh mode.
    Cleanup-only mode never calls this function.
    """

    discovered = []

    auto_number = next_auto_number(
        existing_events
    )

    successful_queries = 0

    for query in SEARCH_QUERIES:
        try:
            result = tavily_search(
                query
            )

            successful_queries += 1

        except Exception as exc:
            print(
                "Tavily query failed: "
                f"{exc}"
            )

            continue

        answer = result.get(
            "answer",
            "",
        )

        candidates = extract_json_array(
            answer
        )

        print(
            "Structured candidates returned: "
            f"{len(candidates)}"
        )

        for raw in candidates:
            event = clean_event(raw)

            if not event:
                continue

            combined = (
                existing_events
                + discovered
            )

            if looks_duplicate(
                event,
                combined,
            ):
                print(
                    "Duplicate skipped: "
                    f"{event['name']}"
                )

                continue

            event["id"] = (
                f"auto-{auto_number}"
            )

            auto_number += 1

            ordered_event = {
                "id": event["id"],
                "name": event["name"],
                "date": event["date"],
                "region": event["region"],
                "category": event["category"],
                "org": event["org"],
                "fmt": event["fmt"],
                "fee": event["fee"],
                "regClose": event["regClose"],
                "est": event["est"],
                "skills": event["skills"],
                "url": event["url"],
            }

            discovered.append(
                ordered_event
            )

            print(
                "NEW EVENT:",
                ordered_event["name"],
                "|",
                ordered_event["date"],
                "|",
                ordered_event["region"],
            )

    # If every Tavily request failed, make GitHub Actions
    # fail rather than showing a misleading green workflow.
    if successful_queries == 0:
        raise RuntimeError(
            "All Tavily searches failed. "
            "No successful Tavily API response "
            "was received."
        )

    print()

    print(
        "Tavily queries completed successfully: "
        f"{successful_queries}/"
        f"{len(SEARCH_QUERIES)}"
    )

    return discovered


def sort_events(events):
    """
    Sort fixed-date events nearest-first.

    Recurring/non-standard date descriptions appear
    after fixed ISO dates.
    """

    def sort_key(event):
        event_date = str(
            event.get(
                "date",
                "",
            )
        )

        if re.fullmatch(
            r"\d{4}-\d{2}-\d{2}",
            event_date,
        ):
            return (
                0,
                event_date,
            )

        return (
            1,
            event_date.lower(),
        )

    return sorted(
        events,
        key=sort_key,
    )


def save_events(events):
    with EVENTS_FILE.open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            events,
            f,
            ensure_ascii=False,
            indent=2,
        )

        f.write("\n")


def parse_args():
    """
    Command-line modes:

    python scripts/research.py
        -> full cleanup + Tavily refresh

    python scripts/research.py --cleanup-only
        -> cleanup/sort only, zero Tavily calls
    """

    parser = argparse.ArgumentParser(
        description=(
            "CIEL HR Job Fair Dashboard "
            "event maintenance."
        )
    )

    parser.add_argument(
        "--cleanup-only",
        action="store_true",
        help=(
            "Clean expired/unactionable events "
            "and sort events.json without making "
            "any Tavily API calls."
        ),
    )

    return parser.parse_args()


def main():
    args = parse_args()

    print("=" * 60)

    if args.cleanup_only:
        print(
            "CIEL HR JOB FAIR DASHBOARD V2 — "
            "DAILY CLEANUP"
        )

    else:
        print(
            "CIEL HR JOB FAIR DASHBOARD V2 — "
            "CLEANUP + TAVILY REFRESH"
        )

    print("=" * 60)

    print(
        f"Today: {TODAY}"
    )

    # =====================================================
    # STEP 1 — LOAD CURRENT EVENTS.JSON
    # =====================================================

    existing_events = load_existing_events()

    # =====================================================
    # STEP 2 — CLEAN EXISTING EVENTS
    # =====================================================

    print()
    print(
        "Cleaning existing event data..."
    )

    (
        active_existing_events,
        removed_events,
    ) = cleanup_existing_events(
        existing_events
    )

    # =====================================================
    # CLEANUP-ONLY MODE
    # =====================================================
    #
    # Intended for the DAILY GitHub Action.
    #
    # No Tavily function is called in this branch.
    # Therefore daily cleanup consumes zero Tavily searches.
    # =====================================================

    if args.cleanup_only:
        cleaned_events = sort_events(
            active_existing_events
        )

        if cleaned_events == existing_events:
            print()

            print("=" * 60)
            print("DAILY CLEANUP COMPLETE")
            print("=" * 60)

            print(
                "No expired/unactionable events "
                "needed removal."
            )

            print(
                "Expired/unactionable events removed: "
                f"{len(removed_events)}"
            )

            print(
                "Total active events: "
                f"{len(cleaned_events)}"
            )

            print(
                "events.json remains unchanged."
            )

            return

        save_events(
            cleaned_events
        )

        print()

        print("=" * 60)
        print("DAILY CLEANUP COMPLETE")
        print("=" * 60)

        print(
            "Expired/unactionable events removed: "
            f"{len(removed_events)}"
        )

        print(
            "Total active events now: "
            f"{len(cleaned_events)}"
        )

        print(
            "events.json updated successfully."
        )

        return

    # =====================================================
    # FULL RESEARCH MODE
    # =====================================================
    #
    # Intended for the WEEKLY Monday GitHub Action.
    #
    # Flow:
    #
    # cleanup
    #   ↓
    # Tavily research
    #   ↓
    # validation
    #   ↓
    # duplicate detection
    #   ↓
    # merge
    #   ↓
    # sort
    #   ↓
    # save
    # =====================================================

    print()
    print(
        "Starting Tavily research..."
    )

    new_events = research_new_events(
        active_existing_events
    )

    # =====================================================
    # STEP 3 — MERGE CLEAN EXISTING + NEW EVENTS
    # =====================================================

    merged = (
        active_existing_events
        + new_events
    )

    # =====================================================
    # STEP 4 — SORT NEAREST UPCOMING EVENTS FIRST
    # =====================================================

    merged = sort_events(
        merged
    )

    # =====================================================
    # STEP 5 — SAVE ONLY IF SOMETHING CHANGED
    # =====================================================

    if merged == existing_events:
        print()

        print("=" * 60)
        print("WEEKLY REFRESH COMPLETE")
        print("=" * 60)

        print(
            "No event data changes detected."
        )

        print(
            "Expired/unactionable events removed: "
            f"{len(removed_events)}"
        )

        print(
            "New events added: "
            f"{len(new_events)}"
        )

        print(
            "Total active events: "
            f"{len(merged)}"
        )

        print(
            "events.json remains unchanged."
        )

        return

    save_events(
        merged
    )

    # =====================================================
    # FINAL WEEKLY REFRESH SUMMARY
    # =====================================================

    print()

    print("=" * 60)
    print("WEEKLY REFRESH COMPLETE")
    print("=" * 60)

    print(
        "Expired/unactionable events removed: "
        f"{len(removed_events)}"
    )

    print(
        "New events added: "
        f"{len(new_events)}"
    )

    print(
        "Total active events now: "
        f"{len(merged)}"
    )

    print(
        "events.json updated successfully."
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        print()

        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        sys.exit(1)
