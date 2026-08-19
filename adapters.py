"""
ATS adapters. Each returns a list of normalized dicts:
    {id, company, title, location, url, posted}

Every endpoint here is the company's OWN public job feed — the same data
their careers page renders from. There is no intermediary, so there is no
aggregator lag.
"""
from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import json
import re
import time
from datetime import datetime, timedelta, timezone

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}

_EIGHTFOLD_DETAIL_CACHE = {}
_WORKDAY_DETAIL_CACHE = {}
_SMARTRECRUITERS_DETAIL_CACHE = {}
_LINKEDIN_DETAIL_CACHE = {}
_APPLE_DETAIL_CACHE = {}
_ORACLE_DETAIL_CACHE = {}
_L3_DATE_CACHE = {}
_AMBIGUOUS_SOFTWARE_TITLE = re.compile(r"\bsoftware\s+(?:engineer|developer)\b", re.I)
_EXPLICIT_LEVEL_OR_EXCLUSION = re.compile(
    r"\b(?:intern(?:ship)?|co-?op|new\s*grad|graduate|university|campus|"
    r"early career|entry.level|junior|associate|apprentice|senior|sr\.?|"
    r"staff|principal|manager|director|lead|founding|architect|specialist|"
    r"mid[ .-]?(?:career|level)|quality|test|sme)\b|"
    r"\b(?:engineer|developer)\s*(?:[1-9]|i{1,3}|iv|v)\b|"
    r"\blevel\s*[1-9]\b",
    re.I,
)


def _uid(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def _plain(*values) -> str:
    """Collapse common ATS HTML/list description shapes into searchable text."""
    parts = []

    def collect(value):
        if not value:
            return
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            for key in ("text", "content", "description"):
                collect(value.get(key))
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item)

    for value in values:
        collect(value)
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", " ".join(parts)))
    return re.sub(r"\s+", " ", text).strip()


def _extract_eightfold_description(page: str) -> str:
    """Extract the full description embedded in an Eightfold detail page."""
    candidates = []
    for key in ("jobDescription", "description"):
        pattern = rf'"{key}"\s*:\s*("(?:\\.|[^"\\])*")'
        for match in re.finditer(pattern, page, re.DOTALL):
            try:
                value = json.loads(match.group(1))
            except (json.JSONDecodeError, TypeError):
                continue
            plain = _plain(value)
            if len(plain) >= 20:
                candidates.append(plain)
        if candidates:
            break
    return max(candidates, key=len, default="")


def _needs_software_detail(title: str, description: str = "") -> bool:
    """Limit slower page fetches to otherwise ambiguous software titles."""
    return (not description and bool(_AMBIGUOUS_SOFTWARE_TITLE.search(title or ""))
            and not _EXPLICIT_LEVEL_OR_EXCLUSION.search(title or ""))


def _needs_eightfold_detail(title: str, description: str = "") -> bool:
    """Backward-compatible name used by the Eightfold adapter and tests."""
    return _needs_software_detail(title, description)


async def _cached_json_description(client, cache: dict, url: str, *keys: str) -> str:
    """Fetch an ATS detail JSON document with a six-hour positive cache."""
    now = time.monotonic()
    cached = cache.get(url)
    if cached:
        fetched_at, description = cached
        if now - fetched_at < (21600 if description else 300):
            return description
    try:
        response = await client.get(url, headers={**UA, "Accept": "application/json"},
                                    timeout=25)
        response.raise_for_status()
        data = response.json()
        values = []
        for key in keys:
            value = data
            for part in key.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            values.append(value)
        description = _plain(*values)
    except Exception:
        description = ""
    cache[url] = (now, description)
    return description


async def _eightfold_detail_description(client, url: str, domain: str) -> str:
    now = time.monotonic()
    cached = _EIGHTFOLD_DETAIL_CACHE.get(url)
    if cached:
        fetched_at, description = cached
        ttl = 21600 if description else 300
        if now - fetched_at < ttl:
            return description
    try:
        response = await client.get(
            url, params={"domain": domain}, headers=UA, timeout=30,
        )
        response.raise_for_status()
        description = _extract_eightfold_description(response.text)
    except Exception:
        description = ""
    _EIGHTFOLD_DETAIL_CACHE[url] = (now, description)
    return description


def _iso(v) -> str:
    """Normalize a posting date, preserving exact UTC time when supplied."""
    if not v:
        return ""
    if isinstance(v, (int, float)):  # epoch millis (Lever, Ashby)
        ts = v / 1000 if v > 1e11 else v
        return (datetime.fromtimestamp(ts, timezone.utc)
                .isoformat(timespec="seconds").replace("+00:00", "Z"))
    s = str(v).strip()
    # Preserve ISO timestamps so jobs from the same day can be ordered exactly.
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        if len(s) == 10:
            return s
        try:
            parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return (parsed.astimezone(timezone.utc)
                    .isoformat(timespec="seconds").replace("+00:00", "Z"))
        except ValueError:
            return s[:10]
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Workday reports relative dates: "Posted Today" / "Posted 7 Days Ago"
    low = s.lower()
    if "posted" in low or "ago" in low:
        today = datetime.now().date()  # local time — Workday relative dates are US-centric
        if "today" in low:
            return today.strftime("%Y-%m-%d")
        if "yesterday" in low:
            return (today - timedelta(days=1)).strftime("%Y-%m-%d")
        m = re.search(r"(\d+)\+?\s*day", low)
        if m:
            return (today - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
        m = re.search(r"(\d+)\+?\s*month", low)
        if m:
            return (today - timedelta(days=30 * int(m.group(1)))).strftime("%Y-%m-%d")
    return ""  # unknown — better than a wrong date


# ---------------------------------------------------------------- Greenhouse
async def greenhouse(client, company, token, **kw):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    r = await client.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "id": _uid("gh", token, j["id"]),
            "company": company,
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "posted": _iso(j.get("first_published") or j.get("updated_at")),
            "description": _plain(j.get("content")),
        })
    return out


