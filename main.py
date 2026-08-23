#!/usr/bin/env python3
"""
jobwatch — poll company ATS feeds directly, alert on genuinely new roles.

  python main.py --once     # one pass (for cron / GitHub Actions)
  python main.py            # independent per-source scheduler
  python main.py --seed     # record everything currently open WITHOUT alerting
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

try:  # silence the expected warning for the one insecure source
    import warnings
    warnings.filterwarnings("ignore", message=".*[Vv]erif.*")
except Exception:
    pass

import adapters
import dashboard
import pw_scrapers
import source_discovery
import storage


ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
DIRECT_SOURCE_FILE = ROOT / "direct_sources.json"
COVERAGE_FILE = ROOT / "coverage_report.json"
SEEN_FILE = ROOT / "seen.json"        # legacy, migrated on first load
DB_FILE = ROOT / "jobwatch.db"
LOCAL_ENV_FILE = ROOT / ".env.local"


def load_local_env(path: Path = LOCAL_ENV_FILE) -> None:
    """Load notification credentials from a git-ignored local file."""
    if not path.exists():
        return
    allowed = {"DISCORD_WEBHOOK", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in allowed or os.environ.get(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if value:
            os.environ[key] = value


# --------------------------------------------------------------- persistence
def load_seen() -> dict:
    """Compatibility helper used by local scripts and older integrations."""
    store = storage.JobStore(DB_FILE)
    store.import_legacy(STATE_FILE)
    return store.dashboard_state()


def save_seen(state: dict) -> None:
    """Deprecated: SQLite is now the source of truth."""
    raise RuntimeError("save_seen is deprecated; use storage.JobStore")


# ------------------------------------------------------------------ matching
def compile_filters(cfg: dict):
    """Compile the strict role, level, location, and age gates."""
    def rx(key):
        pats = cfg["filters"].get(key) or cfg.get(key) or []
        return re.compile("|".join(f"(?:{p})" for p in pats), re.I) if pats else None
    return {
        "max_age": cfg.get("max_age_days"),
        "age_exempt": rx("max_age_exempt_if_title_matches"),
        "role":     rx("role_must_match_any"),      # is it a software job?
        "level":    rx("level_must_match_any"),     # is it intern / new grad?
        "intern":   rx("internship_signal_any"),
        "newgrad":  rx("new_grad_signal_any"),
        "entry_desc": rx("entry_description_any"),
        "experienced": rx("experienced_description_any"),
        "non_fulltime": rx("non_full_time_any"),
        "quant":    rx("quant_role_any"),
        "kill":     rx("title_exclude_any"),        # non-engineering functions
        "url_kill": rx("url_exclude_any"),          # level evidence hidden in ATS slug
        "us":       rx("us_signal_any"),            # positive US evidence
        "not_us":   rx("non_us_any"),               # explicit foreign evidence
        "loc_pfx":  rx("non_us_location_prefix"),   # "IL, Haifa" = Israel
        "boost":    rx("boost_any"),
        "max_age":  cfg.get("max_age_days"),
    }


_ENTRY_EXPERIENCE_RANGE = re.compile(
    r"\b([01])\s*(?:-|\u2013|\u2014|to)\s*(\d{1,2})\s+years?\b.{0,80}?\bexperience\b",
    re.I | re.DOTALL,
)

_GENERIC_SOFTWARE_TITLE = re.compile(
    r"\bsoftware\s+(?:engineer(?:ing)?|engr|developer|development)\b",
    re.I,
)


def _entry_experience_match(description: str):
    """Return a 0/1-to-N experience range that explicitly includes new grads."""
    return _ENTRY_EXPERIENCE_RANGE.search(description or "")


def _generic_software_title(title: str) -> bool:
    """True for SWE titles that should be included unless evidence excludes them.

    Seniority and adjacent-role exclusions remain in the normal title kill gate;
    this signal only changes the early-career gate from allow-list to deny-list.
    """
    return bool(_GENERIC_SOFTWARE_TITLE.search(title or ""))


def classify_target(job: dict, F) -> bool:
    """Assign one of the four allowed categories, or reject the posting.

    Source cohort is accepted only when the source is explicitly configured as
    an internship/new-grad feed. Generic company-search results never bypass
    level evidence.
    """
    title = str(job.get("title") or "")
    description = str(job.get("description") or "")
    detail = f"{title} {description}"
    cohort = str(job.get("_cohort") or "").lower()

    intern_title = bool(F.get("intern") and F["intern"].search(title))
    newgrad_title = bool(F.get("newgrad") and F["newgrad"].search(title))
    entry_range = _entry_experience_match(description)
    entry_description = bool(
        (F.get("entry_desc") and F["entry_desc"].search(description)) or entry_range
    )

    generic_software = _generic_software_title(title)
    is_intern = intern_title or cohort == "internship"
    is_newgrad = (
        newgrad_title
        or cohort == "newgrad"
        or entry_description
        or (generic_software and not is_intern)
    )
    if not (is_intern or is_newgrad):
        return False

    # Entry-level full-time means full-time: reject explicit contract,
    # temporary, and part-time evidence. Internships are naturally temporary.
    if is_newgrad and not is_intern:
        if F.get("non_fulltime") and F["non_fulltime"].search(detail):
            return False
        if F.get("experienced"):
            experienced_matches = list(F["experienced"].finditer(description))
            # "0-4 years of experience" contains the substring "4 years of
            # experience". Do not misread that upper bound as a 4-year minimum.
            for match in experienced_matches:
                if not entry_range or not (
                    entry_range.start() <= match.start() < entry_range.end()
                ):
                    return False

    role = "quant developer" if F.get("quant") and F["quant"].search(title) else "software engineering"
    level = "internship" if is_intern else "new-grad full-time"
    evidence = "title" if (intern_title or newgrad_title) else (
        f"{cohort} feed" if cohort else (
            f"description ({entry_range.group(0).strip()})"
            if entry_range else (
                "generic software title with no disqualifying evidence"
                if generic_software else "description"
            )
        )
    )
    job["_category"] = f"{role} {level}"
    job["_classification_reason"] = f"{level} evidence from {evidence}"
    return True


def too_old(posted: str, max_age_days) -> bool:
    """Evergreen pipeline reqs sit on boards for years. A Summer 2027 role was
    not posted in 2015. Unknown dates are kept — we only drop proven-stale."""
    if not max_age_days or not posted or len(posted) < 10 or posted[4] != "-":
        return False
    try:
        d = datetime.strptime(posted[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - d).days > max_age_days


def matches(job: dict, F) -> bool:
    # Hosted ATS APIs are inconsistent about empty scalar fields: several emit
    # JSON null instead of an empty string. Normalize at the classifier boundary
    # so one malformed card cannot abort an otherwise successful full scan.
    title = str(job.get("title") or "")
    location = str(job.get("location") or "")
    job["title"] = title
    job["location"] = location
    text = f"{title} {location}"

    if F["kill"] and F["kill"].search(title):
        return False
    if F.get("url_kill") and F["url_kill"].search(str(job.get("url") or "")):
        return False
    if F["role"] and not F["role"].search(title):
        return False
    if not classify_target(job, F):
        return False
    # Location: explicit foreign evidence loses; otherwise require positive US
    # evidence somewhere in title or location. Vague postings are dropped on
    # purpose — an unlocatable role is not worth an alert.
    if F["not_us"] and F["not_us"].search(text):
        return False
    if F.get("loc_pfx") and F["loc_pfx"].search(location):
        return False
    if F["us"] and not F["us"].search(text):
        return False
    # Age: evergreen reqs sit on boards for years. But a title naming the 2027
    # cycle is the role being raced for regardless of when it was first posted.
    exempt = F.get("age_exempt") and F["age_exempt"].search(title)
    if not exempt and too_old(str(job.get("posted") or ""), F.get("max_age")):
        return False
    return True


def _age_days(posted):
    """Days since posting, or None if the date can't be read. Used by why.py."""
    p = str(posted or "")
    if len(p) < 10 or p[4] != "-":
        return None
    try:
        d = datetime.strptime(p[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - d).days


# ------------------------------------------------------------------- alerting
async def send_telegram(client, cfg, jobs):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    for j in jobs:  # one message per role — each is independently tappable
        warning = "\n⚠️ fallback delayed — official adapter unavailable" if j.get("_fallback_delayed") else ""
        text = (f"🚨 <b>{esc(j['company'])}</b>\n"
                f"{esc(j['title'])}\n"
                f"📍 {esc(j['location'] or '—')}\n"
                f"<a href=\"{j['url']}\">Apply now</a>{warning}")
        try:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=15,
            )
        except Exception as e:
            print(f"  ! telegram failed: {e}", file=sys.stderr)


