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
    "skills": ["M&A", "Strategy"], "education": ["MIT BS, Mechanical Engineering"],
}
VOICE = {"tone": "warm, precise", "characteristic_phrases": ["I am drawn to"], "approximate": False}

FAKE_DRAFT = """===RESUME===
NAME: Muhammad Eltahir
CONTACT: muhammad.e.eltahir@gmail.com  ·  New York, NY
## EXPERIENCE
@ Condé Nast :: Jul 2021 – Present
> Corp Dev Manager
- Led M&A integration
## EDUCATION
@ MIT :: 2019
> B.S. Mechanical Engineering
## SKILLS
- M&A  ·  Strategy
===COVER_LETTER===
Dear OpenAI,

I am drawn to your mission. My corp-dev work fits this role.

Sincerely,
Muhammad
===OMITTED===
- 10+ years enterprise SaaS
"""


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

    def test_local_files_idempotent_and_grounded_note(self):
        job = _job(self.conn)
        with mock.patch.object(drafting.llm, "complete_text", return_value=FAKE_DRAFT) as m:
            res = drafting.generate_for_role(self.conn, job, MASTER, VOICE, model="x", to_drive=False)
        # grounding: the prompt fed the master profile as the source of facts
        prompt = m.call_args.args[0]
        self.assertIn("MASTER PROFILE", prompt)
        self.assertIn("Condé Nast", prompt)
        self.assertEqual(res["where"], "local")
        folder = Path(res["folder"])
        for name in ("resume.md", "resume.docx", "cover_letter.md", "cover_letter.docx"):
            self.assertTrue((folder / name).exists(), name)
        # honesty note (omitted requirements) recorded in the resume markdown, not invented in
        resume_md = (folder / "resume.md").read_text()
        self.assertIn("NOT claimed", resume_md)
        self.assertIn("10+ years enterprise SaaS", resume_md)
        # persisted + idempotent
        self.assertIsNotNone(store.get_draft(self.conn, job["id"]))
        with mock.patch.object(drafting.llm, "complete_text", side_effect=AssertionError("should skip")):
            self.assertIsNone(drafting.generate_for_role(self.conn, job, MASTER, VOICE, model="x", to_drive=False))
        with mock.patch.object(drafting.llm, "complete_text", return_value=FAKE_DRAFT):
            self.assertIsNotNone(drafting.generate_for_role(self.conn, job, MASTER, VOICE, model="x",
                                                            regenerate=True, to_drive=False))

    def test_drive_path_uploads_and_records_links(self):
        job = _job(self.conn)
        links = {"folder": "https://drive.google.com/drive/folders/sub456",
                 "resume_url": "https://docs/r", "cover_url": "https://docs/c"}
        with mock.patch.object(drafting.llm, "complete_text", return_value=FAKE_DRAFT), \
             mock.patch.object(drafting.oauth, "upload_drafts", return_value=links) as up:
            res = drafting.generate_for_role(self.conn, job, MASTER, VOICE, model="x")  # to_drive default
        self.assertEqual(res["where"], "drive")
        self.assertIn("sub456", res["folder"])
        # the docx bytes (not markdown) were handed to the uploader
        self.assertEqual(up.call_args.args[1], job["company"])
        d = store.get_draft(self.conn, job["id"])
        self.assertEqual(d["resume_url"], "https://docs/r")
        self.assertEqual(d["cover_url"], "https://docs/c")
        self.assertIn("sub456", d["drive_url"])

    def test_run_drafts_counts(self):
        rows = [_job(self.conn, company=f"Co{i}", title=f"Role {i}", sid=str(100 + i)) for i in range(2)]
        with mock.patch.object(drafting.llm, "complete_text", return_value=FAKE_DRAFT):
            gen, skipped = drafting.run_drafts(self.conn, rows, MASTER, VOICE, model="x", to_drive=False)
        self.assertEqual((gen, skipped), (2, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
