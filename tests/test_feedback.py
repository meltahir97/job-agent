"""Offline tests for milestone 7: feedback capture + its effect on scoring/digest."""
import unittest

from job_agent import db, digest, store
from job_agent.models import Job


class TestFeedback(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.j = store.upsert_job(
            self.conn, Job(source="adzuna", source_job_id="1", title="Director Strategy", company="Acme", location="SF")
        )[0]
        store.record_score(self.conn, self.j, stage="deep", model="m", fit_score=90, label="match", rationale="r")

    def tearDown(self):
        self.conn.close()

    def test_decision_is_upserted_not_duplicated(self):
        store.record_feedback(self.conn, self.j, "saved")
        self.assertEqual([r["decision"] for r in store.list_feedback(self.conn)], ["saved"])
        store.record_feedback(self.conn, self.j, "dismissed", note="too junior")
        rows = store.list_feedback(self.conn)
        self.assertEqual(len(rows), 1)                  # one current decision per job
        self.assertEqual(rows[0]["decision"], "dismissed")
        self.assertEqual(rows[0]["note"], "too junior")

    def test_feedback_feeds_scoring_examples(self):
        store.record_feedback(self.conn, self.j, "saved")
        ex = store.feedback_examples(self.conn)
        self.assertEqual((ex[0]["decision"], ex[0]["title"]), ("saved", "Director Strategy"))

    def test_dismissed_excluded_from_digest(self):
        self.assertEqual(len(digest.select_for_digest(self.conn, min_score=60)), 1)
        store.record_feedback(self.conn, self.j, "dismissed")
        self.assertEqual(len(digest.select_for_digest(self.conn, min_score=60)), 0)

    def test_get_job(self):
        self.assertIsNotNone(store.get_job(self.conn, self.j))
        self.assertIsNone(store.get_job(self.conn, 99999))


if __name__ == "__main__":
    unittest.main(verbosity=2)
