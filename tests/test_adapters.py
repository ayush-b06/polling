import unittest

import adapters


class FakeResponse:
    def __init__(self, payload=None, text="", headers=None):
        self.payload = payload or {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class FakeEightfoldClient:
    async def get(self, url, **kwargs):
        if url.endswith("/api/pcsx/search"):
            return FakeResponse({"data": {"count": 1, "positions": [{
                "id": "role-1",
                "name": "Software Engineer",
                "locations": ["Seattle, Washington, United States"],
                "positionUrl": "/careers/job/role-1",
                "postedTs": 1786131045000,
            }]}})
        return FakeResponse(text=(
            '<script>{"jobDescription":"Candidates may have '
            '0\\u20134 years of professional experience."}</script>'
        ))


class FakeL3Client:
    async def get(self, url, **kwargs):
        if "tile-search-results" in url:
            return FakeResponse(text=(
                '<li class="job-tile" data-url="/job/Rochester-Associate-Software-NY/123/">'
                '<a class="jobTitle-link">Associate, Software Engineering</a>'
                '<div class="section-field location"><div id="x-section-location-value">'
                'Rochester, NY, US</div></div></li>'
            ))
        return FakeResponse(text=(
            '<meta itemprop="datePosted" content="Mon Aug 10 00:00:00 UTC 2026">'
        ))


class FakeWorkdayClient:
    async def post(self, url, **kwargs):
        return FakeResponse({"total": 1, "jobPostings": [{
            "title": "Software Engineer",
            "locationsText": "Austin, TX",
            "externalPath": "/job/Austin/Software-Engineer_R1",
            "postedOn": "Posted Today",
        }]})

    async def get(self, url, **kwargs):
        return FakeResponse({"jobPostingInfo": {
            "jobDescription": "Requires 5+ years of software engineering experience."
        }})


class FakeSmartRecruitersClient:
    async def get(self, url, **kwargs):
        if url.endswith("/postings?limit=100"):
            return FakeResponse({"content": [{
                "id": "role-1", "name": "Software Engineer",
                "location": {"city": "Austin", "region": "TX"},
            }]})
        return FakeResponse({"jobAd": {"sections": {"qualifications": {
            "text": "At least 3 years of relevant engineering experience."
        }}}})


class FakeLinkedInClient:
    async def get(self, url, **kwargs):
        if "seeMoreJobPostings" in url:
            start = int((kwargs.get("params") or {}).get("start", 0))
            if start:
                return FakeResponse(text="")
            return FakeResponse(text='''
                <li><div data-entity-urn="urn:li:jobPosting:12345">
                  <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/example-12345?position=1"></a>
                  <h3 class="base-search-card__title">Software Engineer</h3>
                  <span class="job-search-card__location">Sunnyvale, CA</span>
                  <time datetime="2026-08-10"></time>
                </div></li>
            ''')
        return FakeResponse(text='''
            <div class="show-more-less-html__markup">
              Requires 5+ years of software engineering experience.
            </div>
        ''')


class FakeAppleClient:
    async def get(self, url, **kwargs):
        return FakeResponse(headers={"x-apple-csrf-token": "test-token"})

    async def post(self, url, **kwargs):
        self.request = kwargs
        page = kwargs["json"]["page"]
        jobs = [] if page > 1 else [{
            "positionId": "200677377",
            "postingTitle": "Software Engineer, IS&T Early Career Opportunities",
            "transformedPostingTitle": "software-engineer-is-t-early-career-opportunities",
            "locations": [{"name": "Austin, Texas, United States"}],
            "postDateInGMT": "2026-08-11T19:27:15.125+00:00",
            "jobSummary": "Build consequential enterprise technology systems.",
        }]
        return FakeResponse({"res": {"searchResults": jobs, "totalRecords": 1}})


class FakeIBMClient:
    def __init__(self):
        self.requests = []

    async def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        filters = kwargs["json"]["post_filter"]["bool"]["must"]
        position_type = filters[0]["term"]["field_keyword_18"]
        offset = kwargs["json"].get("from", 0)
        if position_type == "Internship":
            job_ids = ([f"intern-{index}" for index in range(100)]
                       if offset == 0 else ["intern-100"])
            total = 101
        else:
            job_ids = ["grad-1"]
            total = 1
        return FakeResponse({"hits": {
            "total": {"value": total, "relation": "eq"},
            "hits": [{"_id": f"search-{job_id}", "_source": {
                "title": "Software Engineer" if position_type == "Entry Level"
                         else "Software Engineer Intern",
                "url": f"https://careers.ibm.com/careers/JobDetail?jobId={job_id}",
                "description": "Build cloud software.",
                "field_keyword_05": "United States",
                "field_keyword_08": "Software Engineering",
                "field_keyword_18": position_type,
                "field_keyword_19": "Multiple Cities",
            }} for job_id in job_ids],
        }})


class FakeVanguardClient:
    def __init__(self):
        self.requests = []

    async def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        facet = kwargs["params"]["customAttributeFilter"]
        if 'level="Students"' in facet:
            jobs = [{
                "id": 101,
                "title": "College to Corporate Internship - Application Development (PA)",
                "primary_city": "Malvern",
                "primary_state": "PA",
                "primary_country": "US",
                "url": "http://www.vanguardjobs.com/job/101/example/",
                "open_date": "2026-08-17T00:00:00",
                "description": "Build Java services.",
            }, {
                "id": 102,
                "title": "Data Science Internship",
                "primary_city": "London",
                "primary_country": "GB",
                "url": "http://www.vanguardjobs.com/job/102/example/",
                "open_date": "2026-08-17T00:00:00",
            }]
        else:
            jobs = [{
                "id": 103,
                "title": "Entry Level Application Engineer - 2027 Start Date",
                "primary_city": "Charlotte",
                "primary_state": "NC",
                "primary_country": "US",
                "url": "http://www.vanguardjobs.com/job/103/example/",
                "open_date": "2026-08-18T00:00:00",
                "description": "0 - 3 years of experience.",
            }]
        return FakeResponse({
            "totalHits": len(jobs),
            "searchResults": [{"job": job} for job in jobs],
        })


class FakeOracleHCMClient:
    def __init__(self):
        self.requests = []

    async def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if url.endswith("/recruitingCEJobRequisitions"):
            return FakeResponse({"items": [{
                "TotalJobsCount": 1,
                "requisitionList": [{
                    "Id": "155664",
                    "Title": "Software Engr I",
                    "PostedDate": "2026-08-14",
                    "PrimaryLocationCountry": "US",
                    "PrimaryLocation": "Richmond, VA, United States",
                    "secondaryLocations": [],
                }],
            }]})
        return FakeResponse({"items": [{
            "Id": "155664",
            "Title": "Software Engr I",
            "PrimaryLocation": "Richmond, VA, United States",
            "ExternalPostedStartDate": "2026-08-14T16:06:34+00:00",
            "ExternalDescriptionStr": "Develop production software.",
            "ExternalQualificationsStr": "0-2 years of software experience.",
            "secondaryLocations": [],
        }]})


class FakePagedOracleHCMClient:
    def __init__(self):
        self.offsets = []

    async def get(self, url, **kwargs):
        if url.endswith("/recruitingCEJobRequisitions"):
            finder = kwargs["params"]["finder"]
            offset = int(__import__("re").search(r"offset=(\d+)", finder).group(1))
            self.offsets.append(offset)
            ids = range(offset, min(offset + 100, 101))
            return FakeResponse({"items": [{
                "TotalJobsCount": 101,
                "requisitionList": [{
                    "Id": str(20260000 + index),
                    "Title": "Software Engineer I",
                    "PostedDate": "2026-08-20",
                    "PrimaryLocationCountry": "US",
                    "PrimaryLocation": "Oakland, CA, United States",
                } for index in ids],
            }]})
        req = __import__("re").search(r'Id="([^"]+)', kwargs["params"]["finder"]).group(1)
        return FakeResponse({"items": [{
            "Id": req, "Title": "Software Engineer I",
            "PrimaryLocation": "Oakland, CA, United States",
            "ExternalPostedStartDate": "2026-08-20T16:10:00Z",
            "ExternalQualificationsStr": "0-2 years of experience",
        }]})


class PostedDateNormalizationTests(unittest.TestCase):
    def test_preserves_iso_timestamp_in_utc(self):
        self.assertEqual(
            adapters._iso("2026-08-07T15:30:45-04:00"),
            "2026-08-07T19:30:45Z",
        )

    def test_preserves_epoch_timestamp(self):
        self.assertEqual(
            adapters._iso(1786131045000),
            "2026-08-07T19:30:45Z",
        )

    def test_keeps_day_only_dates_day_only(self):
        self.assertEqual(adapters._iso("2026-08-07"), "2026-08-07")

    def test_extracts_embedded_eightfold_description(self):
        page = '<script>{"jobDescription":"Zero prior experience required."}</script>'
        self.assertEqual(
            adapters._extract_eightfold_description(page),
            "Zero prior experience required.",
        )


class EightfoldDetailTests(unittest.IsolatedAsyncioTestCase):
    async def test_enriches_ambiguous_software_title_from_detail_page(self):
        adapters._EIGHTFOLD_DETAIL_CACHE.clear()
        jobs = await adapters.eightfold(
            FakeEightfoldClient(), "Example", "example", "example.com",
            query="software engineer",
        )
        self.assertEqual(len(jobs), 1)
        self.assertIn("0–4 years", jobs[0]["description"])


class AmbiguousDetailEnrichmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_apple_uses_current_api_and_parses_early_career_role(self):
        client = FakeAppleClient()
        jobs = await adapters.apple(client, query="early career")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], adapters._uid("ap", "200677377"))
        self.assertEqual(jobs[0]["posted"], "2026-08-11T19:27:15Z")
        self.assertIn("Austin", jobs[0]["location"])
        self.assertEqual(client.request["json"]["sort"], "relevance")
        self.assertEqual(
            client.request["headers"]["X-Apple-CSRF-Token"], "test-token"
        )

    async def test_ibm_uses_official_cohorts_and_us_country_filter(self):
        client = FakeIBMClient()
        jobs = await adapters.ibm(client)
        self.assertEqual(len(jobs), 102)
        self.assertEqual({job["_cohort"] for job in jobs}, {"internship", "newgrad"})
        self.assertTrue(all(job["location"].endswith("United States") for job in jobs))
        self.assertTrue(all(job["posted"] == "" for job in jobs))
        newgrad = next(job for job in jobs if job["_cohort"] == "newgrad")
        self.assertIn("Position type: Entry Level", newgrad["description"])
        self.assertEqual(
            client.requests[0][0], "https://www-api.ibm.com/search/api/v2"
        )
        self.assertEqual(
            client.requests[0][1]["json"]["post_filter"]["bool"]["must"][1],
            {"term": {"field_keyword_05": "United States"}},
        )
        self.assertEqual(
            [request[1]["json"]["from"] for request in client.requests],
            [0, 100, 0],
        )

    async def test_vanguard_uses_first_party_level_facets_and_us_filter(self):
        client = FakeVanguardClient()
        jobs = await adapters.vanguard(client)
        self.assertEqual(len(jobs), 2)
        self.assertEqual({job["_cohort"] for job in jobs}, {"internship", "newgrad"})
        self.assertTrue(all(job["location"].endswith("US") for job in jobs))
        self.assertTrue(all(job["url"].startswith("https://www.vanguardjobs.com/")
                            for job in jobs))
        self.assertEqual(jobs[0]["posted"], "2026-08-17")
        self.assertEqual(
            [request[1]["params"]["offset"] for request in client.requests],
            [0, 0],
        )
        self.assertTrue(all(
            'primary_category="Technology"' in
            request[1]["params"]["customAttributeFilter"]
            for request in client.requests
        ))

    async def test_oracle_hcm_uses_company_facets_and_detail_timestamp(self):
        adapters._ORACLE_DETAIL_CACHE.clear()
        client = FakeOracleHCMClient()
        jobs = await adapters.oracle_hcm(
            client,
            company="Honeywell",
            host="ibqbjb.fa.ocs.oraclecloud.com",
            site="CX_1",
            slug="careers.honeywell.com",
            path_site="Honeywell",
            title_facet="124",
            location_facet="300000000469866",
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["title"], "Software Engr I")
        self.assertEqual(jobs[0]["posted"], "2026-08-14T16:06:34Z")
        self.assertIn("0-2 years", jobs[0]["description"])
        self.assertEqual(
            jobs[0]["url"],
            "https://careers.honeywell.com/en/sites/Honeywell/job/155664/",
        )
        finder = client.requests[0][1]["params"]["finder"]
        self.assertIn("selectedTitlesFacet=124", finder)
        self.assertIn("selectedLocationsFacet=300000000469866", finder)

    async def test_workday_enriches_generic_software_title(self):
        adapters._WORKDAY_DETAIL_CACHE.clear()
        jobs = await adapters.workday(
            FakeWorkdayClient(), "Example", "example", "External", "wd1",
        )
        self.assertIn("5+ years", jobs[0]["description"])

    async def test_oracle_hcm_paginates_and_keeps_exact_blue_shield_time(self):
        adapters._ORACLE_DETAIL_CACHE.clear()
        client = FakePagedOracleHCMClient()
        jobs = await adapters.oracle_hcm(
            client, company="Blue Shield of California",
            host="ecge.fa.us2.oraclecloud.com", site="CX_1003",
            slug="ecge.fa.us2.oraclecloud.com", path_site="CX_1003",
        )
        self.assertEqual(len(jobs), 101)
        self.assertEqual(client.offsets, [0, 100])
        self.assertEqual(jobs[0]["posted"], "2026-08-20T16:10:00Z")
        self.assertIn("/hcmUI/CandidateExperience/en/sites/CX_1003/job/", jobs[0]["url"])
        self.assertNotIn("_detail_url", jobs[0])

    async def test_smartrecruiters_enriches_generic_software_title(self):
        adapters._SMARTRECRUITERS_DETAIL_CACHE.clear()
        jobs = await adapters.smartrecruiters(
            FakeSmartRecruitersClient(), "Example", "example",
        )
        self.assertIn("At least 3 years", jobs[0]["description"])
        self.assertNotIn("_detail_url", jobs[0])

    async def test_linkedin_uses_official_company_jobs_and_details(self):
        adapters._LINKEDIN_DETAIL_CACHE.clear()
        jobs = await adapters.linkedin_company(FakeLinkedInClient())
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["company"], "LinkedIn")
        self.assertEqual(jobs[0]["posted"], "2026-08-10")
        self.assertIn("5+ years", jobs[0]["description"])


class L3HarrisDateTests(unittest.IsolatedAsyncioTestCase):
    async def test_enriches_target_role_with_official_detail_date(self):
        adapters._L3_DATE_CACHE.clear()
        jobs = await adapters.l3harris(FakeL3Client())
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["posted"], "2026-08-10")


if __name__ == "__main__":
    unittest.main()