# --------------------------------------------------------------------- Lever
async def lever(client, company, token, **kw):
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    r = await client.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    out = []
    for j in r.json():
        cats = j.get("categories") or {}
        out.append({
            "id": _uid("lv", token, j.get("id")),
            "company": company,
            "title": j.get("text", ""),
            "location": cats.get("location", ""),
            "url": j.get("hostedUrl", ""),
            "posted": _iso(j.get("createdAt")),
            "description": _plain(j.get("descriptionPlain"), j.get("description"),
                                  j.get("lists"), j.get("additionalPlain")),
        })
    return out


# --------------------------------------------------------------------- Ashby
async def ashby(client, company, token, **kw):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    r = await client.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "id": _uid("ab", token, j.get("id")),
            "company": company,
            "title": j.get("title", ""),
            "location": j.get("location", ""),
            "url": j.get("jobUrl", ""),
            "posted": _iso(j.get("publishedAt")),
            "description": _plain(j.get("descriptionPlain"), j.get("descriptionHtml"),
                                  j.get("description")),
        })
    return out


# ------------------------------------------------------------ SmartRecruiters
async def smartrecruiters(client, company, token, **kw):
    url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100"
    r = await client.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    out = []
    for j in r.json().get("content", []):
        loc = j.get("location") or {}
        out.append({
            "id": _uid("sr", token, j.get("id")),
            "company": company,
            "title": j.get("name", ""),
            "location": ", ".join(filter(None, [loc.get("city"), loc.get("region")])),
            "url": f"https://jobs.smartrecruiters.com/{token}/{j.get('id')}",
            "posted": _iso(j.get("releasedDate")),
            "_detail_url": f"https://api.smartrecruiters.com/v1/companies/{token}/postings/{j.get('id')}",
        })
    ambiguous = [job for job in out if _needs_software_detail(
        job["title"], job.get("description", "")
    )]
    if ambiguous:
        detail_sem = asyncio.Semaphore(4)

        async def enrich(job):
            async with detail_sem:
                return await _cached_json_description(
                    client, _SMARTRECRUITERS_DETAIL_CACHE, job["_detail_url"],
                    "jobAd.sections.jobDescription.text",
                    "jobAd.sections.qualifications.text",
                    "jobAd.sections.additionalInformation.text",
                    "jobAd.sections.experience.text",
                )

        descriptions = await asyncio.gather(*(enrich(job) for job in ambiguous))
        for job, description in zip(ambiguous, descriptions):
            if description:
                job["description"] = description
    for job in out:
        job.pop("_detail_url", None)
    return out


# -------------------------------------------------------- LinkedIn corporate
def _linkedin_cards(page: str) -> list[dict]:
    """Parse LinkedIn's public company-jobs cards without requiring login."""
    cards = []
    for block in re.findall(r"<li[^>]*>(.*?)</li>", page or "", re.I | re.DOTALL):
        jid = re.search(r"urn:li:jobPosting:(\d+)", block)
        title = re.search(
            r'<h3[^>]*class="[^"]*base-search-card__title[^"]*"[^>]*>(.*?)</h3>',
            block, re.I | re.DOTALL,
        )
        location = re.search(
            r'<span[^>]*class="[^"]*job-search-card__location[^"]*"[^>]*>(.*?)</span>',
            block, re.I | re.DOTALL,
        )
        link = re.search(
            r'<a[^>]*class="[^"]*base-card__full-link[^"]*"[^>]*href="([^"]+)"',
            block, re.I | re.DOTALL,
        )
        posted = re.search(r'<time[^>]*datetime="([^"]+)"', block, re.I)
        if not (jid and title and link):
            continue
        cards.append({
            "jid": jid.group(1),
            "title": _plain(title.group(1)),
            "location": _plain(location.group(1)) if location else "",
            "url": html_lib.unescape(link.group(1)).split("?")[0],
            "posted": _iso(posted.group(1)) if posted else "",
        })
    return cards


async def _linkedin_detail_description(client, jid: str) -> str:
    now = time.monotonic()
    cached = _LINKEDIN_DETAIL_CACHE.get(jid)
    if cached:
        fetched_at, description = cached
        if now - fetched_at < (21600 if description else 300):
            return description
    try:
        response = await client.get(
            f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}",
            headers=UA, timeout=25,
        )
        response.raise_for_status()
        match = re.search(
            r'<div[^>]*class="[^"]*show-more-less-html__markup[^"]*"[^>]*>'
            r'(.*?)</div>', response.text, re.I | re.DOTALL,
        )
        description = _plain(match.group(1)) if match else ""
    except Exception:
        description = ""
    _LINKEDIN_DETAIL_CACHE[jid] = (now, description)
    return description


