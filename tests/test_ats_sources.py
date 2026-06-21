"""Offline normalization tests for the four ATS sources (no live calls).

Each test feeds a small, representative captured payload through the source and
checks the normalized Job, with emphasis on grounding (salary null unless stated,
remote True/None only).
"""
import unittest
from unittest import mock

from job_agent.sources import ats as ats_mod
from job_agent.sources.ats_sources import (
    AshbySource, GoogleSource, GreenhouseSource, LeverSource, WorkableSource,
)
from job_agent.sources.base import JobQuery


def _fetch(source, fixture):
    with mock.patch.object(ats_mod, "raw_jobs", return_value=fixture):
        return source.fetch()


class TestGreenhouse(unittest.TestCase):
    FIX = [
        {"id": 101, "title": "Director, Corporate Development",
         "updated_at": "2026-05-20T10:00:00-04:00",
         "location": {"name": "San Francisco, CA"},
         "absolute_url": "https://boards.greenhouse.io/acme/jobs/101",
         "content": "&lt;p&gt;Lead M&amp;A.&lt;/p&gt;",
         "departments": [{"id": 1, "name": "Corporate Development"}]},
        {"id": 102, "title": "Strategy Manager (Remote)",
         "updated_at": "2026-05-21T10:00:00-04:00",
         "location": {"name": "Remote - US"},
         "absolute_url": "https://boards.greenhouse.io/acme/jobs/102",
         "content": "<p>Remote role.</p>", "departments": []},
    ]

    def test_normalize(self):
        jobs = _fetch(GreenhouseSource("acme", "Acme"), self.FIX)
        a, b = jobs
        self.assertEqual((a.source, a.source_job_id, a.company), ("greenhouse", "101", "Acme"))
        self.assertEqual(a.location, "San Francisco, CA")
        self.assertIsNone(a.remote)                         # no remote signal
        self.assertEqual(a.description, "Lead M&A.")        # HTML entities decoded + stripped
        self.assertEqual(a.url, "https://boards.greenhouse.io/acme/jobs/101")
        self.assertEqual(a.category, "Corporate Development")
        self.assertIsNone(a.salary_min)                     # GH board API has no salary
        self.assertTrue(b.remote)                           # "Remote - US"


class TestLever(unittest.TestCase):
    FIX = [
        {"id": "abc-1", "text": "VP Strategy",
         "categories": {"location": "San Francisco", "team": "Strategy", "commitment": "Full-time"},
         "hostedUrl": "https://jobs.lever.co/acme/abc-1", "createdAt": 1716200000000,
         "descriptionPlain": "Own strategy.", "workplaceType": "on-site",
         "salaryRange": {"min": 200000, "max": 260000, "currency": "USD"}},
        {"id": "abc-2", "text": "Remote Ops Lead",
         "categories": {"location": "Remote - US", "team": "Ops"},
         "hostedUrl": "https://jobs.lever.co/acme/abc-2", "createdAt": 1716300000000,
         "descriptionPlain": "Remote ops.", "workplaceType": "remote"},
    ]

    def test_normalize(self):
        a, b = _fetch(LeverSource("acme", "Acme"), self.FIX)
        self.assertEqual((a.source, a.source_job_id, a.title), ("lever", "abc-1", "VP Strategy"))
        self.assertEqual((a.salary_min, a.salary_max, a.salary_currency), (200000, 260000, "USD"))
        self.assertIsNone(a.remote)                         # on-site
        self.assertTrue(a.posted_at.endswith("Z"))          # epoch ms -> ISO
        self.assertEqual(a.category, "Strategy")
        self.assertTrue(b.remote)                           # workplaceType == remote
        self.assertIsNone(b.salary_min)                     # no salaryRange -> null


