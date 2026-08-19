#!/usr/bin/env python3
"""
Find the right ATS platform + slug for a company, instead of guessing.

    python discover.py plaid doordash snowflake citadel
    python discover.py --add companies.txt      # bulk: probe and append winners
    python discover.py --workday https://boeing.wd1.myworkdayjobs.com/EXTERNAL_CAREERS
    python discover.py --workday-find "Capital One" CVS Boeing Progressive

For each name it tries every platform with a few slug variants and reports
which combination actually returns postings. Copy the winning line straight
into config.yaml.
"""
import asyncio
import re
import sys
from pathlib import Path

import httpx

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
      "Accept": "application/json"}

PROBES = [
    ("greenhouse", "https://boards-api.greenhouse.io/v1/boards/{s}/jobs", "jobs"),
    ("lever", "https://api.lever.co/v0/postings/{s}?mode=json", None),
    ("ashby", "https://api.ashbyhq.com/posting-api/job-board/{s}", "jobs"),
    ("smartrecruiters", "https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=1", "content"),
    ("workable", "https://apply.workable.com/api/v1/widget/accounts/{s}?details=true", "jobs"),
    ("recruitee", "https://{s}.recruitee.com/api/offers/", "offers"),
    ("rippling", "https://api.rippling.com/platform/api/ats/v1/board/{s}/jobs", None),
]


def variants(name: str):
    base = re.sub(r"[^a-z0-9]", "", name.lower())
    spaced = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    first = name.split()[0].lower()
    out = [base, spaced, name,
           base + "inc", base + "careers", base + "jobs", base + "com",
           base + "ai", base + "hq", base + "group", base + "3d", base + "labs",
           base + "technologies", base + "software", base + "-inc",
           base.replace("the", "", 1) or base, first, first + "careers",
           name.lower().replace(" ", "."), base + ".ai", base + ".com",
           "-".join(w[0] for w in name.split()) if len(name.split()) > 1 else base,
           "".join(w[0] for w in name.split()) if len(name.split()) > 1 else base]
    seen, uniq = set(), []
    for v in out:
        if v and v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


async def try_one(client, platform, url_tpl, key, slug, sem):
    url = url_tpl.format(s=slug)
    async with sem:
        try:
            r = await client.get(url, headers=UA, timeout=12)
        except Exception:
            return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    jobs = data if key is None else data.get(key, [])
    if not isinstance(jobs, list) or not jobs:
        return None
    sample = jobs[0]
    title = sample.get("title") or sample.get("text") or sample.get("name") or ""
    return platform, slug, len(jobs), title


async def discover(names):
    sem = asyncio.Semaphore(15)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for name in names:
            tasks = [
                try_one(client, p, u, k, s, sem)
                for p, u, k in PROBES
                for s in variants(name)
            ]
            results = [r for r in await asyncio.gather(*tasks) if r]
            print(f"\n=== {name} ===")
            if not results:
                print("  no ATS feed under that name — trying Workday...")
                await workday_find([name], quiet=True)
                continue
            results.sort(key=lambda r: -r[2])
            for platform, slug, n, title in results[:3]:
                print(f"  {n:>5} postings · e.g. {title[:50]}")
                print(f"  → - {{type: {platform}, company: {name.title()}, token: {slug}}}")


async def bulk_add(path_or_names):
    """Probe many companies and append every real feed found into config.yaml."""
    if len(path_or_names) == 1 and Path(path_or_names[0]).exists():
        raw = Path(path_or_names[0]).read_text().splitlines()
        names = [l.strip() for l in raw if l.strip() and not l.startswith("#")]
    else:
        names = path_or_names

    cfg_path = Path(__file__).parent / "config.yaml"
    cfg_text = cfg_path.read_text()

    sem = asyncio.Semaphore(20)
    found, missing = [], []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for i in range(0, len(names), 15):           # batches, to stay polite
            batch = names[i:i + 15]
            jobs = {
                n: [try_one(client, p, u, k, s_, sem)
                    for p, u, k in PROBES for s_ in variants(n)]
                for n in batch
            }
            for name, tasks in jobs.items():
                hits = [r for r in await asyncio.gather(*tasks) if r]
                if not hits:
                    missing.append(name)
                    continue
                hits.sort(key=lambda r: -r[2])
                platform, slug, n, _ = hits[0]
                line = (f"  - {{type: {platform}, company: {name}, token: {slug}}}")
                if f"token: {slug}}}" in cfg_text:
                    print(f"  = {name:<28} already in config")
                    continue
                found.append(line)
                print(f"  + {name:<28} {platform}/{slug}  ({n} postings)")
            await asyncio.sleep(1)

    if found:
        block = "\n  # ---- added by discover.py --add\n" + "\n".join(found) + "\n"
        cfg_path.write_text(cfg_text.rstrip("\n") + "\n" + block)
        print(f"\nappended {len(found)} sources to config.yaml")
    else:
        print("\nnothing new to add")

    if missing:
        print(f"\n{len(missing)} had no public Greenhouse/Lever/Ashby/SmartRecruiters feed.")
        print("Most of these are Workday or a bespoke site. For Workday, open their")
        print("careers page and run:  python discover.py --workday <url>")
        print("  " + ", ".join(missing))
    print("\nNow run:  python check.py")


