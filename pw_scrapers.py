"""
Playwright-based scrapers for career sites that require JS rendering.

These cover companies whose career pages have no public REST API:
  - They render job listings client-side (React/Next.js)
  - They sit behind Cloudflare or other bot protection
  - Their APIs are paginated server-side with no query params

Each function follows the same adapter signature: async (client, **kw) -> list[dict].
The httpx client is unused — Playwright drives its own browser.
"""
from __future__ import annotations

import asyncio
import html as html_lib
import json
import re
import time

from adapters import _uid, _iso, _plain

# Lazy-initialized shared browser
_BROWSER = None
_PW = None
_BROWSER_START_TASK = None
_META_DETAIL_CACHE = {}


def _meta_description_from_html(page_html: str) -> str:
    """Extract Meta's official description/qualifications from JSON-LD."""
    for match in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        page_html or "", re.I | re.DOTALL,
    ):
        try:
            payload = json.loads(html_lib.unescape(match.group(1)))
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, list):
            payload = next((item for item in payload if isinstance(item, dict)), {})
        if not isinstance(payload, dict):
            continue
        description = _plain(
            payload.get("description"), payload.get("qualifications"),
            payload.get("responsibilities"), payload.get("experienceRequirements"),
        )
        if description:
            return description
    return ""


async def _meta_detail_description(browser, url: str) -> str:
    now = time.monotonic()
    cached = _META_DETAIL_CACHE.get(url)
    if cached:
        fetched_at, description = cached
        if now - fetched_at < (21600 if description else 300):
            return description
    detail_page = None
    try:
        # Meta returns HTTP 400 to the shared httpx client even though the same
        # public page works in a browser. Use the already-running Playwright
        # browser so detail enrichment is as reliable as the search scrape.
        detail_page = await _new_page(browser)
        await detail_page.goto(url, wait_until="domcontentloaded", timeout=30000)
        description = _meta_description_from_html(await detail_page.content())
    except Exception:
        description = ""
    finally:
        if detail_page is not None:
            await detail_page.close()
    _META_DETAIL_CACHE[url] = (now, description)
    return description


async def _start_browser():
    from playwright.async_api import async_playwright
    try:
        from playwright_stealth import stealth_async
    except ImportError:
        stealth_async = None
    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
    except Exception:
        await playwright.stop()
        raise
    browser._stealth = stealth_async  # stash for page setup
    return playwright, browser


async def _get_browser():
    global _BROWSER, _PW, _BROWSER_START_TASK
    if _BROWSER is None:
        # Several Playwright adapters are polled concurrently. Share the same
        # in-flight startup task so they cannot each launch their own browser.
        if _BROWSER_START_TASK is None:
            _BROWSER_START_TASK = asyncio.create_task(_start_browser())
        startup = _BROWSER_START_TASK
        try:
            _PW, _BROWSER = await startup
        finally:
            if _BROWSER_START_TASK is startup:
                _BROWSER_START_TASK = None
    return _BROWSER


async def close_browser():
    """Cleanly stop the shared browser before the asyncio loop is closed."""
    global _BROWSER, _PW, _BROWSER_START_TASK
    browser, playwright = _BROWSER, _PW
    _BROWSER = None
    _PW = None
    _BROWSER_START_TASK = None
    if browser is not None:
        try:
            await browser.close()
        except Exception:
            pass
    if playwright is not None:
        try:
            await playwright.stop()
        except Exception:
            pass


