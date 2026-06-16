"""Offline tests for the website generator (select_master + render_html; no file writes)."""
import unittest

from job_agent import db, store, website
from job_agent.models import Job


def _job(i, company, title, url):
    return Job(source="greenhouse", source_job_id=str(i), title=title, company=company,
               location="San Francisco, CA", url=url, description="d")


class TestWebsite(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.ids = {}
        seed = [
            (1, "Acme", "Director & Strategy <ops>", "https://acme.example/1", 88, "match"),
            (2, "Globex", "VP Business Development", None, 60, "stretch"),    # no URL -> no Apply link
            (3, "Skipco", "Junior PM", "https://s.example/3", 40, "skip"),    # skip -> excluded
            (4, "Acme", "Dismissed Director", "https://acme.example/4", 80, "match"),  # dismissed -> excluded
        ]
        for i, co, title, url, fit, label in seed:
            jid = store.upsert_job(self.conn, _job(i, co, title, url))[0]
            self.ids[i] = jid
            store.record_score(self.conn, jid, stage="deep", model="m", fit_score=fit, label=label,
                               rationale="why", red_flags=["flag"])
        self.conn.execute("INSERT INTO feedback (job_id,decision,created_at,updated_at) VALUES (?,?,?,?)",
                          (self.ids[4], "dismissed", "2026-01-01", "2026-01-01"))
        self.conn.execute("INSERT INTO notifications (job_id, notified_at) VALUES (?, ?)",
                          (self.ids[1], "2026-01-01"))  # job 1 already seen -> not NEW
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_master_excludes_skip_and_dismissed(self):
        rows = website.select_master(self.conn)
        self.assertEqual([r["id"] for r in rows], [self.ids[1], self.ids[2]])  # 88, 60

    def test_render_tiers_new_badge_escaping_grounding(self):
        rows = website.select_master(self.conn)
        html, stats = website.render_html(rows)
        self.assertEqual((stats["strong"], stats["look"], stats["new"]), (1, 1, 1))  # only job 2 is new
        self.assertIn("Strong matches", html)
        self.assertIn("Worth a look", html)
        self.assertIn(">NEW<", html)                                  # job 2 badged new
        self.assertEqual(html.count(">NEW<"), 1)                      # job 1 (notified) not new
        # HTML escaping (grounding/safety)
        self.assertIn("Director &amp; Strategy &lt;ops&gt;", html)
        self.assertNotIn("<ops>", html)
        # real URLs only, never invented
        self.assertIn('href="https://acme.example/1"', html)
        self.assertNotIn('href="None"', html)
        # excluded roles absent
        self.assertNotIn("Junior PM", html)
        self.assertNotIn("Dismissed Director", html)

    def test_mark_published_clears_new(self):
        rows = website.select_master(self.conn)
        website.mark_published(self.conn, rows)
        rows2 = website.select_master(self.conn)
        _, stats = website.render_html(rows2)
        self.assertEqual(stats["new"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
