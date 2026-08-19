#!/usr/bin/env python3
"""
Render state.json into a single self-contained dashboard.html.

    python dashboard.py          # rebuild from current state and print the path

main.py calls this automatically after every poll, so the page is never
more than one poll interval stale.
"""
from __future__ import annotations

import html
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
OUT = ROOT / "dashboard.html"

CSS = """
:root{
  --ink:#0F1419; --surface:#161D25; --edge:#232D38;
  --paper:#E6E2D9; --muted:#78889A;
  --fresh:#F0A93B; --warm:#C9762F; --cool:#5E9BB5; --done:#4A8F6D;
  --dead:#B5544A;
}
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:var(--ink); color:var(--paper); min-height:100vh;
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,sans-serif;
  padding:32px 20px 80px;
}
.mono{font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace}
.wrap{max-width:1080px;margin:0 auto}

header{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;margin-bottom:4px}
h1{font-size:19px;font-weight:600;letter-spacing:-.01em}
.stamp{font-size:12px;color:var(--muted);letter-spacing:.04em}
.lede{color:var(--muted);font-size:13px;margin-bottom:24px}

.stats{display:flex;gap:28px;flex-wrap:wrap;padding:16px 0 20px;border-bottom:1px solid var(--edge)}
.stat b{display:block;font-size:26px;font-weight:600;letter-spacing:-.02em;line-height:1.1}
.stat span{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.09em}
.stat.hot b{color:var(--fresh)}

.controls{display:flex;gap:10px;flex-wrap:wrap;margin:20px 0 18px}
input[type=search],select{
  background:var(--surface);border:1px solid var(--edge);color:var(--paper);
  padding:9px 12px;border-radius:6px;font-size:14px;font-family:inherit;
}
input[type=search]{flex:1;min-width:200px}
input[type=search]:focus,select:focus{outline:2px solid var(--cool);outline-offset:1px}
.chip{
  background:transparent;border:1px solid var(--edge);color:var(--muted);
  padding:9px 14px;border-radius:6px;font-size:13px;cursor:pointer;font-family:inherit;
}
.chip[aria-pressed=true]{border-color:var(--fresh);color:var(--fresh)}
#live{margin-left:auto}
#live[aria-pressed=true]{border-color:var(--done);color:var(--done)}
.dot{display:inline-block;width:6px;height:6px;border-radius:50%;
     background:var(--done);margin-right:7px;vertical-align:middle}
#live[aria-pressed=false] .dot{background:var(--muted)}

.row{
  display:grid;grid-template-columns:3px 1fr auto;gap:16px;align-items:center;
  padding:13px 0 13px 0;border-bottom:1px solid var(--edge);
}
.row.applied{opacity:.4}
.row.na{opacity:.32}
.row.na .title a{text-decoration:line-through;text-decoration-color:var(--dead)}
.row.na .gut{background:var(--dead)!important}
/* the freshness gutter — the whole point of this tool is time */
.gut{width:3px;height:100%;min-height:38px;border-radius:2px;background:var(--edge)}
.row[data-age="0"] .gut{background:var(--fresh)}
.row[data-age="1"] .gut{background:var(--warm)}
.row[data-age="2"] .gut{background:var(--cool);opacity:.6}

.co{font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted)}
.title{font-size:15px;font-weight:500;margin:2px 0 3px}
.title a{color:var(--paper);text-decoration:none}
.title a:hover{color:var(--fresh);text-decoration:underline}
.meta{font-size:12px;color:var(--muted);display:flex;gap:12px;flex-wrap:wrap}
.new{color:var(--fresh);font-weight:600}

.right{display:flex;align-items:center;gap:14px;white-space:nowrap}
.when{font-size:12px;color:var(--muted);text-align:right}
.mark{
  background:transparent;border:1px solid var(--edge);color:var(--muted);
  width:30px;height:30px;border-radius:6px;cursor:pointer;font-size:14px;flex:none;
}
.mark[aria-pressed=true]{border-color:var(--done);color:var(--done)}
.mark.na[aria-pressed=true]{border-color:var(--dead);color:var(--dead)}
.mark:focus-visible{outline:2px solid var(--cool);outline-offset:2px}
.marks{display:flex;gap:6px}

.badge{
  font-size:9px;letter-spacing:.07em;text-transform:uppercase;
  padding:2px 6px;border-radius:3px;font-weight:600;vertical-align:middle;
}
.badge.direct{color:var(--done);border:1px solid var(--done)}
.badge.resolved{color:var(--cool);border:1px solid var(--cool)}
.badge.scraped{color:var(--muted);border:1px solid var(--edge)}
.badge.pending,.badge.retry{color:var(--fresh);border:1px solid var(--fresh)}
.badge.sent{color:var(--done);border:1px solid var(--done)}
.when .seen{color:var(--muted);font-style:italic}

.health{margin:0 0 18px;padding:12px 14px;border:1px solid var(--edge);
        border-radius:6px;color:var(--muted);font-size:12px}
.health summary{cursor:pointer;color:var(--paper)}
.health ul{margin:10px 0 0 18px}
.health .bad{color:var(--dead)}

.empty{padding:56px 0;text-align:center;color:var(--muted)}
.empty b{display:block;color:var(--paper);font-weight:500;margin-bottom:6px}
@media (max-width:640px){
  .row{grid-template-columns:3px 1fr;gap:12px}
  .right{grid-column:2;justify-content:flex-start;margin-top:6px}
}
@media (prefers-reduced-motion:no-preference){
  .row{transition:opacity .15s ease}
}
"""

