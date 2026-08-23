"""Promote aggregator job URLs into official company ATS sources."""
from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse


def _segments(parsed) -> list[str]:
    return [unquote(part) for part in parsed.path.split("/") if part]


def infer_direct_source(company: str, url: str) -> dict | None:
    """Return a pollable official source inferred from one application URL."""
    company = str(company or "").strip()
    if not company or not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    parts = _segments(parsed)
    source = None

    if host.endswith("greenhouse.io"):
        token = None
        if parts and parts[0] not in {"embed", "jobs"}:
            token = parts[0]
        query = parse_qs(parsed.query)
        token = token or (query.get("for") or [None])[0]
        if token:
            source = {"type": "greenhouse", "company": company, "token": token}

    elif host == "jobs.lever.co" and parts:
        source = {"type": "lever", "company": company, "token": parts[0]}

    elif host == "jobs.ashbyhq.com" and parts:
        source = {"type": "ashby", "company": company, "token": parts[0]}

    elif host in {"apply.workable.com", "apply.workable.com"} and parts:
        source = {"type": "workable", "company": company, "token": parts[0]}

    elif host.endswith((".myworkdayjobs.com", ".myworkdaysite.com")) and "job" in parts:
        labels = host.split(".")
        wd_index = next((i for i, label in enumerate(labels) if label.startswith("wd")), None)
        job_index = parts.index("job")
        if wd_index is not None and wd_index > 0 and job_index > 0:
            public_tenant = labels[wd_index - 1]
            tenant = public_tenant
            if tenant.startswith("osv-"):
                # Workday's public alias uses a hyphen while the page's
                # embedded CXS tenant uses an underscore (osv-acme → osv_acme).
                tenant = tenant.replace("-", "_")
            source = {
                "type": "workday",
                "company": company,
                "tenant": tenant,
                "host": labels[wd_index],
                "site": parts[job_index - 1],
            }
            if host.endswith(".myworkdaysite.com"):
                source["domain"] = "myworkdaysite.com"
            if public_tenant != tenant:
                source["subdomain"] = public_tenant

    elif host.endswith(".oraclecloud.com") and "sites" in parts:
        site_index = parts.index("sites")
        if len(parts) > site_index + 1:
            site = parts[site_index + 1]
            source = {
                "type": "oracle_hcm", "company": company, "host": host,
                "site": site, "path_site": site, "slug": host,
            }

    elif host.endswith(".icims.com") and "jobs" in parts:
        token = host.split(".")[0]
        token = re_sub_prefix(token, ("careers-", "jobs-"))
        source = {
            "type": "icims", "company": company, "token": token,
            "base_url": f"{parsed.scheme or 'https'}://{host}",
        }

    elif host.endswith(".bamboohr.com") and (
            (parts and parts[0] in {"careers", "jobs"}) or "id" in parse_qs(parsed.query)):
        source = {
            "type": "bamboohr", "company": company,
            "token": host.removesuffix(".bamboohr.com"),
        }

    elif host == "jobs.jobvite.com" and parts:
        source = {"type": "jobvite", "company": company, "token": parts[0]}

    elif host.endswith(".pinpointhq.com") and "postings" in parts:
        source = {
            "type": "pinpoint", "company": company,
            "token": host.removesuffix(".pinpointhq.com"),
        }

    elif host.endswith(".applytojob.com") and parts:
        source = {
            "type": "jazzhr", "company": company,
            "token": host.removesuffix(".applytojob.com"),
        }

    elif ("successfactors" in host or host.endswith(".jobs2web.com")):
        query = parse_qs(parsed.query)
        token = (query.get("company") or query.get("companyId") or [None])[0]
        source = {
            "type": "successfactors", "company": company,
            "base_url": f"{parsed.scheme or 'https'}://{host}",
        }
        if token:
            source["token"] = token

    elif host == "jobs.smartrecruiters.com" and parts:
        source = {"type": "smartrecruiters", "company": company, "token": parts[0]}

    elif host == "ats.rippling.com" and parts:
        source = {"type": "rippling", "company": company, "token": parts[0]}

    elif host.endswith(".eightfold.ai"):
        query = parse_qs(parsed.query)
        subdomain = host.removesuffix(".eightfold.ai")
        domain = (query.get("domain") or [f"{subdomain}.com"])[0]
        if domain and subdomain:
            source = {
                "type": "eightfold", "company": company,
                "subdomain": subdomain, "domain": domain,
                "query": "software engineer",
            }

    if source:
        source["generated"] = True
        source["promoted_from"] = "simplify"
    return source


def discover_direct_sources(jobs) -> list[dict]:
    """Infer and deduplicate direct boards from normalized fallback jobs."""
    import storage  # local import avoids a module cycle during startup

    found = {}
    for job in jobs:
        source = infer_direct_source(job.get("company", ""), job.get("url", ""))
        if not source:
            continue
        found.setdefault(storage.source_key(source), source)
    return list(found.values())


def promotion_identity(source: dict):
    """Logical board identity used to replace corrected generated configs."""
    company = str(source.get("company") or "").strip().casefold()
    if source.get("type") == "workday":
        return ("workday", company, source.get("host"), source.get("site"))
    if source.get("type") == "eightfold":
        return ("eightfold", company, source.get("subdomain"))
    if source.get("type") == "oracle_hcm":
        return ("oracle_hcm", company, source.get("host"), source.get("site"))
    return (source.get("type"), company, source.get("token"), source.get("brand"),
            source.get("base_url"))


def re_sub_prefix(value: str, prefixes: tuple[str, ...]) -> str:
    """Remove a known hosted-ATS hostname prefix without touching the token."""
    for prefix in prefixes:
        if value.startswith(prefix):
            return value[len(prefix):]
    return value
