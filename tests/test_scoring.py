"""Offline tests for the two-stage scoring pipeline (LLM mocked).

Verifies: triage->deep flow, cost pre-filters, grounding (invented ids ignored),
label fallback, and the "model omitted a listing" safe path.
"""
import json
import unittest
from unittest import mock

from job_agent import db, store
from job_agent.models import Job
from job_agent.reasoning import llm, scoring

PROFILE = {"name": "T", "seniority": "Director", "domains": ["Strategy"], "target_titles": ["Director"]}


def _job(i: int) -> Job:
    return Job(
        source="adzuna", source_job_id=str(i), title=f"Job {i}",
        company=f"Co {i}", location="San Francisco, CA", description=f"Description for job {i}.",
    )


class TestScoring(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.ids = [store.upsert_job(self.conn, _job(i))[0] for i in range(1, 5)]  # 4 jobs

    def tearDown(self):
        self.conn.close()

    def test_full_two_stage_flow_with_grounding(self):
        j1, j2, j3, j4 = self.ids
        triage_result = [
            {"id": j1, "keep": True, "reason": "fit"},
            {"id": j2, "keep": False, "reason": "wrong function"},
            {"id": j3, "keep": True, "reason": "fit"},
            {"id": j4, "keep": True, "reason": "fit"},
            {"id": 99999, "keep": True, "reason": "HALLUCINATED id — must be ignored"},
        ]
        deep_result = [
            {"id": j1, "fit_score": 85, "label": "match", "rationale": "Strong overlap.", "red_flags": ["none"]},
            {"id": j3, "fit_score": 60, "label": "unknown-label", "rationale": "Partial.", "red_flags": []},
            # j4 deliberately omitted -> safe skip path
            {"id": 99999, "fit_score": 100, "label": "match", "rationale": "ghost", "red_flags": []},
        ]
        fake = mock.Mock(side_effect=[triage_result, deep_result])

        with mock.patch.object(llm, "complete_json", fake):
            stats = scoring.run_scoring(self.conn, PROFILE, deep_model="claude-sonnet-4-6")

        # exactly two LLM calls: one triage batch + one deep batch
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(stats, {"triaged": 4, "kept": 3, "deep_scored": 2})

        # triage rows: keep flags correct
        triage = {r["job_id"]: r["keep"] for r in self.conn.execute("SELECT job_id,keep FROM scores WHERE stage='triage'")}
        self.assertEqual(triage, {j1: 1, j2: 0, j3: 1, j4: 1})

        # grounding: no score rows for the hallucinated id
        bogus = self.conn.execute("SELECT COUNT(*) n FROM scores WHERE job_id=99999").fetchone()["n"]
        self.assertEqual(bogus, 0)

        deep = {r["job_id"]: r for r in self.conn.execute("SELECT * FROM scores WHERE stage='deep'")}
        self.assertEqual(set(deep), {j1, j3, j4})            # j4 got a placeholder row
        self.assertEqual(deep[j1]["fit_score"], 85)
        self.assertEqual(deep[j1]["label"], "match")
        self.assertEqual(json.loads(deep[j1]["red_flags"]), ["none"])
        self.assertEqual(deep[j3]["label"], "stretch")       # invalid label -> derived from score 60
        self.assertIsNone(deep[j4]["fit_score"])             # omitted -> null, never invented
        self.assertEqual(deep[j4]["label"], "skip")

    def test_incremental_no_rescore(self):
        fake = mock.Mock(side_effect=[
            [{"id": i, "keep": False, "reason": "x"} for i in self.ids],  # triage drops all
        ])
        with mock.patch.object(llm, "complete_json", fake):
            scoring.run_scoring(self.conn, PROFILE, deep_model="claude-sonnet-4-6")
        # second run: nothing left to triage or deep-score -> zero LLM calls
        fake2 = mock.Mock(side_effect=AssertionError("should not be called"))
        with mock.patch.object(llm, "complete_json", fake2):
            stats = scoring.run_scoring(self.conn, PROFILE, deep_model="claude-sonnet-4-6")
        self.assertEqual(stats, {"triaged": 0, "kept": 0, "deep_scored": 0})


if __name__ == "__main__":
    unittest.main(verbosity=2)