async def deliver_discord_outbox(client, store):
    """Deliver every due Discord notification and retain failures for retry."""
    hook = os.environ.get("DISCORD_WEBHOOK")
    due = store.due_discord(limit=50)
    if not due:
        return 0
    if not hook:
        print(f"  ! {len(due)} Discord alert(s) pending; DISCORD_WEBHOOK is not set",
              file=sys.stderr)
        return 0

    sent = 0
    for chunk in [due[i:i + 10] for i in range(0, len(due), 10)]:
        embeds = []
        for j in chunk:
            embed = {
                "title": f"{j['company']} — {j['title']}"[:250] or "New role",
                "description": (str(j["location"] or "—") +
                                ("\n⚠️ fallback delayed — official adapter unavailable"
                                 if j.get("discovered_source_type") == "simplify" else ""))[:4000],
                "color": 0xE8543F,
            }
            if str(j.get("url") or "").startswith(("https://", "http://")):
                embed["url"] = j["url"]
            embeds.append(embed)
        ids = [j["outbox_id"] for j in chunk]
        try:
            response = await client.post(
                hook,
                params={"wait": "true"},
                json={"content": "🚨 New role(s)", "embeds": embeds,
                      "allowed_mentions": {"parse": []}},
                timeout=15,
            )
            if response.status_code == 429:
                retry_after = None
                try:
                    retry_after = max(1, int(float(response.json().get("retry_after", 1))))
                except Exception:
                    try:
                        retry_after = max(1, int(float(response.headers.get("Retry-After", 1))))
                    except Exception:
                        retry_after = 1
                store.mark_outbox_failed(ids, "Discord rate limit (429)", retry_after)
                continue
            response.raise_for_status()
            remote_id = None
            try:
                remote_id = str(response.json().get("id") or "") or None
            except Exception:
                pass
            store.mark_outbox_sent(ids, remote_id)
            sent += len(ids)
        except Exception as e:
            store.mark_outbox_failed(ids, f"{type(e).__name__}: {e}")
            print(f"  ! discord failed: {e}", file=sys.stderr)
    return sent


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------- core
_INSECURE_CLIENT = None