class TestAshby(unittest.TestCase):
    FIX = [
        {"id": "job_1", "title": "Corporate Strategy Lead", "location": "San Francisco Bay Area",
         "isRemote": False, "jobUrl": "https://jobs.ashbyhq.com/acme/job_1",
         "publishedAt": "2026-05-19T00:00:00Z", "descriptionPlain": "Strategy lead.",
         "department": "Strategy",
         "compensation": {  # real Ashby shape: structured Salary component + equity (ignored)
             "compensationTierSummary": "$200K – $250K • Offers Equity",
             "summaryComponents": [
                 {"compensationType": "Salary", "currencyCode": "USD",
                  "minValue": 200000, "maxValue": 250000},
                 {"compensationType": "EquityCashValue", "currencyCode": "USD",
                  "minValue": None, "maxValue": None},
             ],
         }},
        {"id": "job_2", "title": "Remote BizOps", "location": "Remote, US", "isRemote": True,
         "jobUrl": "https://jobs.ashbyhq.com/acme/job_2", "publishedAt": "2026-05-18T00:00:00Z",
         "descriptionHtml": "<p>BizOps.</p>", "department": "BizOps"},  # no compensation
    ]

    def test_normalize(self):
        a, b = _fetch(AshbySource("acme", "Acme"), self.FIX)
        self.assertEqual((a.source, a.source_job_id), ("ashby", "job_1"))
        self.assertEqual(a.location, "San Francisco Bay Area")
        self.assertIsNone(a.remote)
        self.assertEqual(a.description, "Strategy lead.")
        # salary parsed from the structured Salary component (equity ignored)
        self.assertEqual((a.salary_min, a.salary_max, a.salary_currency), (200000, 250000, "USD"))
        self.assertEqual(b.description, "BizOps.")           # from descriptionHtml
        self.assertTrue(b.remote)
        self.assertIsNone(b.salary_min)                      # no compensation -> null, not invented


class TestWorkable(unittest.TestCase):
    FIX = [
        {"id": "wk1", "title": "Head of Strategy",
         "location": {"city": "San Francisco", "region": "California", "country": "United States", "telecommuting": False},
         "application_url": "https://acme.workable.com/j/WK1", "published_on": "2026-05-17",
         "description": "<p>Strategy.</p>", "department": "Strategy"},
        {"id": "wk2", "title": "Remote Ops Manager",
         "location": {"city": None, "country": "United States", "telecommuting": True},
         "application_url": "https://acme.workable.com/j/WK2", "published_on": "2026-05-16",
         "description": "<p>Ops.</p>"},
    ]

    def test_normalize(self):
        a, b = _fetch(WorkableSource("acme", "Acme"), self.FIX)
        self.assertEqual((a.source, a.source_job_id, a.title), ("workable", "wk1", "Head of Strategy"))
        self.assertEqual(a.location, "San Francisco, California, United States")
        self.assertIsNone(a.remote)
        self.assertEqual(a.description, "Strategy.")
        self.assertIsNone(a.salary_min)
        self.assertEqual(b.location, "United States")       # null city dropped
        self.assertTrue(b.remote)                           # telecommuting

    def test_max_results_cap(self):
        jobs = WorkableSource("acme", "Acme")
        with mock.patch.object(ats_mod, "raw_jobs", return_value=self.FIX):
            self.assertEqual(len(jobs.fetch(JobQuery(keywords="", max_results=1))), 1)


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class _FakeSession:
    def __init__(self, html):
        self.html, self.calls = html, 0

    def get(self, url, **k):
        self.calls += 1
        return _Resp(self.html)


class TestGoogle(unittest.TestCase):
    # mirrors the real careers HTML: per-job <a href="jobs/results/<id>-<slug>?..." aria-label="Learn more about <title>">
    FIX = (
        '<a href="jobs/results/107380289797268166-strategic-partnerships-development-manager-ctv-youtube'
        '?q=youtube&amp;location=United+States" aria-label="Learn more about Strategic Partnerships '
        'Development Manager, CTV, YouTube"></a>'
        '<a href="jobs/results/114203708736053958-software-engineer-youtube-shopping'
        '?q=youtube&amp;location=United+States" aria-label="Learn more about Software Engineer, YouTube Shopping"></a>'
        '<a href="jobs/results/131915072506077894-strategic-partner-manager-shopping-youtube'
        '?q=youtube&amp;location=United+States" aria-label="Learn more about Strategic Partner Manager, Shopping, YouTube"></a>'
    )

    def test_parses_filters_and_normalizes(self):
        src = GoogleSource(None, "Google", session=_FakeSession(self.FIX), queries=["YouTube"])
        jobs = src.fetch()
        titles = [j.title for j in jobs]
        self.assertIn("Strategic Partnerships Development Manager, CTV, YouTube", titles)
        self.assertIn("Strategic Partner Manager, Shopping, YouTube", titles)
        self.assertNotIn("Software Engineer, YouTube Shopping", titles)  # eng dropped at source
        j = jobs[0]
        self.assertEqual(j.source, "google")
        self.assertEqual(j.company, "Google")
        self.assertTrue(j.url.startswith("https://www.google.com/about/careers/applications/jobs/results/"))
        self.assertEqual(j.source_job_id, "107380289797268166")

    def test_no_jobs_returns_empty(self):
        src = GoogleSource(None, "Google", session=_FakeSession("<html>no jobs here</html>"), queries=["YouTube"])
        self.assertEqual(src.fetch(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
