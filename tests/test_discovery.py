"""Offline tests for company discovery (web-search + verification mocked)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from job_agent import config, db, discovery, store, website
from job_agent.companies import Company
from job_agent.sources.resolver import Resolution

PROFILE = {"experience_threads": ["Strategy", "Media"], "seniority": "Director", "summary": "x"}

CANDIDATES = [
    {"company": "Existing Co", "reason": "dup", "evidence_url": "https://e.example"},      # already tracked -> skip
    {"company": "ResolvedCo", "reason": "fits strategy", "evidence_url": "https://r.example"},  # real feed -> proposed
    {"company": "CareersCo", "reason": "fits media", "evidence_url": "https://careers.example/jobs"},  # reachable page -> proposed
    {"company": "GhostCo", "reason": "maybe", "evidence_url": "https://ghost.invalid"},     # unreachable -> unverified
]


def _resolve(company, session, **kw):
    if company.name == "ResolvedCo":
        return Resolution("ResolvedCo", "greenhouse", "resolvedco", "resolved", 5, "ok")
    return Resolution(company.name, None, None, "unresolved", detail="no board")


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_norm_and_cadence(self):
        self.assertEqual(discovery._norm("a16z (Games)"), "a16z games")
        self.assertTrue(discovery.should_run(self.conn))                      # never run
        db.set_meta(self.conn, "last_discovery_at", "2099-01-01T00:00:00+00:00")
        self.assertFalse(discovery.should_run(self.conn))                     # recent (future)
        self.assertTrue(discovery.should_run(self.conn, force=True))          # force overrides

    def test_discover_verifies_and_buckets(self):
        with mock.patch.object(discovery, "load_companies", return_value=[Company("Existing Co", "greenhouse", "existing")]), \
             mock.patch.object(discovery.llm, "web_search", return_value=(json.dumps(CANDIDATES), [])), \
             mock.patch.object(discovery.resolver_mod, "resolve_company", side_effect=_resolve), \
             mock.patch.object(discovery, "_http_ok", side_effect=lambda url, sess, **k: url == "https://careers.example/jobs"):
            res = discovery.discover(self.conn, PROFILE, model="x")

        proposed = {p["company"] for p in res["proposed"]}
        unverified = {u["company"] for u in res["unverified"]}
        self.assertEqual(proposed, {"ResolvedCo", "CareersCo"})              # existing excluded
        self.assertEqual(unverified, {"GhostCo"})
        # ResolvedCo stored with a real feed + ats/slug; CareersCo as careers page
        rc = [s for s in store.list_suggestions(self.conn, "proposed") if s["company"] == "ResolvedCo"][0]
        self.assertEqual((rc["ats"], rc["slug"]), ("greenhouse", "resolvedco"))
        self.assertIn("greenhouse.io", rc["evidence_url"])
        # last_discovery_at stamped
        self.assertIsNotNone(db.get_meta(self.conn, "last_discovery_at"))

    def test_discover_does_not_repropose_dismissed(self):
        store.add_suggestion(self.conn, company="GhostCo", norm_name="ghostco", reason="x",
                             evidence_url=None, ats=None, slug=None, status="dismissed")
        with mock.patch.object(discovery, "load_companies", return_value=[]), \
             mock.patch.object(discovery.llm, "web_search", return_value=(json.dumps([CANDIDATES[3]]), [])), \
             mock.patch.object(discovery.resolver_mod, "resolve_company", side_effect=_resolve), \
             mock.patch.object(discovery, "_http_ok", return_value=False):
            res = discovery.discover(self.conn, PROFILE, model="x")
        self.assertEqual(res["proposed"], [])
        self.assertEqual(res["unverified"], [])                               # dismissed -> not re-surfaced

    def test_approve_appends_yaml_and_dismiss(self):
        with tempfile.TemporaryDirectory() as td:
            yml = Path(td) / "companies.yaml"
            yml.write_text("companies:\n  - name: Seed\n    ats: greenhouse\n    slug: seed\n", encoding="utf-8")
            store.add_suggestion(self.conn, company="ResolvedCo", norm_name="resolvedco", reason="fits",
                                 evidence_url="https://x", ats="greenhouse", slug="resolvedco", status="proposed")
            sid = store.list_suggestions(self.conn, "proposed")[0]["id"]
            with mock.patch.object(config, "COMPANIES_PATH", yml):
                msg = discovery.approve(self.conn, sid)
            self.assertIn("approved", msg)
            text = yml.read_text()
            self.assertIn("ResolvedCo", text)
            self.assertIn("slug: resolvedco", text)
            self.assertEqual(store.get_suggestion(self.conn, sid)["status"], "approved")

    def test_approve_without_slug_prompts(self):
        store.add_suggestion(self.conn, company="CareersCo", norm_name="careersco", reason="x",
                             evidence_url="https://c", ats=None, slug=None, status="proposed")
        sid = store.list_suggestions(self.conn, "proposed")[0]["id"]
        msg = discovery.approve(self.conn, sid)
        self.assertIn("--slug", msg)

    def test_website_consider_section(self):
        store.add_suggestion(self.conn, company="CareersCo", norm_name="careersco", reason="fits media",
                             evidence_url="https://careers.example/jobs", ats=None, slug=None, status="proposed")
        html, _ = website.render_html([], suggestions=store.list_suggestions(self.conn, "proposed"))
        self.assertIn("Companies to consider", html)
        self.assertIn("CareersCo", html)
        self.assertIn("job-agent approve", html)
        self.assertIn("https://careers.example/jobs", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
