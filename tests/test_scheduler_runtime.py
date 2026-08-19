import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import adapters
import main
import storage
import yaml


ROOT = Path(__file__).resolve().parents[1]


class SchedulerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_promoted_snapshot_is_seeded_without_duplicate_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = storage.JobStore(root / "jobwatch.db")
            fallback = {"type": "simplify", "feed": "newgrad"}
            fallback["_source_key"] = storage.source_key(fallback)
            fallback["_source_label"] = storage.source_label(fallback)
            store.sync_sources([fallback])
            fallback_job = {
                "id": "fallback-role", "company": "Example",
                "title": "New Grad Software Engineer", "location": "Austin, TX",
                "url": "https://jobs.ashbyhq.com/example/role-1",
                "posted": "2026-08-09",
                "_category": "software engineering new-grad full-time",
            }
            store.record_poll([{
                "source_key": fallback["_source_key"], "source_type": "simplify",
                "source_label": fallback["_source_label"], "ok": True,
                "jobs": [fallback_job], "hits": [fallback_job], "raw_count": 1,
                "duration_ms": 1,
            }], False, now=100)

            cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
            cfg["sources"] = []

            async def fake_ashby(client, company, token, **kwargs):
                return [{
                    "id": "direct-role", "company": company,
                    "title": "New Grad Software Engineer", "location": "Austin, TX",
                    "url": "https://jobs.ashbyhq.com/example/role-1",
                    "posted": "2026-08-09", "description": "",
                }]

            with patch.object(main, "STATE_FILE", root / "state.json"), \
                 patch.object(main.dashboard, "write"), \
                 patch.dict(adapters.REGISTRY, {"ashby": fake_ashby}), \
                 patch.dict(os.environ, {}, clear=True):
                with contextlib.redirect_stdout(io.StringIO()), \
                     contextlib.redirect_stderr(io.StringIO()):
                    await main.poll_once(cfg, store=store)

            self.assertTrue(store.dashboard_state()["direct-role"]["open"])
            self.assertEqual(store.outbox_counts(), {})

    async def test_completed_source_is_persisted_without_waiting_for_a_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "jobwatch.db"
            state_path = root / "state.json"
            state_path.write_text(json.dumps({
                "baseline": {"first_seen": 1, "open": False}
            }))
            cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
            cfg["sources"] = [{
                "type": "greenhouse", "company": "Example", "token": "example",
                "interval_seconds": 15,
            }]
            cfg["initial_spread_seconds"] = 0
            stop = asyncio.Event()

            async def fake_adapter(client, company, token, **kwargs):
                stop.set()
                return [{
                    "id": "scheduled-job", "company": company,
                    "title": "Software Engineer Intern", "location": "Austin, TX",
                    "url": "https://example.com/jobs/scheduled-job", "posted": "",
                    "description": "",
                }]

            with patch.object(main, "DB_FILE", db_path), \
                 patch.object(main, "STATE_FILE", state_path), \
                 patch.object(main.dashboard, "write"), \
                 patch.dict(adapters.REGISTRY, {"greenhouse": fake_adapter}), \
                 patch.dict(os.environ, {}, clear=True):
                with contextlib.redirect_stdout(io.StringIO()), \
                     contextlib.redirect_stderr(io.StringIO()):
                    await asyncio.wait_for(main.run_scheduler(cfg, stop_event=stop), timeout=2)

            store = storage.JobStore(db_path)
            saved = store.dashboard_state()["scheduled-job"]
            self.assertTrue(saved["open"])
            self.assertEqual(saved["category"], "software engineering internship")
            self.assertEqual(store.outbox_counts(), {"pending": 1})


if __name__ == "__main__":
    unittest.main()