async def _new_page(browser):
    page = await browser.new_page(
        viewport={"width": 1440, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
    )
    stealth = getattr(browser, "_stealth", None)
    if stealth:
        await stealth(page)
    return page


# ------------------------------------------------------------------- Optiver
async def optiver(client, company="Optiver", **kw):
    browser = await _get_browser()
    page = await _new_page(browser)
    try:
        await page.goto(
            "https://www.optiver.com/working-at-optiver/career-opportunities/",
            wait_until="domcontentloaded", timeout=30000,
        )
        # Analytics and consent requests can remain open indefinitely. The
        # scraper only needs the job cards, so wait for those instead of the
        # entire network becoming idle.
        await page.locator('a[href*="/join-us/jobs/"]').first.wait_for(
            state="attached", timeout=20000,
        )
        await page.wait_for_timeout(2000)
        # Click "Load more" until all jobs are visible
        for _ in range(20):
            btn = page.locator("button:has-text('Load')")
            if await btn.count() == 0:
                break
            try:
                await btn.first.click()
                await page.wait_for_timeout(1000)
            except Exception:
                break

        jobs = await page.evaluate("""() => {
            const items = [];
            document.querySelectorAll('a[href*="/join-us/jobs/"]').forEach(a => {
                const href = a.href;
                // Skip nav/skip links
                if (href.endsWith('/join-us/jobs/') || href.includes('#')) return;
                const text = a.textContent.trim();
                if (text.length < 3 || text.length > 200) return;
                // Extract location from URL path: /join-us/jobs/{domain}/{location}/{slug}/
                const parts = new URL(href).pathname.split('/').filter(Boolean);
                // parts = ['join-us', 'jobs', domain, location, slug]
                const loc = parts.length >= 5 ? parts[3].replace(/-/g, ' ') : '';
                items.push({
                    title: text,
                    location: loc.charAt(0).toUpperCase() + loc.slice(1),
                    href: href,
                });
            });
            return items;
        }""")
        seen = set()
        out = []
        for j in jobs:
            if j["href"] in seen:
                continue
            seen.add(j["href"])
            out.append({
                "id": _uid("optiver", j["href"]),
                "company": company,
                "title": j["title"],
                "location": j["location"],
                "url": j["href"],
                "posted": "",
            })
        return out
    finally:
        await page.close()


# -------------------------------------------------------------------- HRT
async def hrt(client, company="Hudson River Trading", **kw):
    browser = await _get_browser()
    page = await _new_page(browser)
    try:
        await page.goto(
            "https://www.hudsonrivertrading.com/careers/",
            wait_until="domcontentloaded", timeout=30000,
        )
        await page.locator('a[href*="/hrt-job/"]').first.wait_for(
            state="attached", timeout=20000,
        )
        await page.wait_for_timeout(1000)

        jobs = await page.evaluate("""() => {
            const items = [];
            document.querySelectorAll('a[href*="/hrt-job/"]').forEach(a => {
                const text = a.textContent.trim();
                if (text.length > 3 && text.length < 200 && text !== '↳ Apply Now') {
                    items.push({
                        title: text.split('\\n')[0].trim(),
                        href: a.href,
                    });
                }
            });
            return items;
        }""")

        seen = set()
        out = []
        for j in jobs:
            if j["href"] in seen:
                continue
            seen.add(j["href"])
            out.append({
                "id": _uid("hrt", j["href"]),
                "company": company,
                "title": j["title"],
                "location": "New York, NY",
                "url": j["href"],
                "posted": "",
            })
        return out
    finally:
        await page.close()


# ----------------------------------------------------------------- DE Shaw
async def deshaw(client, company="D. E. Shaw", **kw):
    browser = await _get_browser()
    page = await _new_page(browser)
    try:
        # Fetch all career pages (internships + full-time)
        await page.goto(
            "https://www.deshaw.com/careers",
            wait_until="networkidle", timeout=30000,
        )
        await page.wait_for_timeout(3000)

        jobs = await page.evaluate("""() => {
            const skip = new Set([
                '/careers', '/careers/', '/careers/internships',
                '/careers/career-development', '/careers/choose-your-path',
                '/careers/interviewing', '/careers/benefits',
                '/careers/full-time-opportunities',
            ]);
            const items = [];
            document.querySelectorAll('a[href*="/careers/"]').forEach(a => {
                const path = new URL(a.href).pathname.replace(/\\/$/, '');
                if (skip.has(path)) return;
                // Real job URLs end with a numeric ID like /careers/slug-1234
                if (!/\\d+$/.test(path)) return;
                const row = a.closest('[class*="job"], [class*="listing"], [class*="bundle"], li, div');
                const loc = row ? (row.querySelector('[class*="location"]') || {}).textContent : '';
                let title = a.textContent.trim().split('\\n')[0].trim();
                // Remove leading "icon" artifact from styled-components
                title = title.replace(/^icon/, '');
                // DE Shaw embeds descriptions after ": The D. E. Shaw..."
                const m = title.match(/:\\s+The\\s+D[\\.\\s\\u00a0]+E[\\.\\s\\u00a0]+Shaw/);
                if (m) title = title.substring(0, m.index);
                if (title.length > 3 && title.length < 300) {
                    items.push({
                        title: title,
                        location: (loc || '').trim(),
                        href: a.href,
                    });
                }
            });
            return items;
        }""")

        seen = set()
        out = []
        for j in jobs:
            if j["href"] in seen:
                continue
            seen.add(j["href"])
            out.append({
                "id": _uid("deshaw", j["href"]),
                "company": company,
                "title": j["title"],
                "location": j["location"] or "New York, NY",
                "url": j["href"],
                "posted": "",
            })
        return out
    finally:
        await page.close()


# --------------------------------------------------------------- Two Sigma
async def twosigma(client, company="Two Sigma", **kw):
    browser = await _get_browser()
    page = await _new_page(browser)
    try:
        await page.goto(
            "https://careers.twosigma.com/careers/OpenRoles",
            wait_until="networkidle", timeout=30000,
        )
        await page.wait_for_timeout(3000)

        # Scroll to load all listings
        for _ in range(10):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1000)

        jobs = await page.evaluate("""() => {
            const items = [];
            document.querySelectorAll('a[href*="JobDetail"]').forEach(a => {
                const title = a.textContent.trim();
                if (!title || title.length < 3 || title === 'View role') return;
                // Location is often in sibling elements within the job card
                const card = a.closest('[class*="card"], [class*="role"], [class*="job"], li, article');
                let loc = '';
                if (card) {
                    const locEl = card.querySelector('[class*="location"], [class*="loc"]');
                    loc = locEl ? locEl.textContent.trim() : '';
                }
                items.push({
                    title: title,
                    location: loc,
                    href: a.href,
                });
            });
            return items;
        }""")

        seen = set()
        out = []
        for j in jobs:
            if j["href"] in seen:
                continue
            seen.add(j["href"])
            out.append({
                "id": _uid("twosigma", j["href"]),
                "company": company,
                "title": j["title"],
                "location": j["location"] or "New York, NY",
                "url": j["href"],
                "posted": "",
            })
        return out
    finally:
        await page.close()