async def _insecure_client():
    """Client that skips TLS *hostname* checks. Used only for sources marked
    `insecure: true` in config — currently just Microsoft's careers API, whose
    Azure Front Door endpoint serves a wildcard cert that doesn't list its own
    hostname. We only ever GET public job listings over it: no credentials, no
    personal data, nothing to steal. Never mark a source insecure otherwise."""
    global _INSECURE_CLIENT
    if _INSECURE_CLIENT is None:
        _INSECURE_CLIENT = httpx.AsyncClient(verify=False, follow_redirects=True)
    return _INSECURE_CLIENT


async def fetch_source_result(client, src, sem):
    """Fetch one source while retaining success/failure as first-class state."""
    fn = adapters.REGISTRY.get(src["type"])
    result = {
        "source_key": src.get("_source_key") or storage.source_key(src),
        "source_type": src["type"],
        "source_label": src.get("_source_label") or storage.source_label(src),
        "ok": False,
        "jobs": [],
    }
    if not fn:
        result["error"] = f"unknown source type: {src['type']}"
        result["duration_ms"] = 0
        return result
    args = {k: v for k, v in src.items() if k != "type" and not k.startswith("_")}
    args.pop("generated", None)
    args.pop("promoted_from", None)
    if args.pop("insecure", False):
        client = await _insecure_client()
    async with sem:
        # Measure the source itself, not time waiting behind other sources.
        started = time.monotonic()
        for attempt in range(3):
            try:
                jobs = await fn(client, **args)
                result.update(ok=True, jobs=jobs, raw_count=len(jobs),
                              duration_ms=int((time.monotonic() - started) * 1000))
                return result
            except Exception as e:
                if attempt == 2:
                    print(f"  ! {src.get('company', src['type'])}: {type(e).__name__}: {e}",
                          file=sys.stderr)
                    result.update(error=f"{type(e).__name__}: {e}",
                                  duration_ms=int((time.monotonic() - started) * 1000))
                    return result
                await asyncio.sleep(1.5 * (attempt + 1))
    result["duration_ms"] = int((time.monotonic() - started) * 1000)
    return result


