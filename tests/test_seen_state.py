"""Offline tests for milestone 6: fingerprint dedup + seen-state (no re-notify)."""
import unittest

from job_agent import db, digest, store
from job_agent.models import Job


def _job(src_id: str, title: str, company: str, loc: str = "San Francisco, CA") -> Job:
    return Job(source="adzuna", source_job_id=src_id, title=title, company=company, location=loc)


class TestSeenState(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def _score(self, job_id, score, label):
        store.record_score(self.conn, job_id, stage="deep", model="m", fit_score=score, label=label,
                           rationale="r")

    def test_dedup_and_no_renotify(self):
        # a & b are the SAME role (identical title/company/location) -> same fingerprint
        a = store.upsert_job(self.conn, _job("A", "Director Strategy", "Acme"))[0]
        b = store.upsert_job(self.conn, _job("B", "Director Strategy", "Acme"))[0]
        c = store.upsert_job(self.conn, _job("C", "VP Operations", "Globex"))[0]
        self._score(a, 80, "match")
        self._score(b, 88, "match")   # duplicate, higher score
        self._score(c, 70, "stretch")

        # dedup: a/b collapse to the higher (b); ordered by score -> [b, c]
        rows = digest.select_for_digest(self.conn, min_score=60)
        self.assertEqual([r["id"] for r in rows], [b, c])

        # writing the digest records seen-state for the WHOLE fingerprint group
        path, count, _ = digest.write_digest(self.conn, min_score=60)
        self.assertEqual(count, 2)
        notified = {r["job_id"] for r in self.conn.execute("SELECT job_id FROM notifications")}
        self.assertEqual(notified, {a, b, c})   # a marked too, though only b was shown

        # rerun -> nothing new, no file written
        self.assertEqual(digest.select_for_digest(self.conn, min_score=60), [])
        path2, count2, _ = digest.write_digest(self.conn, min_score=60)
        self.assertEqual((path2, count2), (None, 0))

        # --all still shows the deduped set
        rows_all = digest.select_for_digest(self.conn, min_score=60, only_unnotified=False)
        self.assertEqual([r["id"] for r in rows_all], [b, c])

        # a brand-new, distinct role shows up incrementally
        d = store.upsert_job(self.conn, _job("D", "Head of Corp Dev", "Initech"))[0]
        self._score(d, 90, "match")
        rows3 = digest.select_for_digest(self.conn, min_score=60)
        self.assertEqual([r["id"] for r in rows3], [d])


if __name__ == "__main__":
    unittest.main(verbosity=2)
