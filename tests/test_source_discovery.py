import unittest

import source_discovery
import storage


class DirectSourceInferenceTests(unittest.TestCase):
    def test_infers_supported_official_boards(self):
        cases = [
            (
                "Acme", "https://job-boards.greenhouse.io/acme/jobs/123",
                {"type": "greenhouse", "token": "acme"},
            ),
            (
                "Acme", "https://jobs.ashbyhq.com/acme/abc-def",
                {"type": "ashby", "token": "acme"},
            ),
            (
                "Acme", "https://jobs.lever.co/acme/abc-def",
                {"type": "lever", "token": "acme"},
            ),
            (
                "Acme", "https://apply.workable.com/acme-careers/j/ABC123/",
                {"type": "workable", "token": "acme-careers"},
            ),
            (
                "Acme", "https://osv-acme.wd5.myworkdayjobs.com/en-US/External/job/Austin/job_R1",
                {"type": "workday", "tenant": "osv_acme", "subdomain": "osv-acme",
                 "host": "wd5", "site": "External"},
            ),
            (
                "Acme", "https://acme.eightfold.ai/careers/job/123?domain=acme.com",
                {"type": "eightfold", "subdomain": "acme", "domain": "acme.com"},
            ),
            (
                "Acme", "https://example.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1003/job/123/",
                {"type": "oracle_hcm", "host": "example.fa.us2.oraclecloud.com", "site": "CX_1003"},
            ),
            (
                "Acme", "https://careers-acme.icims.com/jobs/123/software-engineer/job",
                {"type": "icims", "token": "acme"},
            ),
            (
                "Acme", "https://acme.bamboohr.com/careers/42",
                {"type": "bamboohr", "token": "acme"},
            ),
            (
                "Acme", "https://jobs.jobvite.com/acme/job/abc/software-engineer",
                {"type": "jobvite", "token": "acme"},
            ),
            (
                "Acme", "https://acme.pinpointhq.com/en/postings/abc",
                {"type": "pinpoint", "token": "acme"},
            ),
            (
                "Acme", "https://acme.applytojob.com/apply/abc/software-engineer",
                {"type": "jazzhr", "token": "acme"},
            ),
            (
                "Acme", "https://career5.successfactors.eu/career?company=ACME",
                {"type": "successfactors", "token": "ACME"},
            ),
        ]
        for company, url, expected in cases:
            with self.subTest(url=url):
                source = source_discovery.infer_direct_source(company, url)
                self.assertIsNotNone(source)
                self.assertEqual(source["company"], company)
                self.assertTrue(source["generated"])
                for key, value in expected.items():
                    self.assertEqual(source[key], value)

    def test_ignores_unknown_or_incomplete_urls(self):
        self.assertIsNone(source_discovery.infer_direct_source(
            "Acme", "https://careers.acme.com/jobs/123"
        ))
        inferred = source_discovery.infer_direct_source(
            "Acme", "https://acme.eightfold.ai/careers/job/123"
        )
        self.assertEqual(inferred["domain"], "acme.com")

    def test_deduplicates_multiple_jobs_on_one_board(self):
        jobs = [
            {"company": "Acme", "url": "https://jobs.ashbyhq.com/acme/one"},
            {"company": "Acme", "url": "https://jobs.ashbyhq.com/acme/two"},
        ]
        sources = source_discovery.discover_direct_sources(jobs)
        self.assertEqual(len(sources), 1)
        self.assertTrue(storage.source_key(sources[0]).startswith("ashby:"))


if __name__ == "__main__":
    unittest.main()