async def fetch_source(client, src, sem):
    """Compatibility wrapper for why.py and other diagnostic scripts."""
    return (await fetch_source_result(client, src, sem))["jobs"]


def promoted_sources(store: storage.JobStore) -> list[dict]:
    """Combine persisted promotions with new boards inferred from fallback jobs."""
    sources = store.generated_sources()
    # Newly inferred configurations come last and replace stale saved aliases
    # for the same logical board (notably Workday's osv-* public hostnames).
    sources.extend(source_discovery.discover_direct_sources(
        store.fallback_jobs_for_discovery()
    ))
    return list({source_discovery.promotion_identity(src): src
                 for src in sources}.values())


def prepare_sources(cfg: dict, generated_sources: list[dict] | None = None) -> list[dict]:
    """Apply fallback exclusions and attach stable scheduler metadata."""
    generated_sources = generated_sources or []
    srcs = []
    source_keys = set()
    for raw in list(cfg["sources"]) + list(generated_sources):
        src = dict(raw)
        src["_source_key"] = storage.source_key(src)
        src["_source_label"] = storage.source_label(src)
        if src["_source_key"] in source_keys:
            continue
        source_keys.add(src["_source_key"])
        if src.pop("skip_direct", False):
            # Direct-company suppression is health-aware in finalize_results.
            # Static exclusion caused blind spots whenever a configured board
            # was broken or had not completed its first successful poll.
            src["exclude"] = sorted(
                {c.lower() for c in cfg.get("never_alert_companies", [])}
            )
        srcs.append(src)
    return srcs


async def finalize_results(client, sem, srcs, results, store, F):
    """Attach trusted cohort evidence, classify, and resolve fallback dates."""
    healthy_direct = store.healthy_direct_companies()
    for src, result in zip(srcs, results):
        batch = result["jobs"]
        cohort = src.get("cohort")
        if not cohort and src["type"] == "simplify":
            cohort = "internship" if src.get("feed") == "internships" else "newgrad"
        if cohort:
            for job in batch:
                job["_cohort"] = cohort
        if src["type"] == "simplify":
            for job in batch:
                job["_simplify"] = True
                job["_fallback_delayed"] = True
        hits = [job for job in batch if matches(job, F)]
        if src["type"] == "simplify":
            result["audit_hits"] = list(hits)
        if src["type"] == "simplify" and healthy_direct:
            hits = [job for job in hits if str(job.get("company") or "").strip().casefold()
                    not in healthy_direct]
        result["hits"] = hits

    sf_candidates = [job for result in results for job in result.get("hits", [])
                     if job.get("_simplify")]
    if not sf_candidates:
        return results
    current_state = store.dashboard_state()
    unresolved_new = [job for job in sf_candidates if job["id"] not in current_state]
    unresolved_backfill = [
        job for job in sf_candidates
        if job["id"] in current_state
        and not current_state[job["id"]].get("_date_resolved")
    ]
    # Date enrichment is useful metadata, not a reason to hold alerts and the
    # dashboard hostage. A first health-aware fallback pass can expose hundreds
    # of unresolved historical roles, and many custom career URLs consume their
    # full timeout. Resolve a bounded foreground batch; persist every remaining
    # role immediately and backfill a few more on subsequent scans.
    sf_todo = unresolved_new[:24] + unresolved_backfill[:6]
    if not sf_todo:
        return results

    async def _resolve(job):
        async with sem:
            return await adapters.resolve_real_date(client, job["url"])

    dates = await asyncio.gather(*(_resolve(job) for job in sf_todo))
    for job, real in zip(sf_todo, dates):
        if real:
            job["posted"] = real
            job["_date_resolved"] = True
    still = [job for job in sf_todo if not job.get("_date_resolved")]
    if still:
        await adapters.resolve_batch_dates(client, still)
    resolved = sum(1 for job in sf_todo if job.get("_date_resolved"))
    if resolved:
        print(f"  resolved {resolved}/{len(sf_todo)} Simplify dates from ATS APIs")
    return results


