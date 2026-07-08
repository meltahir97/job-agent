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


class TestApplications(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.jid = _job(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_applied_flow_status_and_notes(self):
        self.assertEqual(server.job_action(self.conn, self.jid, "applied")[0], 200)
        self.assertIn(self.jid, store.applied_job_ids(self.conn))
        self.assertEqual(store.get_application(self.conn, self.jid)["status"], "applied")
        # applying also records a positive feedback signal for future scoring
        fb = self.conn.execute("SELECT decision FROM feedback WHERE job_id=?", (self.jid,)).fetchone()
        self.assertEqual(fb["decision"], "saved")
        # status pipeline
        self.assertEqual(server.application_update(self.conn, self.jid, "interviewing")[0], 200)
        self.assertEqual(store.get_application(self.conn, self.jid)["status"], "interviewing")
        self.assertEqual(server.application_update(self.conn, self.jid, "bogus")[0], 400)
        self.assertEqual(server.application_update(self.conn, 999999, "offer")[0], 404)
        # notes + to-dos
        code, _ = server.note_add(self.conn, self.jid, {"text": "sent follow-up", "kind": "note"})
        self.assertEqual(code, 200)
        code, res = server.note_add(self.conn, self.jid, {"text": "prep case study", "kind": "todo"})
        nid = res["id"]
        self.assertEqual(server.note_add(self.conn, self.jid, {"text": "  "})[0], 400)
        self.assertEqual(server.note_action(self.conn, nid, "toggle", {"done": True})[0], 200)
        notes = store.list_app_notes(self.conn, self.jid)
        self.assertEqual(len(notes), 2)
        self.assertEqual([n["done"] for n in notes if n["kind"] == "todo"], [1])
        self.assertEqual(server.note_action(self.conn, nid, "delete", {})[0], 200)
        self.assertEqual(len(store.list_app_notes(self.conn, self.jid)), 1)
        # untrack (notes are kept)
        self.assertEqual(server.job_action(self.conn, self.jid, "unapply")[0], 200)
        self.assertNotIn(self.jid, store.applied_job_ids(self.conn))
        self.assertEqual(len(store.list_app_notes(self.conn, self.jid)), 1)

    def test_page_moves_applied_role_to_tracker(self):
        server.job_action(self.conn, self.jid, "applied")
        server.note_add(self.conn, self.jid, {"text": "waiting on recruiter", "kind": "note"})
        html = server.render_page(self.conn)
        self.assertIn("Applications", html)                    # tracker section present
        self.assertIn("waiting on recruiter", html)            # note rendered
        self.assertIn('class="appst"', html)                   # status dropdown
        self.assertEqual(html.count("Director, Corp Dev"), 1)  # only in tracker, not the role list

    def test_applications_never_on_static_page(self):
        server.job_action(self.conn, self.jid, "applied")
        server.note_add(self.conn, self.jid, {"text": "personal note", "kind": "note"})
        page, _ = website.render_html(website.select_master(self.conn), interactive=False)
        self.assertNotIn("personal note", page)


class TestDraftDedupe(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.jid = _job(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_local_draft_migrates_without_regenerating(self):
        with tempfile.TemporaryDirectory() as td:
            r, c = Path(td) / "resume.docx", Path(td) / "cover_letter.docx"
            r.write_bytes(b"resume-bytes")
            c.write_bytes(b"cover-bytes")
            store.record_draft(self.conn, self.jid, company="Roku", title="Director, Corp Dev",
                               dir=td, resume_docx=r, cover_docx=c, model="m")
            links = {"folder": "https://drive.google.com/drive/folders/mig1",
                     "resume_url": "https://docs/r", "cover_url": "https://docs/c"}
            with mock.patch.object(drafting.oauth, "is_authorized", return_value=True), \
                 mock.patch.object(drafting.oauth, "upload_drafts", return_value=links) as up, \
                 mock.patch.object(drafting.llm, "complete_text",
                                   side_effect=AssertionError("must NOT regenerate")):
                code, res = server.draft_action(self.conn, self.jid)
        self.assertEqual(code, 200)
        self.assertIn("mig1", res["folder"])
        # the ORIGINAL bytes were uploaded (same pair, not a new generation)
        self.assertEqual(up.call_args.args[3], b"resume-bytes")
        self.assertEqual(store.get_draft(self.conn, self.jid)["drive_url"], links["folder"])

    def test_same_role_under_new_id_reuses_draft(self):
        store.record_draft(self.conn, self.jid, company="Roku", title="Director, Corp Dev",
                           dir="https://folder", drive_url="https://folder",
                           resume_url="https://r", cover_url="https://c", model="m")
        jid2 = _job(self.conn, sid="2")  # same company+title re-fetched under a new id
        with mock.patch.object(drafting.llm, "complete_text",
                               side_effect=AssertionError("must NOT redraft")):
            code, res = server.draft_action(self.conn, jid2)
        self.assertEqual(code, 200)
        self.assertTrue(res.get("existing"))
        self.assertEqual(store.get_draft(self.conn, jid2)["drive_url"], "https://folder")


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


class TestWatchlistTracker(unittest.TestCase):
    def test_feed_status_and_tracker_render(self):
        conn = db.connect(":memory:")
        db.init_db(conn)
        jid = _job(conn)  # Roku job, deep-scored 90
        store.upsert_feed_status(conn, company="Roku", ats="greenhouse", slug="roku",
                                 ok=True, fetched=224, kept=48, error=None)
        store.upsert_feed_status(conn, company="Overtime", ats=None, slug=None,
                                 ok=False, fetched=0, kept=0,
                                 error="no public job feed found yet")
        html = server.render_page(conn)
        self.assertIn("Watchlist health", html)
        self.assertIn("1/2 feeds live", html)
        self.assertIn("greenhouse:roku", html)
        self.assertIn("no public job feed found yet", html)   # broken feed surfaced honestly
        # funnel numbers joined in
        funnel = {r["company"]: r for r in store.company_funnel(conn, 30)}
        self.assertEqual(funnel["Roku"]["in_db"], 1)
        self.assertEqual(funnel["Roku"]["surfaced"], 1)
        # upsert replaces, not duplicates
        store.upsert_feed_status(conn, company="Roku", ats="greenhouse", slug="roku",
                                 ok=True, fetched=230, kept=50, error=None)
        rows = store.list_feed_status(conn)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["fetched"] for r in rows if r["company"] == "Roku"], [230])
        # never on the static page
        page, _ = website.render_html([], interactive=False)
        self.assertNotIn("Watchlist health", page)
        conn.close()


class TestNewSources(unittest.TestCase):
    def test_bamboohr_parses_list(self):
        from job_agent.sources.ats_sources import BambooHRSource

        class FakeResp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"result": [
                    {"id": "54", "jobOpeningName": "Head of Partnerships",
                     "departmentLabel": "Business", "location": {"city": "San Francisco", "state": "CA"}},
                    {"id": "55", "jobOpeningName": "", "location": None},  # no title -> skipped
                ]}
        class FakeSession:
            def get(self, url, **kw):
                assert url == "https://podcastle.bamboohr.com/careers/list"
                return FakeResp()
        jobs = BambooHRSource("podcastle", "Podcastle", session=FakeSession()).fetch()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, "Head of Partnerships")
        self.assertEqual(jobs[0].location, "San Francisco, CA")
        self.assertEqual(jobs[0].url, "https://podcastle.bamboohr.com/careers/54")

    def test_ashby_falls_back_to_graphql_when_posting_api_dead(self):
        from job_agent.sources import ats_sources

        class FakeResp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"data": {"jobBoard": {"jobPostings": [
                    {"id": "abc-123", "title": "Strategic Finance Lead",
                     "locationName": "San Francisco", "employmentType": "FullTime"}]}}}
        class FakeSession:
            def post(self, url, **kw):
                assert "non-user-graphql" in url
                return FakeResp()
        src = ats_sources.AshbySource("whatnot", "Whatnot", session=FakeSession())
        with mock.patch.object(ats_sources.ats_mod, "raw_jobs", side_effect=RuntimeError("404")):
            jobs = src.fetch()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, "Strategic Finance Lead")
        self.assertEqual(jobs[0].url, "https://jobs.ashbyhq.com/whatnot/abc-123")


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
