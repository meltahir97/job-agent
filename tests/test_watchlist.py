"""Offline tests for WatchlistSource orchestration (resolver + sources mocked)."""
import unittest
from unittest import mock

import requests

from job_agent.companies import Company
from job_agent.models import Job
from job_agent.sources import ats_sources, resolver
from job_agent.sources.resolver import Resolution
from job_agent.sources.watchlist import WatchlistSource


class _FakeGood:
    def __init__(self, slug, company, session=None, timeout=20):
        self.company = company

    def fetch(self, query=None):
        return [
            Job(source="lever", source_job_id="1", title="VP Strategy", company=self.company, location="San Francisco, CA"),
            Job(source="lever", source_job_id="2", title="VP Strategy", company=self.company, location="London, UK"),
        ]


class _FakeErr:
    def __init__(self, slug, company, session=None, timeout=20):
        pass

    def fetch(self, query=None):
        raise requests.HTTPError("boom")


class TestWatchlist(unittest.TestCase):
    def test_collect_resolves_fetches_filters_and_reports(self):
        companies = [
            Company("Good", "auto"),
            Company("Bad", "auto"),
            Company("Err", "greenhouse", "err"),
        ]
        resolutions = {
            "Good": Resolution("Good", "lever", "good", "resolved", 2),
            "Bad": Resolution("Bad", None, None, "unresolved"),
            "Err": Resolution("Err", "greenhouse", "err", "configured"),
        }

        def fake_resolve(co, session, timeout=10):
            return resolutions[co.name]

        fake_sources = {"lever": _FakeGood, "greenhouse": _FakeErr}

        with mock.patch.object(resolver, "resolve_company", side_effect=fake_resolve), \
             mock.patch.object(ats_sources, "SOURCE_BY_ATS", fake_sources):
            jobs, report = WatchlistSource(companies, session=object()).collect()

        # only the SF job survives the location filter (London dropped)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].location, "San Francisco, CA")

        good = next(r for r in report.results if r.company == "Good")
        self.assertEqual((good.fetched, good.kept), (2, 1))

        self.assertEqual([r.company for r in report.unresolved], ["Bad"])
        self.assertEqual([r.company for r in report.errored], ["Err"])
        self.assertIn("boom", report.errored[0].error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
