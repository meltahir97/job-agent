"""Offline tests for the local interactive app's mutations + interactive render."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from job_agent import config, db, server, store, website
from job_agent.models import Job


def _job(conn, sid="1", company="Roku", title="Director, Corp Dev"):
    j = Job(source="greenhouse", source_job_id=sid, title=title, company=company,
            location="San Jose, CA", url=f"https://x/{sid}", description="d")
    jid = store.upsert_job(conn, j)[0]
    store.record_score(conn, jid, stage="deep", model="m", fit_score=90, label="match",
                       rationale='["fit"]', red_flags=["watch"])
    return jid


class TestJobActions(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.jid = _job(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_reject_then_undo(self):
        code, res = server.job_action(self.conn, self.jid, "reject")
        self.assertEqual(code, 200)
        self.assertIn(self.jid, store.decided_job_ids(self.conn))
        self.assertEqual([r["id"] for r in website.select_master(self.conn)], [])  # hidden
        code, res = server.job_action(self.conn, self.jid, "undo")
        self.assertEqual(code, 200)
        self.assertNotIn(self.jid, store.decided_job_ids(self.conn))
        self.assertEqual([r["id"] for r in website.select_master(self.conn)], [self.jid])  # back

    def test_save_and_errors(self):
        self.assertEqual(server.job_action(self.conn, self.jid, "save")[0], 200)
        row = self.conn.execute("SELECT decision FROM feedback WHERE job_id=?", (self.jid,)).fetchone()
        self.assertEqual(row["decision"], "saved")
        self.assertEqual(server.job_action(self.conn, 999999, "reject")[0], 404)
        self.assertEqual(server.job_action(self.conn, self.jid, "bogus")[0], 400)


class TestSuggestionActions(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_dismiss(self):
        store.add_suggestion(self.conn, company="Foo", norm_name="foo", reason="r",
                             evidence_url="https://f", ats=None, slug=None, status="proposed")
        sid = store.list_suggestions(self.conn, "proposed")[0]["id"]
        self.assertEqual(server.suggestion_action(self.conn, sid, "dismiss")[0], 200)
        self.assertEqual(store.get_suggestion(self.conn, sid)["status"], "dismissed")

    def test_approve_with_feed_appends_yaml(self):
        store.add_suggestion(self.conn, company="Patreon", norm_name="patreon", reason="r",
                             evidence_url="https://p", ats="ashby", slug="patreon", status="proposed")
        sid = store.list_suggestions(self.conn, "proposed")[0]["id"]
        with tempfile.TemporaryDirectory() as td:
            yml = Path(td) / "companies.yaml"
            yml.write_text("companies:\n  - name: Seed\n    ats: greenhouse\n    slug: seed\n")
            with mock.patch.object(config, "COMPANIES_PATH", yml):
                code, res = server.suggestion_action(self.conn, sid, "approve")
            self.assertEqual(code, 200)
            self.assertTrue(res["ok"])
            self.assertIn("Patreon", yml.read_text())

    def test_approve_without_slug_is_422(self):
        store.add_suggestion(self.conn, company="EA", norm_name="ea", reason="r",
                             evidence_url="https://ea", ats=None, slug=None, status="proposed")
        sid = store.list_suggestions(self.conn, "proposed")[0]["id"]
        code, res = server.suggestion_action(self.conn, sid, "approve")
        self.assertEqual(code, 422)
        self.assertFalse(res["ok"])


class TestInteractiveRender(unittest.TestCase):
    def test_interactive_has_buttons_static_has_commands(self):
        conn = db.connect(":memory:")
        db.init_db(conn)
        _job(conn)
        store.add_suggestion(conn, company="Patreon", norm_name="patreon", reason="r",
                             evidence_url="https://p", ats="ashby", slug="patreon", status="proposed")
        rows = website.select_master(conn)
        sugg = store.list_suggestions(conn, "proposed")

        html_i, _ = website.render_html(rows, suggestions=sugg, interactive=True)
        self.assertIn('data-act="reject"', html_i)            # role buttons
        self.assertIn('data-kind="sug"', html_i)              # suggestion buttons
        self.assertIn("/api/job/", html_i)                    # actions script wired to the API
        self.assertNotIn("job-agent reject", html_i)          # no terminal commands in the app

        html_s, _ = website.render_html(rows, suggestions=sugg, interactive=False)
        self.assertNotIn('data-act="reject"', html_s)         # static page: no buttons
        self.assertIn("job-agent serve", html_s)              # points to the local app
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
