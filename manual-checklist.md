# Companies with no public feed — check these by hand

These run bespoke career sites with no open JSON endpoint. No poller can watch
them; anyone claiming otherwise is scraping HTML that breaks weekly. Bookmark
this list and check it **once a day** during recruiting season.

| Company | Where to look | Notes |
|---|---|---|
| Google | careers.google.com/students | Window is 2–4 weeks, ~mid-October. Check daily then. |
| Apple | jobs.apple.com | Rolling. |
| Meta | metacareers.com/jobs | Usually opens Aug–Sept. |
| TikTok / ByteDance | lifeattiktok.com/search | **Hard cap: 2 applications across all ByteDance affiliates**, reviewed in the order you applied. Spend them deliberately. |
| Netflix | explore.jobs.netflix.net | Rolling, few intern roles. |
| Bloomberg | bloomberg.com/careers | Large intern class, opens early. |
| Goldman Sachs | goldmansachs.com/careers | Opens July–Aug, closes fast. |
| JPMorgan | careers.jpmorgan.com | Same early timing. |
| Morgan Stanley | morganstanley.com/careers | |
| Citadel / Citadel Securities | citadel.com/careers | |
| Two Sigma | twosigma.com/careers | |
| D. E. Shaw | deshaw.com/careers | |
| Jane Street | janestreet.com/join-jane-street | Greenhouse token exists but 404s; use the site. |
| LinkedIn | linkedin.com/jobs | Microsoft subsidiary, separate board. |
| Snap | careers.snap.com | |
| Epic Systems | careers.epic.com | One of the largest US new-grad SWE hirers. |

## Why these can't be automated

jobwatch covers seven platforms: Greenhouse, Lever, Ashby, SmartRecruiters,
Workable, Recruitee, and Workday, plus custom adapters for Microsoft and Amazon.
Any company on one of those can be added in one line. Everyone else built their
own careers stack, and there's no endpoint to poll.

Large non-tech employers are usually **Workday** — those ARE coverable:

    python discover.py --workday <careers URL from your browser>

The remaining gap is iCIMS, Phenom, Oracle HCM, and SuccessFactors, which large
enterprises use and which don't expose clean public feeds. If a company you want
isn't found by `discover.py --add`, check its careers URL: if it says
`myworkdayjobs.com`, you can add it. If not, it goes on this list.