async def poll_once(cfg, seed=False, store=None):
    F = compile_filters(cfg)
    store = store or storage.JobStore(DB_FILE)
    store.import_legacy(STATE_FILE)
    store.import_source_registry(DIRECT_SOURCE_FILE)
    store.retire_unmanaged_once("strict-targeting-v1")
    first_run = store.job_count() == 0

    promoted = promoted_sources(store)
    srcs = prepare_sources(cfg, promoted)
    store.sync_sources(srcs)
    health = {row["source_key"]: row for row in store.source_health()}
    seed_keys = {
        src["_source_key"] for src in srcs if src.get("generated")
        and not health.get(src["_source_key"], {}).get("last_success_at")
    }

    sem = asyncio.Semaphore(cfg.get("concurrency", 12))
    limits = httpx.Limits(max_connections=30, max_keepalive_connections=15)
    async with httpx.AsyncClient(limits=limits, follow_redirects=True, http2=False) as client:
        enqueue = not (seed or first_run)
        fresh = []
        all_results = []

        async def fetch_and_record(batch_srcs, force_seed=False):
            if not batch_srcs:
                return []
            batch_results = await asyncio.gather(
                *(fetch_source_result(client, source, sem) for source in batch_srcs)
            )
            await finalize_results(client, sem, batch_srcs, batch_results, store, F)
            for source, result in zip(batch_srcs, batch_results):
                source_seed = force_seed or source["_source_key"] in seed_keys
                added = store.record_poll(
                    [result], enqueue_notifications=enqueue and not source_seed,
                )
                if not source_seed:
                    fresh.extend(added)
                if source_seed and result.get("ok"):
                    seed_keys.discard(source["_source_key"])
            all_results.extend(batch_results)
            return batch_results

        # Official boards always complete first. Simplify then acts as an audit
        # and discovery pass, never as the preferred record for a healthy board.
        direct_srcs = [source for source in srcs if source["type"] != "simplify"]
        fallback_srcs = [source for source in srcs if source["type"] == "simplify"]
        await fetch_and_record(direct_srcs)
        fallback_results = await fetch_and_record(fallback_srcs)

        # A URL first observed in this Simplify pass is promoted and polled now,
        # during the same ephemeral Actions run. The fallback record already
        # owns any alert, so the initial board snapshot safely upgrades it.
        inferred = source_discovery.discover_direct_sources(
            job for result in fallback_results for job in result.get("audit_hits") or []
        )
        existing_keys = {source["_source_key"] for source in srcs}
        candidates = prepare_sources({**cfg, "sources": []}, inferred)
        new_sources = [source for source in candidates
                       if source["_source_key"] not in existing_keys]
        if new_sources:
            store.register_sources(new_sources)
            await fetch_and_record(new_sources, force_seed=True)
            srcs.extend(new_sources)
            print(f"  promoted and immediately polled {len(new_sources)} official source(s)")

        for company in store.healthy_direct_companies():
            store.deactivate_fallback_company(company)

        if enqueue and fresh:
            current = store.dashboard_state()
            for job in fresh:
                upgraded = current.get(str(job.get("id") or ""))
                if upgraded:
                    for field in ("company", "title", "location", "url", "posted"):
                        job[field] = upgraded.get(field, job.get(field))
                    job["_fallback_delayed"] = bool(upgraded.get("fallback_delayed"))
            fresh.sort(key=lambda j: (F["boost"] is None or F["boost"].search(j["company"]) is None, j["company"]))
            for j in fresh:
                print(f"  → {j['company']}: {j['title']} [{j['location']}]\n    {j['url']}")
            await send_telegram(client, cfg, fresh)

        delivered = 0
        if not seed:
            delivered = await deliver_discord_outbox(client, store)

        scan_completed_at = store.mark_scan_completed()
        state = store.dashboard_state()
        dashboard.write(state, store.source_health(), store.outbox_counts(),
                        scan_completed_at=scan_completed_at,
                        coverage=store.coverage_report())
        # Keep the existing Actions fallback viable without making JSON the
        # runtime source of truth. Pending alerts are omitted so an ephemeral
        # next run detects and retries them.
        store.export_legacy(STATE_FILE, exclude_pending=True)
        store.export_source_registry(DIRECT_SOURCE_FILE)
        store.export_coverage_report(COVERAGE_FILE, scan_completed_at)

        scanned = sum(int(r.get("raw_count") or 0) for r in all_results if r.get("ok"))
        open_count = sum(1 for rec in state.values() if rec.get("open"))
        print(f"[{time.strftime('%H:%M:%S')}] {scanned} scanned · "
              f"{open_count} open & matching · {len(fresh)} NEW · "
              f"{delivered} Discord delivered · dashboard.html updated")

        if seed or first_run:
            print("  (seed run — recorded baseline, no alerts sent)")
            return


