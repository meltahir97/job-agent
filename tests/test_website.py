"""Offline tests for the website generator (select_master + render_html; no file writes)."""
import json
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

    def test_collapsible_rows_and_filter_controls(self):
        rows = website.select_master(self.conn)
        html, _ = website.render_html(rows)
        self.assertIn('<details class="role"', html)        # collapsed by default, expand on click
        self.assertIn("<summary>", html)
        for control in ('id="q"', 'id="f-tier"', 'id="f-co"', 'id="f-remote"', 'id="f-pay"'):
            self.assertIn(control, html)                    # filter controls present
        self.assertIn("<script>", html)                     # self-contained inline JS, no deps
        self.assertIn("All companies", html)

    def test_pros_cons_bullets_and_pay_and_remote(self):
        conn = db.connect(":memory:")
        db.init_db(conn)
        j = Job(source="lever", source_job_id="9", title="Head of Strategy", company="Payco",
                location="Remote, US", remote=True, url="https://p.example/9", description="d",
                salary_min=150000, salary_max=200000, salary_currency="USD")
        jid = store.upsert_job(conn, j)[0]
        store.record_score(conn, jid, stage="deep", model="m", fit_score=82, label="match",
                           rationale=json.dumps(["Owns strategy", "Bay/remote OK"]),
                           red_flags=["Equity-heavy", "none"])
        html, _ = website.render_html(website.select_master(conn))
        self.assertIn("Why it fits", html)
        self.assertIn("Owns strategy", html)
        self.assertIn("Watch-outs", html)
        self.assertIn("Equity-heavy", html)
        for piece in ("$150k", "$200k", "USD"):
            self.assertIn(piece, html)                      # pay range shown when available
        self.assertIn('data-pay="1"', html)                 # filterable: pay disclosed
        self.assertIn('data-remote="1"', html)              # filterable: remote
        conn.close()

    def test_review_and_reject_affordances(self):
        rows = website.select_master(self.conn)
        html, _ = website.render_html(rows)                          # static (read-only) page
        self.assertIn("job-agent serve", html)                       # banner points to the local app
        jid = self.ids[1]                                            # a surfaced role
        self.assertIn(f"job-agent reject {jid}", html)               # per-row reject command
        self.assertIn(f"job-agent save {jid}", html)
        self.assertIn(f"id {jid} ", html)                            # id shown to act on

    def test_decided_job_ids(self):
        # job 4 was dismissed via feedback in setUp
        self.assertIn(self.ids[4], store.decided_job_ids(self.conn))
        self.assertNotIn(self.ids[1], store.decided_job_ids(self.conn))

    def test_per_company_cap(self):
        conn = db.connect(":memory:")
        db.init_db(conn)
        for i in range(3):  # 3 roles at one high-volume company
            jid = store.upsert_job(conn, _job(10 + i, "BigCo", f"Role {i}", f"https://b/{i}"))[0]
            store.record_score(conn, jid, stage="deep", model="m", fit_score=80 - i, label="match",
                               rationale="why", red_flags=["f"])
        jid = store.upsert_job(conn, _job(20, "SmallCo", "Solo", "https://s/0"))[0]
        store.record_score(conn, jid, stage="deep", model="m", fit_score=70, label="match",
                           rationale="why", red_flags=["f"])
        from collections import Counter
        capped = Counter(r["company"] for r in website.select_master(conn, per_company_cap=2))
        self.assertEqual(capped["BigCo"], 2)      # 3 -> 2 (keeps highest fit)
        self.assertEqual(capped["SmallCo"], 1)
        self.assertEqual(len(website.select_master(conn, per_company_cap=0)), 4)  # 0 = no cap
        conn.close()

    def test_mark_published_clears_new(self):
        rows = website.select_master(self.conn)
        website.mark_published(self.conn, rows)
        rows2 = website.select_master(self.conn)
        _, stats = website.render_html(rows2)
        self.assertEqual(stats["new"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