JS = """
const JOBS = __DATA__;
const $ = s => document.querySelector(s);
let marks = {};
try { marks = JSON.parse(localStorage.getItem('jw_marks') || '{}'); } catch(e){}
// migrate the old applied-only format
try {
  const old = JSON.parse(localStorage.getItem('jw_applied') || '{}');
  for (const k in old) if (!marks[k]) marks[k] = 'applied';
} catch(e){}

function save(){ try{ localStorage.setItem('jw_marks', JSON.stringify(marks)); }catch(e){} }

function e(v){
  return String(v == null ? '' : v).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function safeUrl(v){
  try { const u = new URL(v); return /^(https?):$/.test(u.protocol) ? e(u.href) : '#'; }
  catch (_) { return '#'; }
}

function ago(days, prefix){
  prefix = prefix || 'posted';
  if (days == null) return '';
  if (days === 0) return prefix + ' today';
  if (days === 1) return prefix + ' yesterday';
  if (days < 7)  return prefix + ' ' + days + 'd ago';
  if (days < 30) return prefix + ' ' + Math.floor(days/7) + 'w ago';
  return prefix + ' ' + Math.floor(days/30) + 'mo ago';
}
function dateLabel(j){
  const detected = j.detected_seconds == null ? ''
    : j.detected_seconds < 60 ? `detected ${j.detected_seconds}s ago`
    : j.detected_seconds < 3600 ? `detected ${Math.floor(j.detected_seconds/60)}m ago`
    : j.detected_seconds < 86400 ? `detected ${Math.floor(j.detected_seconds/3600)}h ago`
    : ago(Math.floor(j.detected_seconds/86400), 'detected');
  let posted = 'company date unknown';
  if (j.posted_date) {
    const bits = j.posted_date.split('-').map(Number);
    let exact = new Date(Date.UTC(bits[0], bits[1] - 1, bits[2]))
      .toLocaleDateString('en-US', {month:'short', day:'numeric', year:'numeric', timeZone:'UTC'});
    if (j.posted_has_time && j.posted_ts != null) {
      exact = new Date(j.posted_ts * 1000).toLocaleString('en-US', {
        month:'short', day:'numeric', year:'numeric', hour:'numeric', minute:'2-digit',
        timeZone:'America/Los_Angeles', timeZoneName:'short'
      });
    }
    posted = ago(j.age, 'posted') + ' · ' + exact;
  }
  return e(detected) + '<br><span class="seen">' + e(posted) + '</span>';
}

function sortRows(rows){
  const mode = $('#sort').value;
  rows.sort((a, b) => {
    const ad = a.detected_seconds == null ? Number.MAX_SAFE_INTEGER : a.detected_seconds;
    const bd = b.detected_seconds == null ? Number.MAX_SAFE_INTEGER : b.detected_seconds;
    if (mode === 'detected') return ad - bd || a.company.localeCompare(b.company);
    const ap = a.posted_ts, bp = b.posted_ts;
    if (ap == null && bp != null) return 1;
    if (ap != null && bp == null) return -1;
    if (ap != null && bp != null && ap !== bp) return bp - ap;
    return ad - bd || a.company.localeCompare(b.company);
  });
  return rows;
}

function render(){
  const q = $('#q').value.toLowerCase().trim();
  const co = $('#co').value;
  const hideHandled = $('#hide').getAttribute('aria-pressed') === 'true';
  const freshOnly  = $('#fresh').getAttribute('aria-pressed') === 'true';
  const srcFilter  = $('#src').value;

  const rows = sortRows(JOBS.filter(j => {
    if (co && j.company !== co) return false;
    if (hideHandled && marks[j.id]) return false;
    if (freshOnly && !(j.detected_seconds !== null && j.detected_seconds <= 172800)) return false;
    if (srcFilter && j.source_type !== srcFilter) return false;
    if (!q) return true;
    return (j.title + ' ' + j.company + ' ' + j.location).toLowerCase().includes(q);
  }));

  $('#count').textContent = rows.length;
  const nApplied = JOBS.filter(j => marks[j.id] === 'applied').length;
  const nNa = JOBS.filter(j => marks[j.id] === 'na').length;
  $('#applied').textContent = nApplied;
  $('#na').textContent = nNa;
  const list = $('#list');

  if (!rows.length){
    list.innerHTML = '<div class="empty"><b>Nothing matches these filters.</b>' +
      'Clear the search, or widen it in config.yaml and let the next poll run.</div>';
    return;
  }

  list.innerHTML = rows.map(j => {
    const ageVal = j.detected_seconds == null ? '' : Math.floor(j.detected_seconds/86400);
    const isFresh = j.detected_seconds !== null && j.detected_seconds <= 172800;
    return `
    <div class="row ${marks[j.id]==='applied'?'applied':''}${marks[j.id]==='na'?'na':''}" data-age="${ageVal===''?'':Math.min(ageVal,3)}">
      <div class="gut"></div>
      <div>
        <div class="co mono">${e(j.company)}</div>
        <div class="title"><a href="${safeUrl(j.url)}" target="_blank" rel="noopener">${e(j.title)}</a></div>
        <div class="meta">
          <span>${e(j.location || '\\u2014')}</span>
          ${isFresh ? '<span class="new">just detected</span>' : ''}
          <span class="badge direct">${e(j.source_type)}</span>
          ${j.category ? `<span class="badge resolved">${e(j.category)}</span>` : ''}
          ${j.alert_status ? `<span class="badge ${e(j.alert_status)}">discord ${e(j.alert_status)}</span>` : ''}
        </div>
      </div>
      <div class="right">
        <div class="when mono">${dateLabel(j)}</div>
        <div class="marks">
          <button class="mark" aria-pressed="${marks[j.id]==='applied'}"
                  title="Applied"
                  aria-label="Mark ${e(j.company)} ${e(j.title)} as applied"
                  data-id="${j.id}" data-mark="applied">\\u2713</button>
          <button class="mark na" aria-pressed="${marks[j.id]==='na'}"
                  title="Not applicable \\u2014 can\\u2019t or won\\u2019t apply"
                  aria-label="Mark ${e(j.company)} ${e(j.title)} as not applicable"
                  data-id="${j.id}" data-mark="na">\\u2715</button>
        </div>
      </div>
    </div>`;
  }).join('');

  list.querySelectorAll('.mark').forEach(b => b.onclick = () => {
    const id = b.dataset.id, want = b.dataset.mark;
    marks[id] === want ? delete marks[id] : marks[id] = want;   // click again to undo
    save(); render();
  });
}

// ---- auto-refresh -------------------------------------------------------
// The poller rewrites this file every cycle, but a browser tab won't notice
// on its own. Reloading is safe: your applied / not-applicable marks live in
// localStorage, and scroll position is restored by the browser.
const REFRESH_MS = 60000;
let timer = null;

function setLive(on){
  $('#live').setAttribute('aria-pressed', on);
  try { localStorage.setItem('jw_live', on ? '1' : '0'); } catch(e){}
  clearInterval(timer);
  if (on) timer = setInterval(() => {
    // don't yank the page out from under an active search
    if (document.activeElement === $('#q') && $('#q').value) return;
    // Persist this before navigation. Browsers restore form controls at
    // different points in the reload lifecycle, which previously let the
    // dropdown and rendered ordering disagree after an automatic refresh.
    try { localStorage.setItem('jw_sort', $('#sort').value); } catch(e){}
    location.reload();
  }, REFRESH_MS);
}

$('#live').onclick = () => setLive($('#live').getAttribute('aria-pressed') !== 'true');

let liveOn = true;
try { liveOn = localStorage.getItem('jw_live') !== '0'; } catch(e){}
setLive(liveOn);

// tick the "updated" stamp so a stale page is obvious at a glance
const BUILT = Date.now();
setInterval(() => {
  const s = Math.round((Date.now() - BUILT) / 1000);
  const el = $('#age');
  if (!el) return;
  el.textContent = s < 60 ? `${s}s ago`
                 : s < 3600 ? `${Math.floor(s/60)}m ago`
                 : `${Math.floor(s/3600)}h ago`;
}, 5000);

$('#q').oninput = render;
$('#co').onchange = render;
$('#src').onchange = render;
try {
  const savedSort = localStorage.getItem('jw_sort');
  $('#sort').value = savedSort === 'detected' ? 'detected' : 'posted';
} catch(e) { $('#sort').value = 'posted'; }
$('#sort').onchange = () => {
  try { localStorage.setItem('jw_sort', $('#sort').value); } catch(e){}
  render();
};
['hide','fresh'].forEach(id => $('#'+id).onclick = e => {
  const t = e.currentTarget;
  t.setAttribute('aria-pressed', t.getAttribute('aria-pressed') !== 'true');
  render();
});
render();
// Firefox/Safari may restore select state after inline scripts run. Reapply
// the saved sort once the page is shown, then render from that authoritative
// value so auto-refresh and a manual refresh behave identically.
window.addEventListener('pageshow', () => {
  try {
    const savedSort = localStorage.getItem('jw_sort');
    $('#sort').value = savedSort === 'detected' ? 'detected' : 'posted';
  } catch(e) { $('#sort').value = 'posted'; }
  render();
});
"""