WD_HOSTS = ["wd1", "wd2", "wd3", "wd5", "wd10", "wd12", "wd101", "wd103"]
WD_SITES = ["careers", "Careers", "External", "external", "ExternalCareers",
            "External_Career_Site", "ExternalCareerSite", "jobs", "Jobs",
            "{t}careers", "{T}_Careers", "{t}_careers", "{T}Careers",
            "{T}_External_Career_Site", "CareerSite", "Global_Careers"]


async def _wd_probe(client, tenant, host, site, sem):
    url = (f"https://{tenant}.{host}.myworkdayjobs.com"
           f"/wday/cxs/{tenant}/{site}/jobs")
    async with sem:
        try:
            r = await client.post(url, json={"appliedFacets": {}, "limit": 1,
                                             "offset": 0, "searchText": ""},
                                  headers={**UA, "Content-Type": "application/json"},
                                  timeout=10)
        except Exception:
            return None
    if r.status_code != 200:
        return None
    try:
        total = r.json().get("total", 0)
    except Exception:
        return None
    return (tenant, host, site, total) if total else None


async def workday_find(names, quiet=False):
    """Brute-force a company's Workday tenant/site/host so you don't hunt URLs."""
    sem = asyncio.Semaphore(25)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for name in names:
            tenant = re.sub(r"[^a-z0-9]", "", name.lower())
            sites = [s.replace("{t}", tenant).replace("{T}", tenant.title())
                     for s in WD_SITES]
            tasks = [_wd_probe(client, tenant, h, s, sem)
                     for h in WD_HOSTS for s in sites]
            hits = [r for r in await asyncio.gather(*tasks) if r]
            if not quiet:
                print(f"\n=== {name} ===")
            if not hits:
                print("  nothing found on any supported platform.")
                print("  Open their careers page and look at the URL:")
                print("    contains myworkdayjobs.com -> python discover.py --workday <url>")
                print("    anything else              -> bespoke site, check it manually")
                continue
            hits.sort(key=lambda r: -r[3])
            for t, h, st, n in hits[:2]:
                print(f"  {n} postings")
                print(f"  - {{type: workday, company: {name}, tenant: {t}, "
                      f"site: {st}, host: {h}, search: intern}}")
            await asyncio.sleep(0.5)


def from_workday_url(url: str):
    """Turn a Workday careers URL you copied from the browser into a config line.

    https://cvshealth.wd1.myworkdayjobs.com/CVS_Health_Careers
        -> - {type: workday, company: Cvshealth, tenant: cvshealth,
              site: CVS_Health_Careers, host: wd1, search: intern}
    """
    m = re.match(r"https?://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(.+)", url.strip())
    if not m:
        print(f"  not a Workday URL: {url}")
        print("  expected something like https://<tenant>.wd1.myworkdayjobs.com/<Site>")
        return
    tenant, host, rest = m.groups()
    parts = [p for p in rest.split("/") if p and not re.fullmatch(r"[a-z]{2}[-_][A-Za-z]{2}", p)]
    if not parts:
        print(f"  found tenant '{tenant}' but no site path in {url}")
        return
    site = parts[0]
    print(f"  - {{type: workday, company: {tenant.title()}, tenant: {tenant}, "
          f"site: {site}, host: {host}, search: intern}}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    if args[0] == "--add":
        asyncio.run(bulk_add(args[1:]))
        sys.exit(0)
    if args[0] == "--workday-find":
        asyncio.run(workday_find(args[1:]))
        sys.exit(0)
    if args[0] == "--workday":
        for u in args[1:]:
            from_workday_url(u)
        sys.exit(0)
    asyncio.run(discover(args))