async def linkedin_company(client, company="LinkedIn", company_id="1337",
                           query="software engineer", location="United States", **kw):
    """LinkedIn's own public company-jobs feed, linked by careers.linkedin.com."""
    out, seen, start = [], set(), 0
    while start < 100:
        response = await client.get(
            "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
            params={
                "keywords": query, "location": location, "f_C": company_id,
                "sortBy": "DD", "start": start,
            },
            headers=UA, timeout=25,
        )
        response.raise_for_status()
        cards = _linkedin_cards(response.text)
        new_cards = [card for card in cards if card["jid"] not in seen]
        if not new_cards:
            break
        for card in new_cards:
            seen.add(card["jid"])
            out.append({
                "id": _uid("linkedin", company_id, card["jid"]),
                "company": company,
                "title": card["title"],
                "location": card["location"],
                "url": card["url"],
                "posted": card["posted"],
                "_linkedin_jid": card["jid"],
            })
        start += len(cards)

    candidates = [job for job in out if _AMBIGUOUS_SOFTWARE_TITLE.search(job["title"])]
    if candidates:
        detail_sem = asyncio.Semaphore(4)

        async def enrich(job):
            async with detail_sem:
                return await _linkedin_detail_description(client, job["_linkedin_jid"])

        descriptions = await asyncio.gather(*(enrich(job) for job in candidates))
        for job, description in zip(candidates, descriptions):
            if description:
                job["description"] = description
    for job in out:
        job.pop("_linkedin_jid", None)
    return out


# ------------------------------------------------------------------- Workday
async def workday(client, company, tenant, site, host, search="", subdomain=None, **kw):
    """Workday's CXS endpoint. host is like 'wd1' / 'wd5' / 'wd103'."""
    base = f"https://{subdomain or tenant}.{host}.myworkdayjobs.com"
    url = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    out, offset = [], 0
    while offset < 200:
        r = await client.post(
            url,
            json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": search},
            headers={**UA, "Content-Type": "application/json", "Accept": "application/json"},
            timeout=25,
        )
        r.raise_for_status()
        data = r.json()
        posts = data.get("jobPostings", [])
        if not posts:
            break
        for j in posts:
            path = j.get("externalPath", "")
            out.append({
                "id": _uid("wd", tenant, site, path),
                "company": company,
                "title": j.get("title", ""),
                "location": j.get("locationsText", ""),
                "url": f"{base}/{site}{path}",
                # Workday reports relative dates; _iso converts them to real ones
                "posted": _iso(j.get("postedOn")),
                "_detail_url": f"{base}/wday/cxs/{tenant}/{site}{path}",
            })
        offset += 20
        if offset >= data.get("total", 0):
            break
    ambiguous = [job for job in out if _needs_software_detail(
        job["title"], job.get("description", "")
    )]
    if ambiguous:
        detail_sem = asyncio.Semaphore(4)

        async def enrich(job):
            async with detail_sem:
                return await _cached_json_description(
                    client, _WORKDAY_DETAIL_CACHE, job["_detail_url"],
                    "jobPostingInfo.jobDescription", "jobPostingInfo.additionalJobDescription",
                )

        descriptions = await asyncio.gather(*(enrich(job) for job in ambiguous))
        for job, description in zip(ambiguous, descriptions):
            if description:
                job["description"] = description
    for job in out:
        job.pop("_detail_url", None)
    return out


# ----------------------------------------------------------------- Microsoft
async def microsoft(client, company="Microsoft", query="software engineer", **kw):
    """Microsoft runs its own careers API — this is why aggregators miss it.

    Tries the known hosts in order. If they all fail the error names each one,
    so you can tell a dead endpoint apart from a blocked network.
    """
    hosts = [
        "https://gcsservices.careers.microsoft.com/search/api/v1/search",
        "https://careers.microsoft.com/api/v1/search",
        "https://jobs.careers.microsoft.com/api/v1/search",
    ]
    params = f"?q={query}&l=en_us&pg=1&pgSz=20&o=Recent&flt=true"
    errors = []
    data = None
    for h in hosts:
        try:
            r = await client.get(h + params,
                                 headers={**UA, "Accept": "application/json"}, timeout=25)
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:
            errors.append(f"{h.split('/')[2]}: {type(e).__name__}")
    if data is None:
        raise RuntimeError("all Microsoft hosts failed -> " + "; ".join(errors))

    res = data.get("operationResult", {}).get("result", {})
    out = []
    for j in res.get("jobs", []):
        jid = j.get("jobId")
        props = j.get("properties") or {}
        out.append({
            "id": _uid("ms", jid),
            "company": company,
            "title": j.get("title", ""),
            "location": ", ".join(props.get("locations", []) or [])
                        or props.get("primaryLocation", ""),
            "url": f"https://jobs.careers.microsoft.com/global/en/job/{jid}",
            "posted": _iso(j.get("postingDate")),
        })
    return out


# -------------------------------------------------------------------- Amazon
async def amazon(client, company="Amazon", query="software development engineer intern", **kw):
    url = ("https://www.amazon.jobs/search.json"
           f"?base_query={query.replace(' ', '+')}&sort=recent&result_limit=100")
    r = await client.get(url, headers={**UA, "Accept": "application/json"}, timeout=25)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "id": _uid("az", j.get("id_icims")),
            "company": company,
            "title": j.get("title", ""),
            "location": j.get("location", ""),
            "url": "https://www.amazon.jobs" + (j.get("job_path") or ""),
            "posted": _iso(j.get("posted_date")),
        })
    return out


# ------------------------------------------------------------------ Workable
async def workable(client, company, token, **kw):
    url = f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"
    r = await client.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        loc = ", ".join(filter(None, [j.get("city"), j.get("state"), j.get("country")]))
        out.append({
            "id": _uid("wk", token, j.get("shortcode") or j.get("id")),
            "company": company,
            "title": j.get("title", ""),
            "location": loc,
            "url": j.get("url") or j.get("application_url", ""),
            "posted": _iso(j.get("published_on") or j.get("created_at")),
            "description": _plain(j.get("description"), j.get("requirements")),
        })
    return out


