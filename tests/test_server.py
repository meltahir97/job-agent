"""Offline tests for the local interactive app's mutations + interactive render."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from job_agent import config, db, drafting, server, store, website
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

    def test_approve_without_slug_auto_adds_no_prompt(self):
        from job_agent import discovery
        store.add_suggestion(self.conn, company="EA", norm_name="ea", reason="r",
                             evidence_url="https://ea", ats=None, slug=None, status="proposed")
        sid = store.list_suggestions(self.conn, "proposed")[0]["id"]
        with tempfile.TemporaryDirectory() as td:
            yml = Path(td) / "companies.yaml"
            yml.write_text("companies:\n  - name: Seed\n    ats: greenhouse\n    slug: seed\n")
            with mock.patch.object(config, "COMPANIES_PATH", yml), \
                 mock.patch.object(discovery, "_auto_resolve_board", return_value=(None, None)):
                code, res = server.suggestion_action(self.conn, sid, "approve")
            self.assertEqual(code, 200)            # never 422 — no slug ever requested
            self.assertTrue(res["ok"])
            self.assertIn("ats: auto", yml.read_text())


class TestDraftAction(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.jid = _job(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_generates_and_returns_links(self):
        with mock.patch.object(drafting, "load_profiles", return_value=({}, {})), \
             mock.patch.object(drafting, "generate_for_role",
                               return_value={"where": "drive", "folder": "https://f",
                                             "resume_url": "https://r", "cover_url": "https://c"}):
            code, res = server.draft_action(self.conn, self.jid)
        self.assertEqual(code, 200)
        self.assertEqual(res["folder"], "https://f")

    def test_bad_id_404(self):
        self.assertEqual(server.draft_action(self.conn, 999999)[0], 404)

    def test_returns_existing_without_regenerating(self):
        store.record_draft(self.conn, self.jid, company="Roku", title="X", dir="https://folder",
                           drive_url="https://folder", resume_url="https://r", cover_url="https://c", model="m")
        with mock.patch.object(drafting, "generate_for_role", side_effect=AssertionError("should not regen")):
            code, res = server.draft_action(self.conn, self.jid)
        self.assertEqual(code, 200)
        self.assertEqual(res["folder"], "https://folder")
        self.assertTrue(res.get("existing"))


class TestNonMatchView(unittest.TestCase):
    def test_other_section_and_draft_button(self):
        conn = db.connect(":memory:")
        db.init_db(conn)
        j = Job(source="greenhouse", source_job_id="9", title="Random Eng Role", company="Roku",
                location="SF", url="https://x/9", description="d")
        jid = store.upsert_job(conn, j)[0]
        store.record_score(conn, jid, stage="deep", model="m", fit_score=10, label="skip",
                           rationale='["no"]', red_flags=["x"])
        self.assertEqual(website.select_master(conn), [])                  # hidden from default view
        allrows = website.select_all_scored(conn)
        self.assertTrue(any(r["id"] == jid for r in allrows))             # present in all view
        html_i, _ = website.render_html(allrows, interactive=True, include_all=True)
        self.assertIn("Other roles", html_i)                              # non-match section
        self.assertIn('data-kind="draft"', html_i)                        # draftable
        self.assertIn('id="f-all"', html_i)                               # include-non-matches toggle
        conn.close()


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