# ---------------------------------------------------------- Goldman Sachs
async def goldmansachs(client, company="Goldman Sachs", **kw):
    browser = await _get_browser()
    page = await _new_page(browser)
    try:
        await page.goto(
            "https://higher.gs.com/roles?q=software+engineer",
            wait_until="networkidle", timeout=45000,
        )
        await page.wait_for_timeout(5000)

        # Scroll to load more results
        for _ in range(5):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1500)

        jobs = await page.evaluate("""() => {
            const items = [];
            document.querySelectorAll('a[href*="/roles/"]').forEach(a => {
                if (!a.href.match(/\\/roles\\/\\d+/)) return;
                // Title is in the first <span> child
                const span = a.querySelector('span');
                if (!span) return;
                const title = span.textContent.trim();
                if (!title || title.length < 5 || title.includes('Showing')) return;
                // Location is in a sibling div with · separators
                const locDiv = a.querySelector('div');
                let location = '';
                if (locDiv) {
                    const parts = locDiv.textContent.trim().split('·').map(s => s.trim());
                    // Usually: City · Country · Level
                    location = parts.slice(0, 2).join(', ');
                }
                items.push({ title, location, href: a.href });
            });
            return items;
        }""")

        seen = set()
        out = []
        for j in jobs:
            if j["href"] in seen:
                continue
            seen.add(j["href"])
            out.append({
                "id": _uid("gs", j["href"]),
                "company": company,
                "title": j["title"],
                "location": j["location"] or "New York, NY",
                "url": j["href"],
                "posted": "",
            })
        return out
    finally:
        await page.close()


# -------------------------------------------------------------------- Meta
async def meta(client, company="Meta", **kw):
    browser = await _get_browser()
    page = await _new_page(browser)
    try:
        await page.goto(
            "https://www.metacareers.com/jobs?q=software%20engineer%20intern",
            wait_until="networkidle", timeout=45000,
        )
        await page.wait_for_timeout(5000)

        # Scroll to load more results
        for _ in range(10):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1500)

        jobs = await page.evaluate("""() => {
            const items = [];
            document.querySelectorAll('a[href*="/profile/job_details/"]').forEach(a => {
                const href = a.href;
                // Title is in <h3>, location is the first <span> sibling
                const h3 = a.querySelector('h3');
                if (!h3) return;
                const title = h3.textContent.trim();
                // Find first span with location text (contains city/state)
                const spans = a.querySelectorAll('span');
                let location = '';
                for (const s of spans) {
                    const t = s.textContent.trim();
                    if (t.length > 3 && t !== title && !t.startsWith('⋅')) {
                        location = t;
                        break;
                    }
                }
                if (title.length > 3 && title.length < 200) {
                    items.push({ title, location, href });
                }
            });
            return items;
        }""")

        seen = set()
        out = []
        for j in jobs:
            url = j["href"].split("?")[0]
            if url in seen:
                continue
            seen.add(url)
            out.append({
                "id": _uid("meta", url),
                "company": company,
                "title": j["title"],
                "location": j["location"],
                "url": url,
                "posted": "",
            })
        if out:
            detail_sem = asyncio.Semaphore(4)

            async def enrich(job):
                async with detail_sem:
                    return await _meta_detail_description(browser, job["url"])

            descriptions = await asyncio.gather(*(enrich(job) for job in out))
            for job, description in zip(out, descriptions):
                if description:
                    job["description"] = description
        return out
    finally:
        await page.close()


