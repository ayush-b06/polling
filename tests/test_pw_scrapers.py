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


if __name__ == "__main__":
    unittest.main()
