#!/usr/bin/env python3
"""
Validate every source in config.yaml. Run this FIRST, and after any config edit.

A wrong ATS token returns 404 and silently contributes zero jobs forever —
that is exactly the failure mode that makes a tracker quietly useless.
This makes it loud.

    python check.py
"""
import asyncio
import sys
from pathlib import Path

import httpx
import yaml

import adapters
from main import compile_filters, matches

ROOT = Path(__file__).parent


async def probe(client, src, sem, F):
    name = src.get("company", src["type"])
    fn = adapters.REGISTRY.get(src["type"])
    if not fn:
        return name, "BAD TYPE", 0, 0
    args = {k: v for k, v in src.items() if k != "type"}
    async with sem:
        try:
            jobs = await fn(client, **args)
        except httpx.HTTPStatusError as e:
            return name, f"HTTP {e.response.status_code}", 0, 0
        except Exception as e:
            return name, type(e).__name__, 0, 0
    hits = sum(1 for j in jobs if matches(j, F))
    return name, "ok", len(jobs), hits


async def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    F = compile_filters(cfg)
    direct = {str(s["company"]).lower() for s in cfg["sources"]
              if s.get("company") and s["type"] != "simplify"}
    srcs = []
    for s in cfg["sources"]:
        s = dict(s)
        if s.pop("skip_direct", False):
            s["exclude"] = sorted(direct | {c.lower() for c in
                                            cfg.get("never_alert_companies", [])})
        srcs.append(s)
    sem = asyncio.Semaphore(10)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        rows = await asyncio.gather(
            *(probe(client, s, sem, F) for s in srcs)
        )

    bad = [r for r in rows if r[1] != "ok"]
    empty = [r for r in rows if r[1] == "ok" and r[2] == 0]

    print(f"{'SOURCE':<26}{'STATUS':<12}{'TOTAL':>7}{'MATCH':>7}")
    print("-" * 52)
    for name, status, total, hits in rows:
        flag = "  " if status == "ok" and total else "!!"
        print(f"{flag}{name:<24}{status:<12}{total:>7}{hits:>7}")

    print(f"\n{len(rows) - len(bad)}/{len(rows)} sources reachable")
    if bad:
        print(f"\nBROKEN — fix the token/tenant in config.yaml:")
        for n, s, *_ in bad:
            print(f"  · {n} ({s})")
    if empty:
        print(f"\nReachable but returned 0 postings (token may be wrong "
              f"or board genuinely empty):")
        for n, *_ in empty:
            print(f"  · {n}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    asyncio.run(main())
