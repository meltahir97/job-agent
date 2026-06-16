"""Offline tests for SmartRecruiters + Workday source normalization (HTTP faked)."""
import unittest

from job_agent.sources.ats_sources import SmartRecruitersSource, WorkdaySource


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _Session:
    def __init__(self, payload):
        self.payload = payload

    def get(self, url, **kw):
        return _Resp(self.payload)

    def post(self, url, **kw):
        return _Resp(self.payload)


SR = {
    "totalFound": 1,
    "content": [{
        "id": "abc123", "name": "Director, Corporate Development",
        "releasedDate": "2026-06-01T00:00:00Z",
        "location": {"city": "San Francisco", "region": "California", "country": "us", "remote": False},
        "department": {"label": "Corporate Development"}, "function": {"label": "Strategy & Ops"},
        "experienceLevel": {"label": "Director"}, "typeOfEmployment": {"label": "Full-time"},
    }],
}

WD = {
    "total": 1,
    "jobPostings": [{
        "title": "Corporate Development Manager",
        "externalPath": "/job/Santa-Clara/Corp-Dev_JR123",
        "locationsText": "US, CA, Santa Clara", "postedOn": "Posted Today",
    }],
}


class TestSmartRecruiters(unittest.TestCase):
    def test_normalize(self):
        jobs = SmartRecruitersSource("ElectronicArts", "EA", session=_Session(SR)).fetch()
        self.assertEqual(len(jobs), 1)
        j = jobs[0]
        self.assertEqual((j.source, j.source_job_id, j.title), ("smartrecruiters", "abc123", "Director, Corporate Development"))
        self.assertEqual(j.company, "EA")
        self.assertEqual(j.location, "San Francisco, California, us")
        self.assertIsNone(j.remote)                                  # remote=False -> None (never assert False)
        self.assertEqual(j.url, "https://jobs.smartrecruiters.com/ElectronicArts/abc123")
        self.assertEqual(j.posted_at, "2026-06-01T00:00:00Z")
        self.assertIn("Director", j.description)                     # composed from level/function


class TestWorkday(unittest.TestCase):
    def test_normalize(self):
        src = WorkdaySource("nvidia", "NVIDIA", session=_Session(WD), dc="wd5", site="NVIDIAExternalCareerSite")
        jobs = src.fetch()
        self.assertEqual(len(jobs), 1)
        j = jobs[0]
        self.assertEqual((j.source, j.source_job_id), ("workday", "/job/Santa-Clara/Corp-Dev_JR123"))
        self.assertEqual(j.title, "Corporate Development Manager")
        self.assertEqual(j.location, "US, CA, Santa Clara")
        self.assertEqual(
            j.url,
            "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/Santa-Clara/Corp-Dev_JR123",
        )
        self.assertIsNone(j.posted_at)                               # relative date -> null, not fabricated

    def test_requires_dc_and_site(self):
        with self.assertRaises(ValueError):
            WorkdaySource("nvidia", "NVIDIA", session=_Session(WD))   # missing dc/site


if __name__ == "__main__":
    unittest.main(verbosity=2)