def build(state: dict, sources: list[dict] | None = None,
          notification_counts: dict | None = None) -> str:
    sources = sources or []
    notification_counts = notification_counts or {}
    today = time.strftime("%Y-%m-%d")
    now_ts = time.time()
    jobs = []
    for jid, r in state.items():
        if not r.get("open", True):
            continue
        age = None
        posted_raw = str(r.get("posted") or "").strip()
        posted = posted_raw[:10]
        posted_ts = None
        posted_has_time = False
        if len(posted) == 10 and posted[4] == "-":
            try:
                posted_day = datetime.strptime(posted, "%Y-%m-%d").date()
                age = max((datetime.now().date() - posted_day).days, 0)
                if len(posted_raw) > 10:
                    parsed = datetime.fromisoformat(posted_raw.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    posted_ts = parsed.timestamp()
                    posted_has_time = True
                else:
                    posted_ts = datetime.combine(
                        posted_day, datetime.min.time(), tzinfo=timezone.utc
                    ).timestamp()
            except ValueError:
                age = None
                posted_ts = None

        detected_seconds = None
        fs = r.get("first_seen")
        if fs:
            detected_seconds = max(int(now_ts - fs), 0)

        jobs.append({
            "id": jid, "company": r.get("company", ""), "title": r.get("title", ""),
            "location": r.get("location", ""), "url": r.get("url", ""),
            "posted": posted_raw or None, "posted_date": posted or None,
            "posted_ts": posted_ts, "posted_has_time": posted_has_time, "age": age,
            "detected_seconds": detected_seconds,
            "source_type": r.get("_source_type") or "legacy",
            "source_label": r.get("_source_label") or "Legacy state import",
            "alert_status": r.get("alert_status"),
            "category": r.get("category") or "",
        })

    # Company posting date is the primary order. Sources that do not expose a
    # trustworthy date follow dated roles and use detection time as a fallback.
    jobs.sort(key=lambda j: (
        j["posted_ts"] is None,
        -j["posted_ts"] if j["posted_ts"] is not None else 0,
        j["detected_seconds"] if j["detected_seconds"] is not None else 10**12,
        j["company"],
    ))
    fresh = sum(1 for j in jobs if j["detected_seconds"] is not None and j["detected_seconds"] <= 172800)
    dated = sum(1 for j in jobs if j["age"] is not None)
    companies = sorted({j["company"] for j in jobs})
    source_types = sorted({j["source_type"] for j in jobs})
    company_sources = {}
    for job in jobs:
        company_sources.setdefault(job["company"], set()).add(job["source_type"])

    # Include configured companies even when they currently have zero eligible
    # openings. This separates source coverage from the filtered job list.
    tracked_sources = {}
    tracked_statuses = {}
    for source in sources:
        company = str(source.get("company") or "").strip()
        if not company:
            continue
        tracked_sources.setdefault(company, set()).add(
            str(source.get("source_type") or "unknown")
        )
        tracked_statuses.setdefault(company, set()).add(
            str(source.get("status") or "pending")
        )
    for company, source_set in company_sources.items():
        tracked_sources.setdefault(company, set()).update(source_set)
        tracked_statuses.setdefault(company, set()).add("observed")
    eligible_by_company = Counter(job["company"] for job in jobs)

    degraded = [s for s in sources if s.get("status") in ("degraded", "suspect_empty")]
    healthy = sum(1 for s in sources if s.get("status") == "healthy")
    pending_alerts = int(notification_counts.get("pending", 0)) + int(notification_counts.get("retry", 0))

    opts = "".join(f'<option value="{html.escape(c, quote=True)}">{html.escape(c)}</option>' for c in companies)
    src_opts = "".join(f'<option value="{html.escape(s, quote=True)}">{html.escape(s)}</option>' for s in source_types)
    data = json.dumps(jobs, ensure_ascii=False).replace("</", "<\\/")
    health_rows = "".join(
        f'<li><span class="bad">{html.escape(str(s.get("label") or s.get("source_key")))}</span>'
        f' — {html.escape(str(s.get("last_error") or s.get("status")))}</li>'
        for s in degraded[:30]
    )
    health = (f'<details class="health" open><summary>{len(degraded)} degraded source(s) · '
              f'{healthy}/{len(sources)} healthy</summary><ul>{health_rows}</ul></details>') if degraded else (
              f'<div class="health">{healthy}/{len(sources)} configured sources healthy</div>' if sources else "")
    coverage_rows = "".join(
        f'<li><span class="mono">{html.escape(company)}</span> — '
        f'{html.escape(", ".join(sorted(tracked_sources[company])))} · '
        f'{eligible_by_company.get(company, 0)} eligible open · '
        f'{html.escape(", ".join(sorted(tracked_statuses[company])))}</li>'
        for company in sorted(tracked_sources)
    )
    fallback_companies = sum(
        1 for source_set in tracked_sources.values() if "simplify" in source_set
    )
    coverage = (
        f'<details class="health"><summary>Company source coverage · '
        f'{len(tracked_sources)} tracked companies · {fallback_companies} using Simplify</summary>'
        f'<ul>{coverage_rows}</ul></details>'
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>jobwatch</title><style>{CSS}</style></head>
<body><div class="wrap">
<header>
  <h1>Open roles</h1>
  <span class="stamp mono">rendered {time.strftime('%b %d, %H:%M %Z')} · <span id="age">just now</span></span>
</header>
<p class="lede">Everything currently live on the boards you track, newest company-posted first.
Roles without a company posting date follow dated roles and are ordered by detection time.</p>

<div class="stats">
  <div class="stat"><b>{len(jobs)}</b><span>open now</span></div>
  <div class="stat hot"><b>{fresh}</b><span>last 48 hours</span></div>
  <div class="stat"><b>{len(companies)}</b><span>companies</span></div>
  <div class="stat"><b>{dated}</b><span>with company date</span></div>
  <div class="stat"><b>{pending_alerts}</b><span>Discord pending</span></div>
  <div class="stat"><b id="applied">0</b><span>applied</span></div>
  <div class="stat"><b id="na">0</b><span>not applicable</span></div>
</div>

{health}
{coverage}

<div class="controls">
  <input type="search" id="q" placeholder="Search title, company, location" aria-label="Search roles">
  <select id="co" aria-label="Filter by company"><option value="">All companies</option>{opts}</select>
  <select id="src" aria-label="Filter by source"><option value="">All sources</option>{src_opts}</select>
  <select id="sort" aria-label="Sort jobs">
    <option value="posted">Newest posted</option>
    <option value="detected">Newest detected</option>
  </select>
  <button class="chip" id="fresh" aria-pressed="false">Last 48h</button>
  <button class="chip" id="hide" aria-pressed="false">Hide handled</button>
  <button class="chip" id="live" aria-pressed="true"
          title="Reload automatically when the poller rewrites this file"><span class="dot"></span>Auto-refresh</button>
</div>

<p class="lede"><span id="count">0</span> showing</p>
<div id="list"></div>
</div>
<script>{JS.replace("__DATA__", data)}</script>
</body></html>"""


def write(state: dict, sources: list[dict] | None = None,
          notification_counts: dict | None = None) -> Path:
    OUT.write_text(build(state, sources, notification_counts), encoding="utf-8")
    return OUT


if __name__ == "__main__":
    try:
        import storage
        store = storage.JobStore(ROOT / "jobwatch.db")
        store.import_legacy(STATE_FILE)
        st = store.dashboard_state()
        p = write(st, store.source_health(), store.outbox_counts())
    except Exception:
        st = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        p = write(st)
    print(f"wrote {p}  ({sum(1 for r in st.values() if r.get('open', True))} open roles)")
