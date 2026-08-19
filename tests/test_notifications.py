import tempfile
import time
import unittest
import os
from pathlib import Path
from unittest.mock import patch

import main
import storage


class LocalEnvironmentTests(unittest.TestCase):
    def test_loads_webhook_without_overriding_process_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env.local"
            env_file.write_text(
                "DISCORD_WEBHOOK=https://discord.test/from-file\n"
                "UNRELATED_SECRET=ignored\n"
            )

            with patch.dict(os.environ, {}, clear=True):
                main.load_local_env(env_file)
                self.assertEqual(
                    os.environ["DISCORD_WEBHOOK"],
                    "https://discord.test/from-file",
                )
                self.assertNotIn("UNRELATED_SECRET", os.environ)

            with patch.dict(
                os.environ,
                {"DISCORD_WEBHOOK": "https://discord.test/from-process"},
                clear=True,
            ):
                main.load_local_env(env_file)
                self.assertEqual(
                    os.environ["DISCORD_WEBHOOK"],
                    "https://discord.test/from-process",
                )


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload or {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


class DiscordOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = storage.JobStore(Path(self.tmp.name) / "jobwatch.db")
        self.src = {"type": "greenhouse", "company": "Example", "token": "example"}
        self.src["_source_key"] = storage.source_key(self.src)
        self.store.sync_sources([self.src])
        job = {"id": "new-job", "company": "Example",
               "title": "Software Engineer Intern", "location": "Austin, TX",
               "url": "https://example.com/jobs/new-job", "posted": "2026-08-09"}
        now = int(time.time())
        self.store.record_poll([{
            "source_key": self.src["_source_key"], "source_type": "greenhouse",
            "source_label": "Example", "ok": True, "jobs": [job], "hits": [job],
            "raw_count": 1, "duration_ms": 10,
        }], True, now=now)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_confirmed_webhook_marks_notification_sent(self):
        client = FakeClient(FakeResponse(200, {"id": "message-123"}))
        with patch.dict("os.environ", {"DISCORD_WEBHOOK": "https://discord.test/webhook"}):
            sent = await main.deliver_discord_outbox(client, self.store)
        self.assertEqual(sent, 1)
        self.assertEqual(self.store.outbox_counts(), {"sent": 1})
        _, kwargs = client.calls[0]
        self.assertEqual(kwargs["params"], {"wait": "true"})
        self.assertEqual(kwargs["json"]["allowed_mentions"], {"parse": []})

    async def test_rate_limit_keeps_notification_for_retry(self):
        client = FakeClient(FakeResponse(429, {"retry_after": 2}))
        with patch.dict("os.environ", {"DISCORD_WEBHOOK": "https://discord.test/webhook"}):
            sent = await main.deliver_discord_outbox(client, self.store)
        self.assertEqual(sent, 0)
        self.assertEqual(self.store.outbox_counts(), {"retry": 1})


if __name__ == "__main__":
    unittest.main()