# ----------------------------------------------------------------- Recruitee
async def recruitee(client, company, token, **kw):
    url = f"https://{token}.recruitee.com/api/offers/"
    r = await client.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    out = []
    for j in r.json().get("offers", []):
        loc = ", ".join(filter(None, [j.get("city"), j.get("country")]))
        out.append({
            "id": _uid("rc", token, j.get("id")),
            "company": company,
            "title": j.get("title", ""),
            "location": loc or j.get("location", ""),
            "url": j.get("careers_url") or j.get("careers_apply_url", ""),
            "posted": _iso(j.get("published_at") or j.get("created_at")),
            "description": _plain(j.get("description"), j.get("requirements")),
        })
    return out


# ------------------------------------------------------------------- Apple
async def apple(client, company="Apple", query="software engineer", **kw):
    """Apple's current first-party careers search API.

    Undocumented means unstable: Apple can change the shape or add bot checks
    without warning. check.py will tell you the day it breaks.
    """
    # Since Apple's 2026 careers-site migration, the token is returned in a
    # response header by this endpoint (the previous /api/role/search API is
    # gone).  Using the same client preserves the session cookies.
    pre = await client.get(
        "https://jobs.apple.com/api/v1/CSRFToken",
        headers={**UA, "Accept": "application/json"}, timeout=25,
    )
    pre.raise_for_status()
    csrf = pre.headers.get("x-apple-csrf-token")
    if not csrf:
        raise RuntimeError("Apple careers API did not return a CSRF token")

    headers = {**UA, "Content-Type": "application/json", "Accept": "application/json",
               "Referer": "https://jobs.apple.com/en-us/search",
               "X-Apple-CSRF-Token": csrf}
    out = []
    seen = set()
    # Relevance is required when a query is present; Apple's `newest` sort
    # currently returns the unfiltered global stream. Five result pages gives
    # each focused config query ample coverage without crawling all Apple jobs.
    for page in range(1, 6):
        r = await client.post(
            "https://jobs.apple.com/api/v1/search",
            json={
                "query": query, "filters": {}, "page": page,
                "locale": "en-us", "sort": "relevance",
                "format": {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"},
            },
            headers=headers, timeout=30,
        )
        r.raise_for_status()
        data = r.json().get("res") or r.json()
        results = data.get("searchResults") or []
        if not results:
            break
        for j in results:
            pid = j.get("positionId") or j.get("id")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            slug = j.get("transformedPostingTitle") or ""
            locs = j.get("locations") or []
            out.append({
                "id": _uid("ap", pid),
                "company": company,
                "title": j.get("postingTitle", ""),
                "location": ", ".join(
                    l.get("name", "") for l in locs if l.get("name")
                ),
                "url": f"https://jobs.apple.com/en-us/details/{pid}/{slug}".rstrip("/"),
                "posted": _iso(j.get("postDateInGMT") or j.get("postingDate")),
                "description": _plain(j.get("jobSummary")),
                "_apple_position_id": pid,
            })

    # Search cards only contain a summary, not qualifications. For generic
    # Software Engineer/Developer titles, fetch Apple's own detail record so
    # explicit experience requirements can disqualify experienced roles.
    ambiguous = [job for job in out if _needs_software_detail(job["title"], "")]
    if ambiguous:
        detail_sem = asyncio.Semaphore(4)

        async def enrich(job):
            pid = job["_apple_position_id"]
            url = f"https://jobs.apple.com/api/v1/jobDetails/{pid}?locale=en-us"
            now = time.monotonic()
            cached = _APPLE_DETAIL_CACHE.get(url)
            if cached and now - cached[0] < (21600 if cached[1] else 300):
                return cached[1]
            async with detail_sem:
                try:
                    response = await client.get(url, headers=headers, timeout=30)
                    response.raise_for_status()
                    detail = response.json().get("res") or response.json()
                    description = _plain(
                        detail.get("jobSummary"), detail.get("description"),
                        detail.get("minimumQualifications"),
                        detail.get("preferredQualifications"),
                        detail.get("educationAndExperience"),
                        detail.get("additionalRequirements"),
                    )
                except Exception:
                    description = ""
                _APPLE_DETAIL_CACHE[url] = (now, description)
                return description

        descriptions = await asyncio.gather(*(enrich(job) for job in ambiguous))
        for job, description in zip(ambiguous, descriptions):
            if description:
                job["description"] = description
    for job in out:
        job.pop("_apple_position_id", None)
    return out


# --------------------------------------------------------------------- IBM
async def ibm(client, company="IBM", country="United States", **kw):
    """IBM's first-party careers search feed backed by its Avature catalog.

    IBM's public search UI calls this endpoint directly. The result cards do
    not expose the Avature detail page's ``Date posted`` value, so ``posted``
    intentionally remains unknown instead of using detection time as a fake
    company date.
    """
    endpoint = "https://www-api.ibm.com/search/api/v2"
    headers = {
        **UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Referer": "https://www.ibm.com/",
    }
    source_fields = [
        "_id", "title", "url", "description", "language", "entitled",
        "field_keyword_05",  # country
        "field_keyword_08",  # area of work
        "field_keyword_17",  # work arrangement
        "field_keyword_18",  # position type
        "field_keyword_19",  # display location
    ]
    out = []
    seen = set()
    for position_type, cohort in (("Internship", "internship"),
                                  ("Entry Level", "newgrad")):
        offset = 0
        while True:
            response = await client.post(
                endpoint,
                json={
                "appId": "careers",
                "scopes": ["careers2"],
                "query": {"bool": {"must": []}},
                "post_filter": {"bool": {"must": [
                    {"term": {"field_keyword_18": position_type}},
                    {"term": {"field_keyword_05": country}},
                ]}},
                # 100 is IBM's maximum accepted page size. The public catalog
                # can exceed it during recruiting season, so paginate with the
                # same Elasticsearch-style `from` field used by the search UI.
                "size": 100,
                "from": offset,
                "sort": [{"_score": "desc"}, {"pageviews": "desc"}],
                "lang": "zz",
                "localeSelector": {},
                "sm": {"query": "", "lang": "zz"},
                "_source": source_fields,
                },
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            hits = (data.get("hits") or {}).get("hits") or []
            total = (data.get("hits") or {}).get("total") or {}
            total_count = int(
                total.get("value", 0) if isinstance(total, dict) else total or 0
            )

            for hit in hits:
                job = hit.get("_source") or {}
                url = str(job.get("url") or "")
                match = re.search(r"[?&]jobId=([^&#]+)", url, re.I)
                job_id = match.group(1) if match else hit.get("_id")
                if not job_id or job_id in seen:
                    continue
                seen.add(job_id)
                location = str(job.get("field_keyword_19") or "").strip()
                # The server-side country filter is authoritative, but IBM labels
                # some US cards only as "Multiple Cities". Preserve that label and
                # add the country so the normal US location gate can verify it.
                if country and country.casefold() not in location.casefold():
                    location = ", ".join(filter(None, [location, country]))
                area = str(job.get("field_keyword_08") or "").strip()
                description = _plain(
                    f"Position type: {position_type}.",
                    f"Area of work: {area}." if area else "",
                    job.get("description"),
                )
                out.append({
                    "id": _uid("ibm", job_id),
                    "company": company,
                    "title": job.get("title", ""),
                    "location": location,
                    "url": url,
                    "posted": "",
                    "description": description,
                    "_cohort": cohort,
                })

            offset += len(hits)
            if not hits or offset >= total_count:
                break
            if offset > 5000:
                raise RuntimeError(f"IBM {position_type} pagination exceeded 5000 jobs")
    return out


# --------------------------------------------------------------- Vanguard
async def vanguard(
    client,
    company="Vanguard",
    org_id="companies/fbd5ce04-22d1-4aae-90dc-0282e45ee06f",
    **kw,
):
    """Vanguard's first-party careers feed (Findly/M-cloud over Workday).

    The public Vanguard job-search page calls this endpoint itself.  Its
    structured Technology + Students/Early career facets let us poll the
    relevant catalog directly without waiting for an aggregator to discover
    the same Workday requisitions.
    """
    endpoint = "https://jobsapi-google.m-cloud.io/api/job/search"
    headers = {
        **UA,
        "Accept": "application/json",
        "Referer": "https://www.vanguardjobs.com/job-search-results/",
    }
    out = []
    seen = set()
    for level, default_cohort in (("Students", "internship"),
                                  ("Early career", "newgrad")):
        offset = 0
        while True:
            response = await client.get(
                endpoint,
                params={
                    "pageSize": 100,
                    "offset": offset,
                    "companyName": org_id,
                    "customAttributeFilter": (
                        f'primary_category="Technology" AND level="{level}" '
                        'AND is_internal="External"'
                    ),
                    "orderBy": "posting_publish_time desc",
                },
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            results = data.get("searchResults") or []
            total = int(data.get("totalHits") or 0)

            for result in results:
                job = result.get("job") or {}
                job_id = job.get("id") or job.get("ref") or job.get("name")
                if not job_id or job_id in seen:
                    continue
                seen.add(job_id)
                country = str(job.get("primary_country") or "").strip()
                # This dashboard is US-only. Filtering here also avoids
                # downloading/processing unrelated overseas early-career jobs.
                if country.upper() not in {"US", "USA", "UNITED STATES"}:
                    continue
                title = str(job.get("title") or "").strip()
                cohort = (
                    "internship"
                    if re.search(r"\bintern(?:ship)?\b|\bco-?op\b", title, re.I)
                    else default_cohort
                )
                public_url = str(job.get("url") or job.get("seo_url") or "")
                if public_url.startswith("http://www.vanguardjobs.com/"):
                    public_url = "https://" + public_url[len("http://"):]
                out.append({
                    "id": _uid("vanguard", job_id),
                    "company": company,
                    "title": title,
                    "location": ", ".join(filter(None, [
                        job.get("primary_city"), job.get("primary_state"), country,
                    ])),
                    "url": public_url,
                    # Vanguard's open_date is a calendar date represented as
                    # midnight without a timezone. Keep it day-only; treating
                    # it as UTC would display the prior evening in Pacific time.
                    "posted": str(job.get("open_date") or "")[:10],
                    "description": _plain(job.get("description")),
                    "_cohort": cohort,
                })

            offset += len(results)
            if not results or offset >= total:
                break
            if offset > 5000:
                raise RuntimeError(f"Vanguard {level} pagination exceeded 5000 jobs")
    return out


# ------------------------------------------------------------- Oracle HCM CE
async def oracle_hcm(client, company, host, site, slug, title_facet,
                     location_facet, path_site=None, **kw):
    """Poll an Oracle Recruiting Candidate Experience site directly.

    ``title_facet`` and ``location_facet`` are the stable IDs exposed by the
    company's own search page. Using them avoids broad keyword matches while
    still following the same first-party catalog the public careers UI uses.
    """
    root = f"https://{host}/hcmRestApi/resources/latest"
    headers = {
        **UA,
        "Accept": "application/json",
        "Accept-Language": "en-US",
        "Ora-Irc-Language": "en",
    }
    finder = (
        "findReqs;siteNumber=" + site
        + ",facetsList=TITLES;LOCATIONS;POSTING_DATES"
        + ",limit=100,offset=0,sortBy=POSTING_DATES_DESC"
        + f",selectedTitlesFacet={title_facet}"
        + f",selectedLocationsFacet={location_facet}"
    )
    response = await client.get(
        f"{root}/recruitingCEJobRequisitions",
        params={
            "onlyData": "true",
            "expand": "requisitionList.secondaryLocations",
            "finder": finder,
        },
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    containers = response.json().get("items") or []
    container = containers[0] if containers else {}
    total = int(container.get("TotalJobsCount") or 0)
    if total > 100:
        raise RuntimeError(f"{company} Oracle HCM facet exceeds page limit: {total}")

    cards = []
    for card in container.get("requisitionList") or []:
        job_id = str(card.get("Id") or "")
        if not job_id:
            continue
        secondary = card.get("secondaryLocations") or []
        countries = {str(card.get("PrimaryLocationCountry") or "").upper()}
        countries.update(str(loc.get("Country") or "").upper()
                         for loc in secondary if isinstance(loc, dict))
        # Location facets should already enforce this, but validate the
        # structured country fields in case Oracle changes facet behavior.
        if "US" not in countries:
            continue
        cards.append((job_id, card))

    detail_sem = asyncio.Semaphore(5)

    async def fetch_detail(job_id):
        now = time.monotonic()
        cache_key = (host, site, job_id)
        cached = _ORACLE_DETAIL_CACHE.get(cache_key)
        if cached and now - cached[0] < (21600 if cached[1] else 300):
            return cached[1]
        async with detail_sem:
            try:
                detail_response = await client.get(
                    f"{root}/recruitingCEJobRequisitionDetails",
                    params={
                        "expand": "all",
                        "onlyData": "true",
                        "finder": f'ById;Id="{job_id}",siteNumber={site}',
                    },
                    headers=headers,
                    timeout=30,
                )
                detail_response.raise_for_status()
                items = detail_response.json().get("items") or []
                detail = items[0] if items else {}
            except Exception:
                detail = {}
        _ORACLE_DETAIL_CACHE[cache_key] = (now, detail)
        return detail

    details = await asyncio.gather(*(fetch_detail(job_id) for job_id, _ in cards))
    out = []
    for (job_id, card), detail in zip(cards, details):
        secondary = detail.get("secondaryLocations") or card.get("secondaryLocations") or []
        secondary_names = [
            str(loc.get("Name") or loc.get("LocationName") or "").strip()
            for loc in secondary if isinstance(loc, dict)
        ]
        location = str(detail.get("PrimaryLocation") or card.get("PrimaryLocation") or "")
        all_locations = list(dict.fromkeys(filter(None, [location, *secondary_names])))
        out.append({
            "id": _uid("oracle-hcm", host, site, job_id),
            "company": company,
            "title": detail.get("Title") or card.get("Title", ""),
            "location": ", ".join(all_locations),
            "url": f"https://{slug}/en/sites/{path_site or site}/job/{job_id}/",
            "posted": _iso(
                detail.get("ExternalPostedStartDate") or card.get("PostedDate")
            ),
            "description": _plain(
                detail.get("ExternalDescriptionStr"),
                detail.get("ExternalResponsibilitiesStr"),
                detail.get("ExternalQualificationsStr"),
                card.get("ShortDescriptionStr"),
            ),
        })
    return out


# ------------------------------------------------------------------ TikTok
async def tiktok(client, company="TikTok", query="software engineer", **kw):
    """lifeattiktok.com's own search API. Same caveat as Apple: undocumented."""
    r = await client.post(
        "https://lifeattiktok.com/api/v1/search/job/posts",
        json={"keyword": query, "limit": 50, "offset": 0,
              "job_category_id_list": [], "location_code_list": [],
              "subject_id_list": [], "recruitment_id_list": []},
        headers={**UA, "Content-Type": "application/json", "Accept": "application/json",
                 "website-path": "tiktok", "Referer": "https://lifeattiktok.com/search"},
        timeout=25,
    )
    r.raise_for_status()
    data = (r.json().get("data") or {})
    out = []
    for j in data.get("job_post_list", []):
        jid = j.get("id") or j.get("code")
        city = (j.get("city_info") or {}).get("en_name", "")
        out.append({
            "id": _uid("tt", jid),
            "company": company,
            "title": j.get("title", ""),
            "location": city,
            "url": f"https://lifeattiktok.com/search/{jid}",
            "posted": _iso(j.get("publish_time")),
        })
    return out


# ------------------------------------------------------------------ Rippling
async def rippling(client, company, token, **kw):
    url = f"https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs"
    r = await client.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    data = r.json()
    jobs = data if isinstance(data, list) else data.get("items", data.get("jobs", []))
    out = []
    for j in jobs:
        loc = j.get("workLocation") or {}
        out.append({
            "id": _uid("rp", token, j.get("uuid") or j.get("id")),
            "company": company,
            "title": j.get("name") or j.get("title", ""),
            "location": loc.get("label") if isinstance(loc, dict) else str(loc or ""),
            "url": j.get("url") or f"https://ats.rippling.com/{token}/jobs/{j.get('uuid')}",
            "posted": _iso(j.get("publishedAt") or j.get("createdAt")),
            "description": _plain(j.get("description")),
        })
    return out


# ----------------------------------------------------------------- Eightfold
async def eightfold(client, company, subdomain, domain, brand=None, query="", **kw):
    """Eightfold's public search API — same data the company's careers page renders.

    Some companies (e.g. PayPal) run several brands off one Eightfold tenant
    (PayPal, Venmo, Paidy all live under domain=paypal.com) — pass `brand` to
    scope to one, or omit it to pull everything under `domain`.
    """
    base = f"https://{subdomain}.eightfold.ai"
    out, start = [], 0
    while start < 500:
        params = {"domain": domain, "query": query, "location": "",
                   "start": start, "sort_by": "relevance"}
        if brand:
            params["filter_brand"] = brand
        r = await client.get(f"{base}/api/pcsx/search", params=params,
                              headers=UA, timeout=20)
        r.raise_for_status()
        data = r.json().get("data", {})
        posts = data.get("positions", [])
        if not posts:
            break
        for j in posts:
            out.append({
                "id": _uid("ef", subdomain, brand, j.get("id")),
                "company": company,
                "title": j.get("name", ""),
                "location": ", ".join(j.get("locations") or []),
                "url": base + j.get("positionUrl", ""),
                "posted": _iso(j.get("postedTs")),
                "description": _plain(j.get("description"), j.get("jobDescription")),
            })
        start += len(posts)
        if start >= data.get("count", 0):
            break

    # Eightfold's search response often omits descriptions. Fetch only plain
    # Software Engineer/Developer detail pages whose level cannot be inferred
    # from the title, keeping the fast path fast for every unambiguous role.
    ambiguous = [job for job in out if _needs_eightfold_detail(
        job["title"], job.get("description", "")
    )]
    if ambiguous:
        detail_sem = asyncio.Semaphore(4)

        async def enrich(job):
            async with detail_sem:
                return await _eightfold_detail_description(client, job["url"], domain)

        descriptions = await asyncio.gather(*(enrich(job) for job in ambiguous))
        for job, description in zip(ambiguous, descriptions):
            if description:
                job["description"] = description
    return out


# ------------------------------------------------------- SimplifyJobs feed
_SIMPLIFY_FEEDS = {
    "internships": "https://raw.githubusercontent.com/SimplifyJobs/"
                   "Summer2027-Internships/dev/.github/scripts/listings.json",
    "newgrad": "https://raw.githubusercontent.com/SimplifyJobs/"
               "New-Grad-Positions/dev/.github/scripts/listings.json",
}


async def simplify(client, company=None, feed="internships", only=None,
                   exclude=None, **kw):
    """Fallback net for companies with no reachable API of their own.

    Simplify's scrapers reach Microsoft, Google, Apple, Meta and TikTok, which
    we can't hit directly. Their feed refreshes roughly hourly, so these arrive
    slower than the 90s direct sources — still far better than not at all.
    Restrict with `only:` so this doesn't duplicate boards we already poll.
    """
    url = _SIMPLIFY_FEEDS.get(feed, _SIMPLIFY_FEEDS["internships"])
    r = await client.get(url, headers=UA, timeout=45)
    r.raise_for_status()
    wanted = [c.lower() for c in (only or [])]
    skip = {c.lower() for c in (exclude or [])}
    out = []
    for j in r.json():
        if not (j.get("active") and j.get("is_visible", True)):
            continue
        co = j.get("company_name") or ""
        low = co.lower()
        if wanted and not any(w in low for w in wanted):
            continue
        # Never duplicate a company we already poll directly — the direct feed
        # is faster and authoritative.
        if any(sk == low or sk in low or low in sk for sk in skip):
            continue
        ts = j.get("date_posted") or j.get("date_updated")
        out.append({
            "id": _uid("sf", j.get("id")),
            "company": co,
            "title": j.get("title", ""),
            "location": ", ".join(j.get("locations") or []),
            "url": j.get("url", ""),
            "posted": _iso(ts),
        })
    return out


async def l3harris(client, company="L3Harris Technologies", query="software engineer",
                   **kw):
    url = ("https://jobs.l3harris.com/tile-search-results"
           f"?q={query}&sortColumn=referencedate&sortDirection=desc"
           "&startrow=0&maxRecords=100")
    r = await client.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    out = []
    for m in re.finditer(
        r'<li[^>]*job-tile[^>]*data-url="(/job/[^"]+/(\d+)/)"[^>]*>(.*?)</li>',
        r.text, re.DOTALL,
    ):
        href, jid, tile = m.groups()
        title_m = re.search(r'jobTitle-link[^>]*>\s*(.*?)\s*</a>', tile, re.DOTALL)
        loc_m = re.search(r'section-field\s+location.*?section-\w+-value">\s*(.*?)\s*</div>',
                          tile, re.DOTALL)
        out.append({
            "id": _uid("l3h", jid),
            "company": company,
            "title": (title_m.group(1).strip() if title_m else ""),
            "location": (loc_m.group(1).strip() if loc_m else ""),
            "url": f"https://jobs.l3harris.com{href}",
            "posted": "",
        })

    # Search tiles omit the date even though each official detail page exposes
    # schema.org datePosted metadata. Fetch only titles that could pass the
    # early-career filter, and cache the immutable posting date.
    candidates = [job for job in out if re.search(
        r"\b(?:associate|intern(?:ship)?|new\s*grad|entry.level|junior)\b|"
        r"\bsoftware\s+engineer(?:ing)?\s*(?:i|1)\b",
        job["title"], re.I,
    )]

    async def posted_date(job):
        url = job["url"]
        cached = _L3_DATE_CACHE.get(url)
        if cached:
            return cached
        try:
            detail = await client.get(url, headers=UA, timeout=20)
            detail.raise_for_status()
            match = re.search(
                r'itemprop=["\']datePosted["\'][^>]*content=["\']([^"\']+)',
                detail.text, re.I,
            )
            if not match:
                match = re.search(
                    r'content=["\']([^"\']+)["\'][^>]*itemprop=["\']datePosted["\']',
                    detail.text, re.I,
                )
            value = match.group(1).strip() if match else ""
            parsed = datetime.strptime(value, "%a %b %d %H:%M:%S %Z %Y")
            date = parsed.strftime("%Y-%m-%d")
        except Exception:
            date = ""
        if date:
            _L3_DATE_CACHE[url] = date
        return date

    if candidates:
        dates = await asyncio.gather(*(posted_date(job) for job in candidates))
        for job, date in zip(candidates, dates):
            if date:
                job["posted"] = date
    return out


async def garmin(client, company="Garmin", **kw):
    url = "https://careers.garmin.com/api/jobs?page=1&limit=100"
    out = []
    page = 1
    while True:
        r = await client.get(f"https://careers.garmin.com/api/jobs?page={page}&limit=100",
                             headers=UA, timeout=20)
        r.raise_for_status()
        data = r.json()
        for j in data.get("jobs", []):
            d = j.get("data", j)
            loc = ", ".join(filter(None, [d.get("city"), d.get("state")]))
            out.append({
                "id": _uid("garmin", d.get("req_id") or d.get("slug")),
                "company": company,
                "title": d.get("title", ""),
                "location": loc,
                "url": f"https://careers.garmin.com/careers-home/jobs/{d.get('slug')}",
                "posted": _iso(d.get("posted_date") or d.get("create_date")),
            })
        if len(data.get("jobs", [])) < 100:
            break
        page += 1
    return out


async def resolve_real_date(client, url):
    """Given a Simplify job URL, fetch the real posting date from the ATS API."""
    # Greenhouse: boards.greenhouse.io/{board}/jobs/{id}
    m = re.match(r'https?://(?:job-)?boards\.greenhouse\.io/([\w-]+)/jobs/(\d+)', url)
    if m:
        board, jid = m.groups()
        try:
            r = await client.get(
                f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}",
                headers=UA, timeout=10)
            if r.status_code == 200:
                d = r.json()
                return _iso(d.get("first_published") or d.get("updated_at"))
        except Exception:
            pass
        return None
    # Lever: jobs.lever.co/{company}/{uuid}[/apply]
    m = re.match(r'https?://jobs\.lever\.co/([\w-]+)/([\da-f][\da-f-]+)', url)
    if m:
        co, jid = m.groups()
        try:
            r = await client.get(
                f"https://api.lever.co/v0/postings/{co}/{jid}",
                headers=UA, timeout=10)
            if r.status_code == 200:
                return _iso(r.json().get("createdAt"))
        except Exception:
            pass
        return None
    return None


async def resolve_batch_dates(client, jobs):
    """Batch-resolve dates for Simplify jobs from Ashby/Workable boards.

    Groups jobs by board, fetches each board once, and matches dates by
    job UUID.  Returns the number of jobs whose date was corrected.
    """
    from collections import defaultdict
    ashby_boards = defaultdict(list)   # board -> [(job, uuid)]
    workable_boards = defaultdict(list)

    for j in jobs:
        url = j.get("url", "")
        m = re.match(r'https?://jobs\.ashbyhq\.com/([\w-]+)/([\da-f][\da-f-]+)', url)
        if m:
            ashby_boards[m.group(1)].append((j, m.group(2)))
            continue
        m = re.match(r'https?://apply\.workable\.com/([\w-]+)/j/([\w-]+)', url)
        if m:
            workable_boards[m.group(1)].append((j, m.group(2)))

    resolved = 0

    # --- Ashby: GET posting-api/job-board/{board} → match by jobUrl UUID ---
    for board, entries in ashby_boards.items():
        try:
            r = await client.get(
                f"https://api.ashbyhq.com/posting-api/job-board/{board}",
                headers=UA, timeout=15)
            if r.status_code != 200:
                continue
            by_id = {}
            for aj in r.json().get("jobs", []):
                jurl = aj.get("jobUrl", "")
                m2 = re.search(r'/([\da-f-]{36})', jurl)
                if m2:
                    by_id[m2.group(1)] = _iso(aj.get("publishedAt"))
            for j, uuid in entries:
                real = by_id.get(uuid)
                if real:
                    j["posted"] = real
                    j["_date_resolved"] = True
                    resolved += 1
        except Exception:
            pass

    # --- Workable: GET widget/accounts/{token} → match by shortcode ---
    for token, entries in workable_boards.items():
        try:
            r = await client.get(
                f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true",
                headers=UA, timeout=15)
            if r.status_code != 200:
                continue
            by_code = {}
            for wj in r.json().get("jobs", []):
                sc = wj.get("shortcode") or ""
                if sc:
                    by_code[sc] = _iso(wj.get("published_on") or wj.get("created_at"))
            for j, code in entries:
                real = by_code.get(code)
                if real:
                    j["posted"] = real
                    j["_date_resolved"] = True
                    resolved += 1
        except Exception:
            pass

    return resolved


REGISTRY = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
    "linkedin_company": linkedin_company,
    "workday": workday,
    "microsoft": microsoft,
    "amazon": amazon,
    "workable": workable,
    "recruitee": recruitee,
    "apple": apple,
    "ibm": ibm,
    "vanguard": vanguard,
    "oracle_hcm": oracle_hcm,
    "tiktok": tiktok,
    "rippling": rippling,
    "eightfold": eightfold,
    "simplify": simplify,
    "garmin": garmin,
    "l3harris": l3harris,
}

# Playwright-based scrapers for JS-rendered / Cloudflare-blocked career sites
try:
    from pw_scrapers import SCRAPERS as _PW
    REGISTRY.update(_PW)
except ImportError:
    pass  # playwright not installed — skip these sources
