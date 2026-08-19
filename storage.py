"""Durable state for jobwatch.

SQLite is the source of truth while the service is running.  The old
``state.json`` file is imported once and can still be exported for the
GitHub-Actions fallback.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Iterable


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_key TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL,
    config_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',
    last_attempt_at INTEGER,
    last_success_at INTEGER,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    consecutive_empty INTEGER NOT NULL DEFAULT 0,
    last_job_count INTEGER,
    last_match_count INTEGER,
    duration_ms INTEGER
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    company TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    posted TEXT NOT NULL DEFAULT '',
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    open INTEGER NOT NULL DEFAULT 1,
    managed INTEGER NOT NULL DEFAULT 1,
    discovered_source_key TEXT,
    discovered_source_type TEXT NOT NULL DEFAULT 'unknown',
    discovered_source_label TEXT NOT NULL DEFAULT '',
    date_resolved INTEGER NOT NULL DEFAULT 0,
    category TEXT NOT NULL DEFAULT '',
    classification_reason TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS observations (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source_key TEXT NOT NULL REFERENCES sources(source_key) ON DELETE CASCADE,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (job_id, source_key)
);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    sent_at INTEGER,
    last_error TEXT,
    remote_id TEXT,
    UNIQUE(job_id, channel)
);

CREATE INDEX IF NOT EXISTS idx_jobs_open ON jobs(open, first_seen);
CREATE INDEX IF NOT EXISTS idx_observations_source ON observations(source_key, active);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(channel, status, next_attempt_at);
"""

# A source fetch already retries three times inside one poll. Requiring three
# failed polls prevents a brief machine-wide DNS/network outage from making
# hundreds of otherwise healthy company adapters look independently broken.
SOURCE_FAILURE_THRESHOLD = 3


IDENTITY_FIELDS = (
    "type", "company", "token", "tenant", "site", "host", "subdomain",
    "domain", "brand", "feed", "only", "query", "search", "url", "org_id",
)


def source_key(src: dict) -> str:
    """Stable identity for one independently-polled source configuration."""
    identity = {k: src[k] for k in IDENTITY_FIELDS if k in src}
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha1(raw.encode()).hexdigest()[:12]
    return f"{src.get('type', 'unknown')}:{digest}"


def source_label(src: dict) -> str:
    company = str(src.get("company") or src.get("feed") or src.get("type") or "source")
    qualifier = src.get("search") or src.get("query")
    return f"{company} — {qualifier}" if qualifier else company


class JobStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)
            columns = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
            if "category" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN category TEXT NOT NULL DEFAULT ''")
            if "classification_reason" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN classification_reason TEXT NOT NULL DEFAULT ''")
            db.execute("PRAGMA journal_mode = WAL")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def job_count(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    def retire_unmanaged_once(self, migration_key: str) -> int:
        """Hide legacy rows once when a stricter classifier becomes active."""
        meta_key = f"migration:{migration_key}"
        with self._connect() as db:
            if db.execute("SELECT 1 FROM meta WHERE key=?", (meta_key,)).fetchone():
                return 0
            db.execute("UPDATE jobs SET open=0 WHERE managed=0")
            changed = int(db.execute("SELECT changes()").fetchone()[0])
            db.execute("INSERT INTO meta(key, value) VALUES(?, ?)",
                       (meta_key, str(int(time.time()))))
            return changed

    def import_legacy(self, path: str | Path) -> int:
        """Import state.json once. Imported rows are not re-alerted."""
        path = Path(path)
        with self._connect() as db:
            done = db.execute(
                "SELECT value FROM meta WHERE key='legacy_import_complete'"
            ).fetchone()
            if done:
                return 0
            try:
                state = json.loads(path.read_text()) if path.exists() else {}
            except (OSError, json.JSONDecodeError):
                state = {}
            now = int(time.time())
            imported = 0
            for jid, rec in state.items():
                if not isinstance(rec, dict):
                    continue
                first_seen = int(rec.get("first_seen") or now)
                source_type = "simplify" if rec.get("_date_resolved") else "legacy"
                db.execute(
                    """INSERT OR IGNORE INTO jobs
                       (id, company, title, location, url, posted, first_seen,
                        last_seen, open, managed, discovered_source_type,
                        discovered_source_label, date_resolved)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                    (jid, str(rec.get("company") or ""), str(rec.get("title") or ""),
                     str(rec.get("location") or ""), str(rec.get("url") or ""),
                     str(rec.get("posted") or ""), first_seen, first_seen,
                     int(bool(rec.get("open", True))), source_type,
                     "Legacy state import", int(bool(rec.get("_date_resolved")))),
                )
                imported += db.execute("SELECT changes()").fetchone()[0]
            db.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('legacy_import_complete', ?)",
                (str(now),),
            )
            return imported

    def sync_sources(self, sources: Iterable[dict]) -> None:
        """Register configured sources and retire observations for removed ones."""
        sources = list(sources)
        keys = [s.get("_source_key") or source_key(s) for s in sources]
        now = int(time.time())
        self.register_sources(sources)
        with self._connect() as db:
            # Reclassify legacy one-off failures under the debounced health
            # policy. Preserve the error and failure count for diagnostics.
            db.execute(
                """UPDATE sources SET status='healthy'
                   WHERE enabled=1 AND status='degraded'
                     AND last_success_at IS NOT NULL
                     AND consecutive_failures<?""",
                (SOURCE_FAILURE_THRESHOLD,),
            )
            if keys:
                marks = ",".join("?" for _ in keys)
                removed = db.execute(
                    f"SELECT source_key FROM sources WHERE enabled=1 AND source_key NOT IN ({marks})",
                    keys,
                ).fetchall()
            else:
                removed = db.execute(
                    "SELECT source_key FROM sources WHERE enabled=1"
                ).fetchall()
            for row in removed:
                key = row["source_key"]
                affected = [r[0] for r in db.execute(
                    "SELECT job_id FROM observations WHERE source_key=? AND active=1", (key,)
                )]
                db.execute("UPDATE sources SET enabled=0, status='disabled' WHERE source_key=?", (key,))
                db.execute("UPDATE observations SET active=0, last_seen=? WHERE source_key=?", (now, key))
                self._refresh_open(db, affected)

    def register_sources(self, sources: Iterable[dict]) -> None:
        """Upsert sources without disabling any existing scheduler entries."""
        with self._connect() as db:
            for src in sources:
                key = src.get("_source_key") or source_key(src)
                clean = {k: v for k, v in src.items() if not str(k).startswith("_")}
                db.execute(
                    """INSERT INTO sources
                       (source_key, source_type, company, label, config_json, enabled)
                       VALUES (?, ?, ?, ?, ?, 1)
                       ON CONFLICT(source_key) DO UPDATE SET
                         source_type=excluded.source_type,
                         company=excluded.company,
                         label=excluded.label,
                         config_json=excluded.config_json,
                         enabled=1""",
                    (key, str(src.get("type") or "unknown"),
                     str(src.get("company") or ""), source_label(src),
                     json.dumps(clean, sort_keys=True, default=str)),
                )

    def generated_sources(self) -> list[dict]:
        """Load direct source configurations previously promoted from fallback jobs."""
        sources = []
        with self._connect() as db:
            rows = db.execute("SELECT config_json FROM sources").fetchall()
        for row in rows:
            try:
                config = json.loads(row["config_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if config.get("generated"):
                sources.append(config)
        return sources

    def fallback_jobs_for_discovery(self) -> list[dict]:
        """Return visible Simplify jobs whose official URLs may reveal an ATS board."""
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                """SELECT DISTINCT company, url FROM jobs
                   WHERE open=1 AND category<>''
                     AND discovered_source_type='simplify' AND url<>''"""
            )]

    def healthy_generated_companies(self) -> set[str]:
        """Companies whose every generated board is healthy and non-empty."""
        grouped = {}
        with self._connect() as db:
            rows = db.execute(
                """SELECT company, config_json, enabled, status,
                          last_job_count, last_match_count
                   FROM sources WHERE enabled=1"""
            ).fetchall()
        for row in rows:
            try:
                config = json.loads(row["config_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not config.get("generated"):
                continue
            company = str(row["company"] or config.get("company") or "").strip().casefold()
            if company:
                grouped.setdefault(company, []).append(row)
        return {
            company for company, company_rows in grouped.items()
            if company_rows and all(
                row["status"] == "healthy"
                and row["last_job_count"] is not None
                and int(row["last_job_count"]) > 0
                and row["last_match_count"] is not None
                and int(row["last_match_count"]) > 0
                for row in company_rows
            )
        }

    def generated_company_ready(self, company: str) -> bool:
        return str(company or "").strip().casefold() in self.healthy_generated_companies()

    def deactivate_fallback_company(self, company: str, now: int | None = None) -> int:
        """Hide duplicate Simplify observations once official boards are healthy."""
        normalized = str(company or "").strip().casefold()
        if not normalized:
            return 0
        now = int(now or time.time())
        with self._connect() as db:
            affected = [row["job_id"] for row in db.execute(
                """SELECT DISTINCT o.job_id
                   FROM observations o
                   JOIN sources s ON s.source_key=o.source_key
                   JOIN jobs j ON j.id=o.job_id
                   WHERE o.active=1 AND s.source_type='simplify'
                     AND lower(trim(j.company))=?""",
                (normalized,),
            )]
            if not affected:
                return 0
            marks = ",".join("?" for _ in affected)
            db.execute(
                f"""UPDATE observations SET active=0, last_seen=?
                    WHERE source_key IN (
                      SELECT source_key FROM sources WHERE source_type='simplify'
                    ) AND job_id IN ({marks})""",
                (now, *affected),
            )
            changed = int(db.execute("SELECT changes()").fetchone()[0])
            self._refresh_open(db, affected)
            return changed

    @staticmethod
    def _refresh_open(db, job_ids: Iterable[str]) -> None:
        for jid in set(job_ids):
            db.execute(
                """UPDATE jobs
                   SET open=CASE WHEN EXISTS(
                     SELECT 1 FROM observations o WHERE o.job_id=jobs.id AND o.active=1
                   ) THEN 1 ELSE 0 END
                   WHERE id=? AND managed=1""",
                (jid,),
            )

    def record_poll(self, results: list[dict], enqueue_notifications: bool,
                    now: int | None = None) -> list[dict]:
        """Persist one poll atomically and return genuinely new matching jobs.

        Failed sources never alter observations. A formerly non-empty source
        must return empty three consecutive times before its empty snapshot is
        trusted, protecting the dashboard from transient blocks/schema errors.
        """
        now = int(now or time.time())
        fresh: list[dict] = []
        with self._connect() as db:
            for result in results:
                key = result["source_key"]
                row = db.execute(
                    """SELECT last_job_count, consecutive_empty,
                              consecutive_failures, last_success_at
                       FROM sources WHERE source_key=?""",
                    (key,),
                ).fetchone()
                if not result.get("ok"):
                    failures = int(row["consecutive_failures"] or 0) + 1 if row else 1
                    previously_healthy = bool(row and row["last_success_at"])
                    status = (
                        "healthy" if previously_healthy
                        and failures < SOURCE_FAILURE_THRESHOLD else "degraded"
                    )
                    db.execute(
                        """UPDATE sources SET status=?, last_attempt_at=?,
                           last_error=?, consecutive_failures=?,
                           duration_ms=? WHERE source_key=?""",
                        (status, now,
                         str(result.get("error") or "unknown fetch error")[:1000],
                         failures, int(result.get("duration_ms") or 0), key),
                    )
                    continue

                raw_count = int(result.get("raw_count", len(result.get("jobs") or [])))
                previous_count = row["last_job_count"] if row else None
                empty_count = int(row["consecutive_empty"] or 0) if row else 0
                suspicious_empty = raw_count == 0 and previous_count not in (None, 0)
                if suspicious_empty and empty_count < 2:
                    db.execute(
                        """UPDATE sources SET status='suspect_empty', last_attempt_at=?,
                           last_error='Unexpected empty result; preserving prior snapshot',
                           consecutive_empty=consecutive_empty+1, duration_ms=?
                           WHERE source_key=?""",
                        (now, int(result.get("duration_ms") or 0), key),
                    )
                    continue

                hits = result.get("hits") or []
                db.execute(
                    """UPDATE sources SET status='healthy', last_attempt_at=?,
                       last_success_at=?, last_error=NULL, consecutive_failures=0,
                       consecutive_empty=0, last_job_count=?, last_match_count=?,
                       duration_ms=? WHERE source_key=?""",
                    (now, now, raw_count, len(hits), int(result.get("duration_ms") or 0), key),
                )

                affected = [r[0] for r in db.execute(
                    "SELECT job_id FROM observations WHERE source_key=? AND active=1", (key,)
                )]
                db.execute("UPDATE observations SET active=0 WHERE source_key=?", (key,))

                for job in hits:
                    jid = str(job["id"])
                    # Aggregators use their own IDs, while Apple's direct API
                    # uses the Apple position ID. Join observations by the
                    # stable Apple details URL so promoting Apple does not
                    # create a duplicate card or notification.
                    apple_url = re.match(
                        r"https?://jobs\.apple\.com/[^/]+/details/([^/?#]+)",
                        str(job.get("url") or ""), re.I,
                    )
                    if apple_url:
                        prefix = f"%/details/{apple_url.group(1).split('-')[0]}%"
                        alias = db.execute(
                            """SELECT id FROM jobs
                               WHERE lower(trim(company))=lower(trim(?)) AND url LIKE ?
                               ORDER BY open DESC, last_seen DESC LIMIT 1""",
                            (str(job.get("company") or ""), prefix),
                        ).fetchone()
                        if alias:
                            jid = str(alias["id"])
                    existed = db.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone() is not None
                    resolved = int(bool(job.get("_date_resolved")))
                    if not existed:
                        db.execute(
                            """INSERT INTO jobs
                               (id, company, title, location, url, posted, first_seen,
                                last_seen, open, managed, discovered_source_key,
                                discovered_source_type, discovered_source_label,
                                date_resolved, category, classification_reason)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?, ?, ?)""",
                            (jid, str(job.get("company") or ""), str(job.get("title") or ""),
                             str(job.get("location") or ""), str(job.get("url") or ""),
                             str(job.get("posted") or ""), now, now, key,
                             str(result.get("source_type") or "unknown"),
                             str(result.get("source_label") or ""), resolved,
                             str(job.get("_category") or ""),
                             str(job.get("_classification_reason") or "")),
                        )
                        fresh.append(dict(job))
                        if enqueue_notifications:
                            db.execute(
                                """INSERT OR IGNORE INTO outbox
                                   (job_id, channel, status, attempts, next_attempt_at, created_at)
                                   VALUES (?, 'discord', 'pending', 0, ?, ?)""",
                                (jid, now, now),
                            )
                    else:
                        db.execute(
                            """UPDATE jobs SET company=?, title=?, location=?, url=?,
                               posted=CASE WHEN date_resolved=1 AND ?=0 AND ?='simplify'
                                           THEN posted ELSE ? END,
                               date_resolved=CASE WHEN date_resolved=1 OR ?=1 THEN 1 ELSE 0 END,
                               discovered_source_key=CASE
                                 WHEN managed=0 OR (discovered_source_type='simplify' AND ?<>'simplify')
                                 THEN ? ELSE discovered_source_key END,
                               discovered_source_type=CASE
                                 WHEN managed=0 OR (discovered_source_type='simplify' AND ?<>'simplify')
                                 THEN ? ELSE discovered_source_type END,
                               discovered_source_label=CASE
                                 WHEN managed=0 OR (discovered_source_type='simplify' AND ?<>'simplify')
                                 THEN ? ELSE discovered_source_label END,
                               category=?, classification_reason=?, last_seen=?, managed=1
                               WHERE id=?""",
                            (str(job.get("company") or ""), str(job.get("title") or ""),
                             str(job.get("location") or ""), str(job.get("url") or ""),
                             resolved, str(result.get("source_type") or "unknown"),
                             str(job.get("posted") or ""), resolved,
                             str(result.get("source_type") or "unknown"), key,
                             str(result.get("source_type") or "unknown"),
                             str(result.get("source_type") or "unknown"),
                             str(result.get("source_type") or "unknown"),
                             str(result.get("source_label") or ""),
                             str(job.get("_category") or ""),
                             str(job.get("_classification_reason") or ""), now, jid),
                        )

                    db.execute(
                        """INSERT INTO observations(job_id, source_key, first_seen, last_seen, active)
                           VALUES (?, ?, ?, ?, 1)
                           ON CONFLICT(job_id, source_key) DO UPDATE SET
                             last_seen=excluded.last_seen, active=1""",
                        (jid, key, now, now),
                    )
                    affected.append(jid)
                self._refresh_open(db, affected)
        return fresh

    def dashboard_state(self) -> dict:
        with self._connect() as db:
            rows = db.execute(
                """SELECT j.*,
                   (SELECT status FROM outbox o WHERE o.job_id=j.id AND o.channel='discord')
                     AS alert_status,
                   (SELECT sent_at FROM outbox o WHERE o.job_id=j.id AND o.channel='discord')
                     AS alerted_at
                   FROM jobs j"""
            ).fetchall()
        state = {}
        for r in rows:
            d = dict(r)
            state[d["id"]] = {
                "company": d["company"], "title": d["title"],
                "location": d["location"], "url": d["url"],
                "posted": d["posted"], "first_seen": d["first_seen"],
                "last_seen": d["last_seen"], "open": bool(d["open"]),
                "_date_resolved": bool(d["date_resolved"]),
                "_source_type": d["discovered_source_type"],
                "_source_label": d["discovered_source_label"],
                "alert_status": d["alert_status"], "alerted_at": d["alerted_at"],
                "category": d["category"],
                "classification_reason": d["classification_reason"],
            }
        return state

    def source_health(self) -> list[dict]:
        with self._connect() as db:
            return [dict(r) for r in db.execute(
                """SELECT source_key, source_type, company, label, status,
                          last_attempt_at, last_success_at, last_error,
                          consecutive_failures, last_job_count, last_match_count,
                          duration_ms
                   FROM sources WHERE enabled=1
                   ORDER BY CASE status WHEN 'degraded' THEN 0 WHEN 'suspect_empty' THEN 1
                            WHEN 'pending' THEN 2 ELSE 3 END, label"""
            )]

    def outbox_counts(self) -> dict[str, int]:
        with self._connect() as db:
            return {r["status"]: int(r["n"]) for r in db.execute(
                "SELECT status, COUNT(*) AS n FROM outbox GROUP BY status"
            )}

    def due_discord(self, limit: int = 50, now: int | None = None) -> list[dict]:
        now = int(now or time.time())
        with self._connect() as db:
            return [dict(r) for r in db.execute(
                """SELECT o.id AS outbox_id, o.attempts, j.*
                   FROM outbox o JOIN jobs j ON j.id=o.job_id
                   WHERE o.channel='discord' AND o.status IN ('pending','retry')
                     AND o.next_attempt_at<=?
                   ORDER BY o.created_at, o.id LIMIT ?""",
                (now, limit),
            )]

    def mark_outbox_sent(self, ids: Iterable[int], remote_id: str | None = None,
                         now: int | None = None) -> None:
        now = int(now or time.time())
        with self._connect() as db:
            db.executemany(
                """UPDATE outbox SET status='sent', attempts=attempts+1,
                   sent_at=?, last_error=NULL, remote_id=? WHERE id=?""",
                [(now, remote_id, int(i)) for i in ids],
            )

    def mark_outbox_failed(self, ids: Iterable[int], error: str,
                           retry_after: int | None = None,
                           now: int | None = None) -> None:
        now = int(now or time.time())
        ids = [int(i) for i in ids]
        with self._connect() as db:
            for oid in ids:
                row = db.execute("SELECT attempts FROM outbox WHERE id=?", (oid,)).fetchone()
                attempts = int(row["attempts"] if row else 0) + 1
                delay = retry_after if retry_after is not None else min(3600, 30 * (2 ** min(attempts - 1, 7)))
                db.execute(
                    """UPDATE outbox SET status='retry', attempts=?, next_attempt_at=?,
                       last_error=? WHERE id=?""",
                    (attempts, now + max(1, int(delay)), str(error)[:1000], oid),
                )

    def export_legacy(self, path: str | Path, exclude_pending: bool = True) -> None:
        """Write state.json for compatibility with the ephemeral Actions runner.

        Unsent new jobs are omitted so an ephemeral next run detects and retries
        them instead of silently treating them as already delivered.
        """
        state = self.dashboard_state()
        if exclude_pending:
            state = {jid: rec for jid, rec in state.items()
                     if rec.get("alert_status") not in ("pending", "retry")}
        clean = {}
        for jid, rec in state.items():
            clean[jid] = {k: v for k, v in rec.items()
                          if k not in ("_source_type", "_source_label", "alert_status", "alerted_at")}
        path = Path(path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(clean, separators=(",", ":")))
        tmp.replace(path)
