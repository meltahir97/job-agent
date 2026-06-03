"""Offline normalization tests for the four ATS sources (no live calls).

Each test feeds a small, representative captured payload through the source and
checks the normalized Job, with emphasis on grounding (salary null unless stated,
remote True/None only).
"""
import unittest
from unittest import mock

from job_agent.sources import ats as ats_mod
from job_agent.sources.ats_sources import (
    AshbySource, GreenhouseSource, LeverSource, WorkableSource,
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
         "department": "Strategy", "compensation": {"compensationTierSummary": "$200K-$250K"}},
        {"id": "job_2", "title": "Remote BizOps", "location": "Remote, US", "isRemote": True,
         "jobUrl": "https://jobs.ashbyhq.com/acme/job_2", "publishedAt": "2026-05-18T00:00:00Z",
         "descriptionHtml": "<p>BizOps.</p>", "department": "BizOps"},
    ]

    def test_normalize(self):
        a, b = _fetch(AshbySource("acme", "Acme"), self.FIX)
        self.assertEqual((a.source, a.source_job_id), ("ashby", "job_1"))
        self.assertEqual(a.location, "San Francisco Bay Area")
        self.assertIsNone(a.remote)
        self.assertEqual(a.description, "Strategy lead.")
        self.assertIsNone(a.salary_min)                     # comp not a simple min/max -> null
        self.assertEqual(b.description, "BizOps.")           # from descriptionHtml
        self.assertTrue(b.remote)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
