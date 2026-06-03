"""Offline tests for digest selection + Markdown rendering (no model calls)."""
import unittest

from job_agent import db, digest, store
from job_agent.models import Job


def _job(i: int, sal: bool = False) -> Job:
    return Job(
        source="adzuna", source_job_id=str(i), title=f"Role {i}", company=f"Co {i}",
        location="San Francisco, CA", url=f"https://example.com/{i}", description="desc",
        salary_min=180000 if sal else None, salary_max=240000 if sal else None,
        salary_currency="USD" if sal else None,
    )


class TestDigest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.j = [store.upsert_job(self.conn, _job(i, sal=(i == 1)))[0] for i in range(1, 5)]
        j1, j2, j3, j4 = self.j
        store.record_score(self.conn, j1, stage="deep", model="m", fit_score=88, label="match",
                           rationale="Strong overlap on corp dev.", red_flags=["Fast-paced", "none"])
        store.record_score(self.conn, j2, stage="deep", model="m", fit_score=65, label="stretch",
                           rationale="Adjacent function.")
        store.record_score(self.conn, j3, stage="deep", model="m", fit_score=40, label="skip",
                           rationale="Too junior.")
        store.record_score(self.conn, j4, stage="deep", model="m", fit_score=80, label="match",
                           rationale="Good, but dismissed.")
        # j4 dismissed via feedback -> must be excluded
        self.conn.execute(
            "INSERT INTO feedback (job_id, decision, created_at, updated_at) VALUES (?,?,?,?)",
            (j4, "dismissed", "2026-01-01", "2026-01-01"),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_selection_excludes_skip_and_dismissed_and_orders_by_score(self):
        rows = digest.select_for_digest(self.conn, min_score=60)
        self.assertEqual([r["id"] for r in rows], [self.j[0], self.j[1]])  # 88 then 65

    def test_min_score_filter(self):
        rows = digest.select_for_digest(self.conn, min_score=85)
        self.assertEqual([r["id"] for r in rows], [self.j[0]])

    def test_only_unnotified_filter(self):
        self.conn.execute(
            "INSERT INTO notifications (job_id, notified_at) VALUES (?, ?)", (self.j[0], "2026-01-01")
        )
        self.conn.commit()
        rows = digest.select_for_digest(self.conn, min_score=60, only_unnotified=True)
        self.assertEqual([r["id"] for r in rows], [self.j[1]])  # j1 already notified

    def test_markdown_content_and_formatting(self):
        rows = digest.select_for_digest(self.conn, min_score=60)
        md = digest.render_markdown(rows)
        self.assertIn("## Co 1 (1)", md)                     # grouped by company
        self.assertIn("## Co 2 (1)", md)
        self.assertLess(md.index("Co 1"), md.index("Co 2"))  # top-scoring company first
        self.assertIn("⭐ match", md)                         # per-role label badge
        self.assertIn("🔭 stretch", md)
        self.assertIn("88/100", md)
        self.assertIn("Strong overlap on corp dev.", md)
        self.assertIn("$180k–$240k USD", md)               # salary formatted
        self.assertIn("Fast-paced", md)                     # real red flag kept
        self.assertNotIn("; none", md)                       # trivial "none" filtered
        self.assertIn("https://example.com/1", md)           # link present
        self.assertNotIn("Too junior", md)                   # skip excluded


if __name__ == "__main__":
    unittest.main(verbosity=2)
