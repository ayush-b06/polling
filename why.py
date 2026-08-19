#!/usr/bin/env python3
"""
Show exactly where postings are being dropped. Run this when the dashboard
looks too empty (or too noisy) and you want facts instead of guesses.

    python why.py                     # full funnel across every source
    python why.py --company Palantir  # funnel for one company
    python why.py --title "Software Engineer Intern" --loc "Chicago, IL"
                                      # test one posting against every gate
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter, defaultdict
from pathlib import Path

import httpx
import yaml

import adapters
from main import compile_filters, fetch_source, _age_days, classify_target

ROOT = Path(__file__).parent

GATES = [
    ("kill",    "title_exclude_any",        "non-engineering title"),
    ("url_kill", "url_exclude_any",         "experienced-role URL"),
    ("role",    "role_must_match_any",      "not a software title"),
    ("level",   "level_must_match_any",     "not intern / new grad"),
    ("not_us",  "non_us_any",               "foreign location"),
    ("loc_pfx", "non_us_location_prefix",   "foreign country code"),
    ("us",      "us_signal_any",            "no US evidence"),
]


def verdict(job, F):
    """Return (kept, gate_key, matched_text). First failing gate wins."""
    title, text = job["title"], f"{job['title']} {job['location']}"
    if F.get("kill"):
        match = F["kill"].search(title)
        if match:
            return False, "kill", match.group(0)
    if F.get("url_kill"):
        match = F["url_kill"].search(str(job.get("url") or ""))
        if match:
            return False, "url_kill", match.group(0)
    if F.get("role") and not F["role"].search(title):
        return False, "role", ""
    if not classify_target(job, F):
        return False, "level", ""
    if F.get("not_us"):
        match = F["not_us"].search(text)
        if match:
            return False, "not_us", match.group(0)
    if F.get("loc_pfx"):
        match = F["loc_pfx"].search(job["location"])
        if match:
            return False, "loc_pfx", match.group(0)
    if F.get("us") and not F["us"].search(text):
        return False, "us", ""
    exempt = F.get("age_exempt") and F["age_exempt"].search(title)
    age = _age_days(job.get("posted"))
    if not exempt and F.get("max_age") and age is not None and age > F["max_age"]:
        return False, "max_age", f"{age}d old"
    return True, None, ""


async def run(company=None):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    F = compile_filters(cfg)
    srcs = cfg["sources"]
    if company:
        srcs = [s for s in srcs if company.lower() in str(s.get("company", "")).lower()]
        if not srcs:
            print(f"no source named '{company}' in config.yaml")
            return

    sem = asyncio.Semaphore(cfg.get("concurrency", 12))
    async with httpx.AsyncClient(follow_redirects=True) as client:
        batches = await asyncio.gather(*(fetch_source(client, s, sem) for s in srcs))
    jobs = []
    for src, batch in zip(srcs, batches):
        cohort = src.get("cohort")
        if not cohort and src["type"] == "simplify":
            cohort = "internship" if src.get("feed") == "internships" else "newgrad"
        for job in batch:
            if cohort:
                job["_cohort"] = cohort
            jobs.append(job)

    drops = Counter()
    samples = defaultdict(list)
    kept = []
    for j in jobs:
        ok, gate, why = verdict(j, F)
        if ok:
            kept.append(j)
        else:
            drops[gate] += 1
            if len(samples[gate]) < 4:
                samples[gate].append((j["company"], j["title"], j["location"], why))

    print(f"\n{len(jobs)} postings fetched from {len(srcs)} sources")
    print(f"{len(kept)} survive every gate\n")
    print(f"{'DROPPED BY':<26}{'COUNT':>7}   what it means")
    print("-" * 74)
    labels = {k: (lbl, desc) for k, lbl, desc in GATES}
    labels["max_age"] = ("max_age_days", f"older than {F.get('max_age')} days")
    for gate, n in drops.most_common():
        lbl, desc = labels.get(gate, (gate, ""))
        print(f"{lbl:<26}{n:>7}   {desc}")

    for gate, _ in drops.most_common():
        lbl = labels.get(gate, (gate, ""))[0]
        print(f"\n  examples dropped by {lbl}:")
        for co, t, loc, why in samples[gate]:
            tag = f"  <- matched {why!r}" if why else ""
            print(f"    {co}: {t[:58]}  [{loc[:26]}]{tag}")

    by_co = Counter(j["company"] for j in kept)
    print(f"\n  surviving roles by company ({len(by_co)} companies):")
    for co, n in by_co.most_common():
        print(f"    {n:>3}  {co}")


def one(title, loc, posted=""):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    F = compile_filters(cfg)
    job = {"title": title, "location": loc, "posted": posted}
    print(f"\n  {title}\n  [{loc}]\n")
    for key, lbl, desc in GATES:
        rx = F.get(key)
        if not rx:
            continue
        if key in ("kill", "not_us", "loc_pfx"):
            hay = title if key == "kill" else (loc if key == "loc_pfx" else f"{title} {loc}")
            m = rx.search(hay)
            print(f"  {'FAIL' if m else 'pass'}  {lbl:<26}"
                  + (f"matched {m.group(0)!r}" if m else ""))
        else:
            hay = title if key in ("role", "level") else f"{title} {loc}"
            m = rx.search(hay)
            print(f"  {'pass' if m else 'FAIL'}  {lbl:<26}"
                  + (f"matched {m.group(0)!r}" if m else "nothing matched"))
    ok, gate, why = verdict(job, F)
    print(f"\n  => {'KEPT' if ok else 'DROPPED at ' + str(gate)}\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--company")
    ap.add_argument("--title")
    ap.add_argument("--loc", default="")
    ap.add_argument("--posted", default="")
    a = ap.parse_args()
    if a.title:
        one(a.title, a.loc, a.posted)
    else:
        asyncio.run(run(a.company))
