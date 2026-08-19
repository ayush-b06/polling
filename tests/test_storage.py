import json
import tempfile
import time
import unittest
from pathlib import Path

import dashboard
import storage


def source(company="Example"):
    src = {"type": "greenhouse", "company": company, "token": company.lower()}
    src["_source_key"] = storage.source_key(src)
    src["_source_label"] = storage.source_label(src)
    return src


def job(jid="job-1"):
    return {
        "id": jid,
        "company": "Example",
        "title": "Software Engineer Intern",
        "location": "San Francisco, CA",
        "url": f"https://example.com/jobs/{jid}",
        "posted": "2026-08-09",
    }


def result(src, jobs=None, ok=True, error=None):
    jobs = list(jobs or [])
    return {
        "source_key": src["_source_key"],
        "source_type": src["type"],
        "source_label": src["_source_label"],
        "ok": ok,
        "error": error,
        "jobs": jobs,
        "hits": jobs,
        "raw_count": len(jobs),
        "duration_ms": 25,
    }


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = storage.JobStore(self.root / "jobwatch.db")
        self.src = source()
        self.store.sync_sources([self.src])

    def tearDown(self):
        self.tmp.cleanup()

    def test_failed_source_preserves_open_jobs(self):
        fresh = self.store.record_poll([result(self.src, [job()])], False, now=100)
        self.assertEqual([j["id"] for j in fresh], ["job-1"])
        for attempt, now in enumerate((200, 300, 400), start=1):
            self.store.record_poll(
                [result(self.src, ok=False, error="HTTP 503")], False, now=now
            )
            health = self.store.source_health()[0]
            self.assertEqual(
                health["status"], "degraded" if attempt == 3 else "healthy"
            )
        self.assertTrue(self.store.dashboard_state()["job-1"]["open"])
        health = self.store.source_health()[0]
        self.assertEqual(health["status"], "degraded")
        self.assertEqual(health["consecutive_failures"], 3)
        self.assertIn("503", health["last_error"])

    def test_unexpected_empty_requires_three_polls_before_closing(self):
        self.store.record_poll([result(self.src, [job()])], False, now=100)
        self.store.record_poll([result(self.src, [])], False, now=200)
        self.store.record_poll([result(self.src, [])], False, now=300)
        self.assertTrue(self.store.dashboard_state()["job-1"]["open"])
        self.store.record_poll([result(self.src, [])], False, now=400)
        self.assertFalse(self.store.dashboard_state()["job-1"]["open"])

    def test_outbox_retries_and_records_delivery(self):
        self.store.record_poll([result(self.src, [job()])], True, now=100)
        due = self.store.due_discord(now=100)
        self.assertEqual(len(due), 1)
        oid = due[0]["outbox_id"]
        self.store.mark_outbox_failed([oid], "rate limited", retry_after=5, now=100)
        self.assertEqual(self.store.due_discord(now=104), [])
        self.assertEqual(len(self.store.due_discord(now=105)), 1)
        self.store.mark_outbox_sent([oid], remote_id="discord-message", now=106)
        self.assertEqual(self.store.outbox_counts(), {"sent": 1})
        self.assertEqual(self.store.dashboard_state()["job-1"]["alert_status"], "sent")

    def test_legacy_import_does_not_enqueue_notifications(self):
        legacy = {
            "old-job": {
                "company": "Old Co", "title": "Software Engineer Intern",
                "location": "Austin, TX", "url": "https://example.com/old",
                "posted": "2026-08-01", "first_seen": 42, "open": True,
            }
        }
        path = self.root / "state.json"
        path.write_text(json.dumps(legacy))
        other = storage.JobStore(self.root / "legacy.db")
        self.assertEqual(other.import_legacy(path), 1)
        self.assertEqual(other.import_legacy(path), 0)
        self.assertEqual(other.outbox_counts(), {})
        self.assertTrue(other.dashboard_state()["old-job"]["open"])
        legacy_src = source("Old Co")
        other.sync_sources([legacy_src])
        current = dict(job("old-job"), company="Old Co")
        other.record_poll([result(legacy_src, [current])], True, now=100)
        migrated = other.dashboard_state()["old-job"]
        self.assertEqual(migrated["_source_type"], "greenhouse")
        self.assertEqual(other.outbox_counts(), {})

    def test_dashboard_shows_detection_source_health_and_alert_state(self):
        self.store.record_poll([result(self.src, [job()])], True, now=100)
        page = dashboard.build(
            self.store.dashboard_state(), self.store.source_health(),
            self.store.outbox_counts(),
        )
        self.assertIn("newest company-posted first", page)
        self.assertIn("greenhouse", page)
        self.assertIn("Discord pending", page)
        self.assertIn("Company source coverage", page)
        self.assertIn("tracked companies", page)
        self.assertIn("1 eligible open", page)
        self.assertIn("America/Los_Angeles", page)

    def test_healthy_generated_source_replaces_fallback_observation(self):
        fallback = {"type": "simplify", "feed": "newgrad"}
        fallback["_source_key"] = storage.source_key(fallback)
        fallback["_source_label"] = storage.source_label(fallback)
        generated = {
            "type": "ashby", "company": "Example", "token": "example",
            "generated": True, "promoted_from": "simplify",
        }
        generated["_source_key"] = storage.source_key(generated)
        generated["_source_label"] = storage.source_label(generated)
        self.store.sync_sources([fallback, generated])

        fallback_job = dict(job("fallback-job"), company="Example")
        self.store.record_poll([result(fallback, [fallback_job])], False, now=100)
        self.assertFalse(self.store.generated_company_ready("Example"))

        direct_job = dict(job("direct-job"), company="Example")
        self.store.record_poll([result(generated, [direct_job])], False, now=200)
        self.assertTrue(self.store.generated_company_ready("Example"))
        self.assertEqual(len(self.store.generated_sources()), 1)
        self.assertEqual(self.store.deactivate_fallback_company("Example", now=201), 1)

        state = self.store.dashboard_state()
        self.assertFalse(state["fallback-job"]["open"])
        self.assertTrue(state["direct-job"]["open"])

    def test_direct_apple_reuses_fallback_card_without_duplicate_alert(self):
        fallback = {"type": "simplify", "feed": "newgrad"}
        fallback["_source_key"] = storage.source_key(fallback)
        fallback["_source_label"] = storage.source_label(fallback)
        direct = {"type": "apple", "company": "Apple", "query": "early career"}
        direct["_source_key"] = storage.source_key(direct)
        direct["_source_label"] = storage.source_label(direct)
        self.store.sync_sources([fallback, direct])

        fallback_job = dict(
            job("simplify-apple-id"), company="Apple",
            title="Software Engineer - Early Career",
            url="https://jobs.apple.com/en-us/details/200677377",
            posted="2026-08-11T22:14:03Z", _date_resolved=True,
        )
        self.store.record_poll([result(fallback, [fallback_job])], True, now=100)

        direct_job = dict(
            job("direct-apple-id"), company="Apple",
            title="Software Engineer, IS&T Early Career Opportunities",
            location="Austin",
            url=("https://jobs.apple.com/en-us/details/200677377/"
                 "software-engineer-is-t-early-career-opportunities"),
            posted="2026-08-11T19:27:15Z",
        )
        fresh = self.store.record_poll([result(direct, [direct_job])], True, now=200)

        self.assertEqual(fresh, [])
        self.assertEqual(self.store.outbox_counts(), {"pending": 1})
        state = self.store.dashboard_state()
        self.assertNotIn("direct-apple-id", state)
        merged = state["simplify-apple-id"]
        self.assertEqual(merged["posted"], "2026-08-11T19:27:15Z")
        self.assertEqual(merged["_source_type"], "apple")
        self.assertIn("IS&T", merged["title"])

    def test_dashboard_orders_by_company_posted_date_then_detection(self):
        now = int(time.time())
        state = {
            "older": dict(job("older"), title="Older dated", posted="2026-08-01",
                          first_seen=now - 5),
            "newer": dict(job("newer"), title="Newer dated", posted="2026-08-08",
                          first_seen=now - 500),
            "unknown-old": dict(job("unknown-old"), title="Unknown detected earlier",
                                posted="", first_seen=now - 100),
            "unknown-new": dict(job("unknown-new"), title="Unknown detected later",
                                posted="", first_seen=now - 10),
            "same-day-early": dict(
                job("same-day-early"), title="Same day early",
                posted="2026-08-08T09:00:00Z", first_seen=now - 1,
            ),
            "same-day-late": dict(
                job("same-day-late"), title="Same day late",
                posted="2026-08-08T18:00:00Z", first_seen=now - 1000,
            ),
        }

        page = dashboard.build(state)
        self.assertLess(page.index("Same day late"), page.index("Same day early"))
        self.assertLess(page.index("Same day early"), page.index("Newer dated"))
        self.assertLess(page.index("Newer dated"), page.index("Older dated"))
        self.assertLess(page.index("Older dated"), page.index("Unknown detected later"))
        self.assertLess(
            page.index("Unknown detected later"),
            page.index("Unknown detected earlier"),
        )
        self.assertIn('value="posted">Newest posted', page)

    def test_dashboard_reapplies_saved_sort_after_auto_refresh(self):
        page = dashboard.build({"job-1": job("job-1")})
        self.assertIn("localStorage.setItem('jw_sort', $('#sort').value)", page)
        self.assertIn("window.addEventListener('pageshow'", page)
        self.assertIn("savedSort === 'detected' ? 'detected' : 'posted'", page)


if __name__ == "__main__":
    unittest.main()