# ----------------------------------------------------------------- Citadel
async def citadel(client, company="Citadel", **kw):
    browser = await _get_browser()
    page = await _new_page(browser)
    try:
        url = kw.get("url", "https://www.citadel.com/careers/open-opportunities/")
        await page.goto(url, wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(5000)

        _extract = """() => {
            const items = [];
            document.querySelectorAll('a[href*="/careers/details/"]').forEach(a => {
                const lines = a.textContent.trim().split('\\n').map(s => s.trim()).filter(Boolean);
                const title = lines[0] || '';
                let location = '';
                for (let i = 1; i < lines.length; i++) {
                    if (lines[i] !== 'New' && !lines[i].includes('Apply')) {
                        location = lines[i];
                        break;
                    }
                }
                if (title.length > 3 && title.length < 200) {
                    items.push({ title, location, href: a.href });
                }
            });
            return items;
        }"""

        # Citadel uses page-number pagination (10 per page)
        all_jobs = await page.evaluate(_extract)
        for pg in range(2, 15):
            pg_url = url.rstrip("/") + f"/page/{pg}/"
            try:
                await page.goto(pg_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)
                batch = await page.evaluate(_extract)
                if not batch:
                    break
                all_jobs.extend(batch)
            except Exception:
                break

        seen = set()
        out = []
        for j in all_jobs:
            if j["href"] in seen:
                continue
            seen.add(j["href"])
            out.append({
                "id": _uid("citadel", j["href"]),
                "company": company,
                "title": j["title"],
                "location": j["location"],
                "url": j["href"],
                "posted": "",
            })
        return out
    finally:
        await page.close()


# ------------------------------------------------------------------ Tesla
async def tesla(client, company="Tesla", **kw):
    browser = await _get_browser()
    page = await _new_page(browser)
    try:
        await page.goto(
            "https://www.tesla.com/careers/search/?query=software%20engineer&country=US",
            wait_until="networkidle", timeout=45000,
        )
        await page.wait_for_timeout(5000)

        # Scroll to load more results
        for _ in range(10):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1500)

        jobs = await page.evaluate("""() => {
            const items = [];
            document.querySelectorAll('a[href*="/careers/"], a[href*="/job/"]').forEach(a => {
                const href = a.href;
                if (href.endsWith('/careers/') || href.endsWith('/search/')) return;
                const title = a.querySelector('h3, h4, [class*="title"]') || a;
                const row = a.closest('[class*="card"], [class*="row"], li');
                const loc = row ? (row.querySelector('[class*="location"]') || {}).textContent : '';
                const text = (title.textContent || '').trim();
                if (text && text.length > 3 && text.length < 200) {
                    items.push({
                        title: text,
                        location: (loc || '').trim(),
                        href: href,
                    });
                }
            });
            return items;
        }""")

        seen = set()
        out = []
        for j in jobs:
            if j["href"] in seen:
                continue
            seen.add(j["href"])
            out.append({
                "id": _uid("tesla", j["href"]),
                "company": company,
                "title": j["title"],
                "location": j["location"],
                "url": j["href"],
                "posted": "",
            })
        return out
    finally:
        await page.close()


SCRAPERS = {
    "pw_optiver": optiver,
    "pw_hrt": hrt,
    "pw_deshaw": deshaw,
    "pw_twosigma": twosigma,
    "pw_goldmansachs": goldmansachs,
    "pw_meta": meta,
    "pw_citadel": citadel,
    "pw_tesla": tesla,
}