def source_interval(src: dict, cfg: dict) -> int:
    """Return this source's independent steady-state polling cadence."""
    if src.get("interval_seconds"):
        return max(15, int(src["interval_seconds"]))
    intervals = cfg.get("poll_intervals") or {}
    source_type = src.get("type", "")
    if source_type == "simplify":
        bucket = "fallback"
    elif source_type.startswith("pw_"):
        bucket = "browser"
    elif source_type == "workday":
        bucket = "workday"
    elif source_type in {"eightfold", "amazon", "microsoft", "apple", "tiktok",
                         "garmin", "l3harris"}:
        bucket = "custom"
    else:
        bucket = "direct_api"
    return max(15, int(intervals.get(bucket, cfg.get("interval_seconds", 90))))


async def network_available(client=None) -> bool:
    """Return whether the machine can currently reach a public ATS endpoint.

    Use a tiny real HTTPS response: a bare TCP socket can succeed while DNS,
    TLS, or HTTP requests from the shared client are already timing out.
    """
    owned_client = client is None
    probe = client or httpx.AsyncClient(follow_redirects=True)
    try:
        response = await probe.get(
            "https://api.github.com/zen",
            headers={"User-Agent": "jobwatch-connectivity-probe"},
            timeout=5,
        )
        return response.status_code < 500
    except (httpx.HTTPError, OSError, asyncio.TimeoutError):
        return False
    finally:
        if owned_client:
            await probe.aclose()


def is_network_failure(result: dict) -> bool:
    """Identify transport failures suitable for the global circuit breaker."""
    if result.get("ok"):
        return False
    error = str(result.get("error") or "").lower()
    return any(marker in error for marker in (
        "connecttimeout", "connecterror", "readtimeout", "writetimeout",
        "pooltimeout", "networkerror", "remoteprotocolerror",
        "nodename nor servname", "name or service not known",
        "err_name_not_resolved", "err_network_changed", "err_internet_disconnected",
    ))


def write_outputs(store) -> None:
    scan_completed_at = store.mark_scan_completed()
    state = store.dashboard_state()
    dashboard.write(state, store.source_health(), store.outbox_counts(),
                    scan_completed_at=scan_completed_at,
                    coverage=store.coverage_report())
    store.export_legacy(STATE_FILE, exclude_pending=True)
    store.export_source_registry(DIRECT_SOURCE_FILE)
    store.export_coverage_report(COVERAGE_FILE, scan_completed_at)


