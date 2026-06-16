"""Offline tests for draft generation (LLM mocked; real .docx written to a temp dir)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from job_agent import config, db, drafting, store
from job_agent.models import Job

MASTER = {
    "name": "Muhammad Eltahir",
    "experience_threads": ["Strategy", "Operations", "Chief of Staff"],
    "employers": [{"company": "Condé Nast", "titles": ["Corp Dev Manager"], "dates": "2022-2025",
                   "highlights": ["Led M&A integration"]}],
    "skills": ["M&A", "Strategy"], "education": ["HBS MBA"],
}
VOICE = {"tone": "warm, precise", "characteristic_phrases": ["I am drawn to"], "approximate": False}

FAKE_LLM = {
    "resume_markdown": "# Muhammad Eltahir\n\n## Experience\n\n### Condé Nast — Corp Dev Manager\n- Led **M&A** integration\n\n## Education\n- HBS MBA",
    "cover_letter_markdown": "Dear OpenAI,\n\nI am drawn to your mission. My corp-dev work fits this role.\n\nSincerely,\nMuhammad",
    "omitted_requirements": ["10+ years enterprise SaaS"],
}


def _job(conn, company="OpenAI", title="Corp Dev Lead", sid="9"):
    j = Job(source="ashby", source_job_id=sid, title=title, company=company,
            location="San Francisco", url=f"https://x/{sid}", description="Lead corp dev. 10+ yrs SaaS.")
    jid = store.upsert_job(conn, j)[0]
    store.record_score(conn, jid, stage="deep", model="m", fit_score=82, label="match",
                       rationale='["fit"]', red_flags=["watch"])
    return store.get_job(conn, jid)


class TestMdToDocx(unittest.TestCase):
    def test_renders_headings_bullets_bold(self):
        import docx
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "r.docx"
            drafting.md_to_docx("# Title\n\n## Section\n- a **bold** point\nplain line", out)
            self.assertTrue(out.exists())
            d = docx.Document(str(out))
            texts = [p.text for p in d.paragraphs]
            self.assertIn("Title", texts)
            self.assertTrue(any("bold point" in t for t in texts))

    def test_slug(self):
        self.assertEqual(drafting._slug("a16z (Games Fund)"), "a16z-games-fund")


class TestGenerateForRole(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(config, "APPLICATIONS_DIR", Path(self.tmp.name))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()
        self.conn.close()

    def test_generates_files_and_is_idempotent_and_grounded_note(self):
        job = _job(self.conn)
        with mock.patch.object(drafting.llm, "complete_json", return_value=FAKE_LLM) as m:
            paths = drafting.generate_for_role(self.conn, job, MASTER, VOICE, model="x")
        # grounding: the prompt fed the master profile as the source of facts
        prompt = m.call_args.args[0]
        self.assertIn("MASTER PROFILE", prompt)
        self.assertIn("Condé Nast", prompt)
        # files written (md + docx for both resume + cover)
        for key in ("resume_md", "resume_docx", "cover_md", "cover_docx"):
            self.assertTrue(Path(paths[key]).exists(), key)
        # honesty note (omitted requirements) recorded in the resume markdown, not invented into it
        self.assertIn("NOT claimed", Path(paths["resume_md"]).read_text())
        self.assertIn("10+ years enterprise SaaS", Path(paths["resume_md"]).read_text())
        # persisted + idempotent
        self.assertIsNotNone(store.get_draft(self.conn, job["id"]))
        with mock.patch.object(drafting.llm, "complete_json", side_effect=AssertionError("should skip")):
            self.assertIsNone(drafting.generate_for_role(self.conn, job, MASTER, VOICE, model="x"))
        # regenerate overwrites (calls the model again)
        with mock.patch.object(drafting.llm, "complete_json", return_value=FAKE_LLM):
            self.assertIsNotNone(drafting.generate_for_role(self.conn, job, MASTER, VOICE, model="x", regenerate=True))

    def test_run_drafts_counts(self):
        rows = [_job(self.conn, company=f"Co{i}", title=f"Role {i}", sid=str(100 + i)) for i in range(2)]
        with mock.patch.object(drafting.llm, "complete_json", return_value=FAKE_LLM):
            gen, skipped = drafting.run_drafts(self.conn, rows, MASTER, VOICE, model="x")
        self.assertEqual((gen, skipped), (2, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
