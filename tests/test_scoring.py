"""Offline tests for the two-stage scoring pipeline (LLM mocked at llm.map_json).

Verifies: triage->deep flow, cost pre-filters, grounding (invented ids ignored),
label fallback, and the recall-friendly "model omitted a listing" path.
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
            # j4 deliberately omitted -> recall-friendly review stub
            {"id": 99999, "fit_score": 100, "label": "match", "rationale": "ghost", "red_flags": []},
        ]
        # map_json returns one result per batch; here one batch per stage.
        fake = mock.Mock(side_effect=[[triage_result], [deep_result]])

        with mock.patch.object(llm, "map_json", fake):
            stats = scoring.run_scoring(self.conn, PROFILE, deep_model="claude-sonnet-4-6")

        self.assertEqual(fake.call_count, 2)                       # one triage call, one deep call
        self.assertEqual(stats, {"triaged": 4, "kept": 3, "deep_scored": 2})

        triage = {r["job_id"]: r["keep"] for r in self.conn.execute("SELECT job_id,keep FROM scores WHERE stage='triage'")}
        self.assertEqual(triage, {j1: 1, j2: 0, j3: 1, j4: 1})

        # grounding: no score rows for the hallucinated id
        self.assertEqual(self.conn.execute("SELECT COUNT(*) n FROM scores WHERE job_id=99999").fetchone()["n"], 0)

        deep = {r["job_id"]: r for r in self.conn.execute("SELECT * FROM scores WHERE stage='deep'")}
        self.assertEqual(set(deep), {j1, j3, j4})
        self.assertEqual(deep[j1]["fit_score"], 85)
        self.assertEqual(deep[j1]["label"], "match")
        self.assertEqual(json.loads(deep[j1]["red_flags"]), ["none"])
        self.assertEqual(deep[j3]["label"], "stretch")            # invalid label -> derived from score 60
        self.assertIsNone(deep[j4]["fit_score"])                  # omitted -> null, never invented
        self.assertEqual(deep[j4]["label"], "stretch")            # recall: surfaced for review, not skipped

    def test_deep_stores_pros_and_cons_bullets(self):
        j1 = self.ids[0]
        triage_result = [{"id": i, "keep": (i == j1)} for i in self.ids]
        deep_result = [{
            "id": j1, "fit_score": 78, "label": "match",
            "pros": ["Owns corp dev", "Bay Area"],
            "cons": ["Equity-heavy", ""],   # blank bullet dropped
        }]
        fake = mock.Mock(side_effect=[[triage_result], [deep_result]])
        with mock.patch.object(llm, "map_json", fake):
            scoring.run_scoring(self.conn, PROFILE, deep_model="claude-sonnet-4-6")
        row = self.conn.execute(
            "SELECT rationale, red_flags FROM scores WHERE stage='deep' AND job_id=?", (j1,)
        ).fetchone()
        self.assertEqual(json.loads(row["rationale"]), ["Owns corp dev", "Bay Area"])  # pros as JSON list
        self.assertEqual(json.loads(row["red_flags"]), ["Equity-heavy"])               # cons; blank dropped

    def test_triage_fails_open_when_listing_omitted(self):
        # Model returns decisions for none of the jobs -> all kept (high recall)
        with mock.patch.object(llm, "map_json", mock.Mock(side_effect=[[[]]])):
            kept = scoring.triage(self.conn, PROFILE, store.jobs_needing_triage(self.conn))
        self.assertEqual(kept, 4)

    def test_incremental_no_rescore(self):
        drop_all = [{"id": i, "keep": False, "reason": "x"} for i in self.ids]
        with mock.patch.object(llm, "map_json", mock.Mock(side_effect=[[drop_all]])):
            scoring.run_scoring(self.conn, PROFILE, deep_model="claude-sonnet-4-6")
        # second run: nothing left to triage or deep-score -> map_json must not be called
        with mock.patch.object(llm, "map_json", mock.Mock(side_effect=AssertionError("should not be called"))):
            stats = scoring.run_scoring(self.conn, PROFILE, deep_model="claude-sonnet-4-6")
        self.assertEqual(stats, {"triaged": 0, "kept": 0, "deep_scored": 0})


if __name__ == "__main__":
    unittest.main(verbosity=2)
