import unittest
from pathlib import Path

import yaml

import main


ROOT = Path(__file__).resolve().parents[1]


class StrictClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
        cls.filters = main.compile_filters(cls.cfg)

    def matches(self, title, description="", cohort=None, url=""):
        job = {
            "title": title,
            "description": description,
            "location": "Austin, TX",
            "posted": "",
            "url": url,
        }
        if cohort:
            job["_cohort"] = cohort
        return main.matches(job, self.filters), job

    def test_accepts_only_the_four_target_categories(self):
        cases = [
            ("Software Engineer Intern", None, "software engineering internship"),
            ("Quantitative Software Developer Intern", None, "quant developer internship"),
            ("New Grad Software Engineer", None, "software engineering new-grad full-time"),
            ("Quant Developer - University Graduate", None, "quant developer new-grad full-time"),
        ]
        for title, cohort, category in cases:
            with self.subTest(title=title):
                kept, job = self.matches(title, cohort=cohort)
                self.assertTrue(kept)
                self.assertEqual(job["_category"], category)

    def test_null_ats_scalar_fields_do_not_abort_scan(self):
        job = {
            "title": "Software Engineer Intern",
            "description": "",
            "location": None,
            "posted": "",
            "url": "https://example.com/jobs/1",
        }
        self.assertFalse(main.matches(job, self.filters))
        self.assertEqual(job["location"], "")

    def test_rejects_adjacent_but_out_of_scope_roles(self):
        for title in [
            "Data Engineer Intern", "Machine Learning Research Intern",
            "Site Reliability Engineer Intern", "Quantitative Research Intern",
            "DevOps Engineer - New Grad", "Platform Engineer Intern",
        ]:
            with self.subTest(title=title):
                self.assertFalse(self.matches(title)[0])

    def test_generic_software_title_passes_unless_disqualified(self):
        kept, job = self.matches("Software Engineer")
        self.assertTrue(kept)
        self.assertIn("no disqualifying evidence", job["_classification_reason"])
        kept, job = self.matches(
            "Software Engineer",
            "This university hire is open to candidates with 0 to 2 years of experience.",
        )
        self.assertTrue(kept)
        self.assertIn("description", job["_classification_reason"])
        self.assertTrue(self.matches("Software Engineer", cohort="newgrad")[0])
        self.assertTrue(self.matches("Software Engineer", cohort="internship")[0])

    def test_honeywell_software_engr_abbreviation_is_classified(self):
        self.assertTrue(self.matches(
            "Software Engr I", "0-2 years of experience in software development."
        )[0])
        self.assertFalse(self.matches("Software Engr II")[0])
        self.assertFalse(self.matches(
            "Advanced Software Engr",
            "Experience must involve five (5) years in software testing.",
        )[0])

    def test_vanguard_application_roles_require_trusted_early_career_evidence(self):
        self.assertFalse(self.matches("Application Engineer")[0])
        self.assertTrue(self.matches(
            "Entry Level Application Engineer - 2027 Start Date",
            cohort="newgrad",
        )[0])
        self.assertTrue(self.matches(
            "College to Corporate Internship - Application Development (PA)",
            cohort="internship",
        )[0])
        self.assertTrue(self.matches(
            "Technology Leadership Program - Application Development (TX)",
            cohort="newgrad",
        )[0])
        self.assertFalse(self.matches(
            "Senior Application Engineer", cohort="newgrad",
        )[0])

    def test_generic_title_accepts_experience_range_with_zero_minimum(self):
        kept, job = self.matches(
            "Software Engineer",
            "We'd love to hear from people with 0–4 years of professional "
            "industry experience with software development.",
        )
        self.assertTrue(kept)
        self.assertIn("0–4 years", job["_classification_reason"])

    def test_generic_title_rejects_experienced_minimum(self):
        descriptions = [
            "Minimum of 3 years of professional software experience.",
            "Master's degree plus two years of experience in a related occupation.",
            "At least 2 years of relevant engineering experience is required.",
            "Requires 5+ years of hands-on software experience.",
            "You have 2-4 years of experience building production systems.",
            "This is a mid-level position.",
            "This is a senior, foundational role on a new team.",
            "We're looking for experienced engineers who own outcomes end-to-end.",
        ]
        for description in descriptions:
            with self.subTest(description=description):
                self.assertFalse(self.matches("Software Engineer", description)[0])

    def test_generic_title_rejects_senior_title_evidence(self):
        titles = [
            "Staff+ Software Engineer",
            "Software Engineer, Infrastructure (Staff)",
            "Software Developer-Mid Career",
            "Founding Software Engineer",
            "Prinicipal Software Engineer",
            "Software Developer II",
            "Software Engineer Level 3 or 4",
            "Specialist, Software Engineering",
            "Software Engineer (SME)",
        ]
        for title in titles:
            with self.subTest(title=title):
                self.assertFalse(self.matches(title)[0])

    def test_one_year_minimum_still_passes(self):
        self.assertTrue(self.matches(
            "Software Engineer",
            "One year of experience in the job offered or a related occupation.",
        )[0])

    def test_experienced_url_slug_disqualifies_generic_title(self):
        urls = [
            "https://example.com/jobs/experienced-software-engineer-python/",
            "https://example.com/job/Analytics-Sr-Software-Engineer_JR1",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertFalse(self.matches(
                    "Software Engineer - Python", url=url,
                )[0])

    def test_paypal_degree_only_requirements_are_kept(self):
        for description in [
            "Minimum Requirements: Bachelor's degree in Computer Science.",
            "Minimum Requirements: Master's degree in Computer Science.",
        ]:
            with self.subTest(description=description):
                self.assertTrue(self.matches("Software Engineer", description)[0])

    def test_experience_and_non_full_time_evidence_override_new_grad(self):
        self.assertFalse(self.matches(
            "New Grad Software Engineer", "Minimum of 3 years of engineering experience"
        )[0])
        self.assertFalse(self.matches(
            "Entry-Level Software Engineer", "This is a part-time contract position"
        )[0])


class SchedulerConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = yaml.safe_load((ROOT / "config.yaml").read_text())

    def test_adapter_buckets_have_independent_cadences(self):
        self.assertEqual(main.source_interval({"type": "greenhouse"}, self.cfg), 60)
        self.assertEqual(main.source_interval({"type": "workday"}, self.cfg), 120)
        self.assertEqual(main.source_interval({"type": "pw_meta"}, self.cfg), 180)
        self.assertEqual(main.source_interval({"type": "simplify"}, self.cfg), 900)
        self.assertEqual(main.source_interval(
            {"type": "greenhouse", "interval_seconds": 20}, self.cfg
        ), 20)

    def test_duplicate_sources_are_polled_once(self):
        prepared = main.prepare_sources(self.cfg)
        keys = [src["_source_key"] for src in prepared]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertLess(len(prepared), len(self.cfg["sources"]))

    def test_network_failure_detection_only_matches_transport_errors(self):
        self.assertTrue(main.is_network_failure({
            "ok": False, "error": "ConnectError: [Errno 8] nodename nor servname"
        }))
        self.assertTrue(main.is_network_failure({
            "ok": False, "error": "ConnectTimeout:"
        }))
        self.assertFalse(main.is_network_failure({
            "ok": False, "error": "HTTPStatusError: 404 Not Found"
        }))
        self.assertFalse(main.is_network_failure({"ok": True, "error": ""}))


if __name__ == "__main__":
    unittest.main()