async def run_scheduler(cfg: dict, stop_event: asyncio.Event | None = None) -> None:
    """Poll every source independently so slow adapters cannot delay fast ones."""
    F = compile_filters(cfg)
    store = storage.JobStore(DB_FILE)
    store.import_legacy(STATE_FILE)
    store.import_source_registry(DIRECT_SOURCE_FILE)
    retired = store.retire_unmanaged_once("strict-targeting-v1")
    if retired:
        print(f"strict targeting migration · retired {retired} unverified legacy rows")
    promoted = promoted_sources(store)
    srcs = prepare_sources(cfg, promoted)
    store.sync_sources(srcs)

    if store.job_count() == 0:
        print("empty database · recording one complete baseline before alerting")
        await poll_once(cfg, seed=True, store=store)

    concurrency = int(cfg.get("concurrency", 12))
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=max(30, concurrency * 2),
                          max_keepalive_connections=max(15, concurrency))
    now_mono = time.monotonic()
    now_wall = time.time()
    spread = max(0, int(cfg.get("initial_spread_seconds", 30)))
    health = {s["source_key"]: s for s in store.source_health()}
    seed_keys = {
        src["_source_key"] for src in srcs if src.get("generated")
        and not health.get(src["_source_key"], {}).get("last_success_at")
    }
    source_keys = {src["_source_key"] for src in srcs}
    next_due = {}
    for src in srcs:
        key = src["_source_key"]
        interval = source_interval(src, cfg)
        last_attempt = health.get(key, {}).get("last_attempt_at")
        if last_attempt:
            delay = max(0.0, interval - (now_wall - float(last_attempt)))
        else:
            digest = int(key.rsplit(":", 1)[-1][:8], 16)
            delay = (digest % (spread * 1000 + 1)) / 1000 if spread else 0.0
        next_due[key] = now_mono + delay

    running = {}
    running_keys = set()
    dirty = False
    last_dashboard = 0.0
    last_export = 0.0
    last_delivery = 0.0
    last_network_check = 0.0
    network_ok = True
    network_failure_streak = 0
    network_backoff_until = 0.0

    print(f"jobwatch scheduler running · {len(srcs)} independent sources "
          f"({len(promoted)} promoted direct) · {concurrency} concurrent · Ctrl-C to stop")
    async with httpx.AsyncClient(limits=limits, follow_redirects=True, http2=False) as client:
        # Date enrichment can involve many fallback jobs. Keep it off the
        # source-fetch semaphore so Simplify cannot queue ahead of direct ATS
        # polls, and do it inside each worker so the scheduler loop stays free
        # to persist other completed sources.
        resolution_sem = asyncio.Semaphore(min(4, concurrency))

        async def fetch_and_finalize(src):
            result = await fetch_source_result(client, src, sem)
            await finalize_results(client, resolution_sem, [src], [result], store, F)
            return result

        try:
            while stop_event is None or not stop_event.is_set():
                now = time.monotonic()
                should_probe = (
                    now - last_network_check >= 10
                    and (network_ok or now >= network_backoff_until)
                )
                if should_probe:
                    was_ok = network_ok
                    network_ok = await network_available(client)
                    last_network_check = time.monotonic()
                    if network_ok:
                        network_failure_streak = 0
                    else:
                        network_backoff_until = last_network_check + 30
                    if network_ok != was_ok:
                        print(
                            "network restored · resuming source polls"
                            if network_ok else
                            "network unavailable · pausing new source polls",
                            file=sys.stderr if not network_ok else sys.stdout,
                        )
                available = max(0, concurrency - len(running)) if network_ok else 0
                due = sorted(
                    (src for src in srcs
                     if src["_source_key"] not in running_keys
                     and next_due[src["_source_key"]] <= now),
                    key=lambda src: next_due[src["_source_key"]],
                )
                for src in due[:available]:
                    key = src["_source_key"]
                    task = asyncio.create_task(fetch_and_finalize(src))
                    running[task] = src
                    running_keys.add(key)
                    next_due[key] = now + source_interval(src, cfg)

                if running:
                    done, _ = await asyncio.wait(
                        set(running), timeout=1.0,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                else:
                    await asyncio.sleep(1.0)
                    done = set()

                for task in done:
                    src = running.pop(task)
                    running_keys.discard(src["_source_key"])
                    try:
                        result = task.result()
                    except Exception as exc:
                        result = {
                            "source_key": src["_source_key"], "source_type": src["type"],
                            "source_label": src["_source_label"], "ok": False, "jobs": [],
                            "error": f"{type(exc).__name__}: {exc}", "duration_ms": 0,
                        }
                    if result.get("ok"):
                        network_failure_streak = 0
                    elif is_network_failure(result):
                        network_failure_streak += 1
                        if network_failure_streak >= 5 and network_ok:
                            network_ok = False
                            network_backoff_until = time.monotonic() + 30
                            print(
                                "connection failure burst · pausing new source polls for 30s",
                                file=sys.stderr,
                            )
                    source_seed = src["_source_key"] in seed_keys
                    fresh = store.record_poll(
                        [result], enqueue_notifications=not source_seed,
                    )
                    if source_seed:
                        fresh = []
                        if result.get("ok"):
                            seed_keys.discard(src["_source_key"])
                    if src.get("generated") and result.get("ok") and \
                            int(result.get("raw_count") or 0) > 0 and \
                            store.generated_company_ready(src.get("company", "")):
                        store.deactivate_fallback_company(src.get("company", ""))
                    elif src["type"] != "simplify" and result.get("ok"):
                        store.deactivate_fallback_company(src.get("company", ""))
                    if fresh:
                        fresh.sort(key=lambda job: (
                            F["boost"] is None or F["boost"].search(job["company"]) is None,
                            job["company"],
                        ))
                        for job in fresh:
                            print(f"  → {job['company']}: {job['title']} [{job['location']}]\n"
                                  f"    {job['url']}")
                        await send_telegram(client, cfg, fresh)

                    # A Simplify hit can reveal an official ATS board that was
                    # not known at startup. Persist and schedule it immediately;
                    # its first successful snapshot is seeded without alerts.
                    if src["type"] == "simplify" and result.get("ok"):
                        inferred = source_discovery.discover_direct_sources(
                            result.get("hits") or []
                        )
                        candidates = prepare_sources(
                            {**cfg, "sources": []}, inferred
                        )
                        new_sources = [candidate for candidate in candidates
                                       if candidate["_source_key"] not in source_keys]
                        if new_sources:
                            store.register_sources(new_sources)
                            for candidate in new_sources:
                                key = candidate["_source_key"]
                                source_keys.add(key)
                                seed_keys.add(key)
                                srcs.append(candidate)
                                next_due[key] = time.monotonic()
                            print(f"  promoted {len(new_sources)} new official source(s) from fallback")
                    dirty = True

                now = time.monotonic()
                delivery_interval = 5 if os.environ.get("DISCORD_WEBHOOK") else 60
                delivery_due = network_ok and (
                    (bool(done) and bool(os.environ.get("DISCORD_WEBHOOK"))) or
                    now - last_delivery >= delivery_interval
                )
                if delivery_due and store.due_discord(limit=1):
                    await deliver_discord_outbox(client, store)
                    last_delivery = now
                    dirty = True
                if dirty and now - last_dashboard >= 5:
                    scan_completed_at = store.mark_scan_completed()
                    state = store.dashboard_state()
                    dashboard.write(state, store.source_health(), store.outbox_counts(),
                                    scan_completed_at=scan_completed_at,
                                    coverage=store.coverage_report())
                    last_dashboard = now
                    dirty = False
                if now - last_export >= 60:
                    store.export_legacy(STATE_FILE, exclude_pending=True)
                    store.export_source_registry(DIRECT_SOURCE_FILE)
                    store.export_coverage_report(
                        COVERAGE_FILE, store.scan_completed_at() or int(time.time())
                    )
                    last_export = now
        finally:
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            write_outputs(store)


async def main():
    load_local_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    a = ap.parse_args()

    cfg = yaml.safe_load(Path(a.config).read_text())

    # Fail loudly on a config type with no adapter, instead of silently
    # skipping the source and quietly watching nothing.
    unknown = {s["type"] for s in cfg["sources"]} - set(adapters.REGISTRY)
    if unknown:
        print(f"! unknown source type(s) in config: {', '.join(sorted(unknown))}",
              file=sys.stderr)
        print(f"  adapters available: {', '.join(sorted(adapters.REGISTRY))}",
              file=sys.stderr)
        print("  your adapters.py is probably older than your config.yaml.",
              file=sys.stderr)
        sys.exit(2)

    try:
        if a.once or a.seed:
            await poll_once(cfg, seed=a.seed)
            return

        await run_scheduler(cfg)
    finally:
        await pw_scrapers.close_browser()


if __name__ == "__main__":
    asyncio.run(main())
