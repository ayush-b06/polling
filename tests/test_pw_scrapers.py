import asyncio
import json
import unittest

import pw_scrapers


class MetaDetailTests(unittest.TestCase):
    def test_extracts_experience_requirement_from_json_ld(self):
        payload = {
            "@type": "JobPosting",
            "description": "Build distributed systems.",
            "qualifications": (
                "Bachelor's degree or equivalent practical experience&nbsp;"
                "8+ years of experience in systems software engineering"
            ),
        }
        page = (
            '<html><script type="application/ld+json">'
            + json.dumps(payload)
            + '</script></html>'
        )
        description = pw_scrapers._meta_description_from_html(page)
        self.assertIn("8+ years of experience", description)


class SharedBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_callers_share_one_browser_startup(self):
        original_start = pw_scrapers._start_browser
        original_browser = pw_scrapers._BROWSER
        original_pw = pw_scrapers._PW
        original_task = pw_scrapers._BROWSER_START_TASK
        starts = 0
        fake_playwright = object()
        fake_browser = object()

        async def fake_start():
            nonlocal starts
            starts += 1
            await asyncio.sleep(0)
            return fake_playwright, fake_browser

        try:
            pw_scrapers._start_browser = fake_start
            pw_scrapers._BROWSER = None
            pw_scrapers._PW = None
            pw_scrapers._BROWSER_START_TASK = None
            browsers = await asyncio.gather(
                *(pw_scrapers._get_browser() for _ in range(6))
            )
            self.assertEqual(starts, 1)
            self.assertEqual(browsers, [fake_browser] * 6)
        finally:
            pw_scrapers._start_browser = original_start
            pw_scrapers._BROWSER = original_browser
            pw_scrapers._PW = original_pw
            pw_scrapers._BROWSER_START_TASK = original_task


if __name__ == "__main__":
    unittest.main()
