"""Offline tests for resume->profile caching and change-detection.

The LLM call and PDF extraction are mocked; the focus is the cost-saving cache
logic: build once, reuse on unchanged resume, rebuild on --force or file change.
"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from job_agent import config, db
from job_agent.reasoning import llm, profile

CANNED = {
    "name": "Test Candidate",
    "current_title": "Director of Strategy",
    "seniority": "Director",
    "years_experience": 10,
    "domains": ["Strategy", "Operations"],
    "skills": ["a", "b", "c"],
    "industries": ["Tech"],
    "education": ["MBA"],
    "target_titles": ["Director", "VP"],
    "dealbreakers": [],
    "nice_to_haves": [],
    "summary": "A strategy leader.",
}


class TestProfileCache(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        d = Path(self.tmp.name)
        self.resume = d / "resume.pdf"
        self.resume.write_bytes(b"resume version one " * 5)
        self.profile_dir = d / "profile"
        self.profile_path = self.profile_dir / "profile.json"
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)

        self.fake = mock.Mock(return_value=dict(CANNED))
        self.patches = [
            mock.patch.object(config, "RESUME_PATH", self.resume),
            mock.patch.object(config, "PROFILE_DIR", self.profile_dir),
            mock.patch.object(config, "PROFILE_PATH", self.profile_path),
            mock.patch.object(profile, "extract_resume_text", lambda p: "RESUME TEXT " * 10),
            mock.patch.object(llm, "complete_json", self.fake),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.conn.close()
        self.tmp.cleanup()

    def test_build_then_cache_then_force_then_change(self):
        # 1. first build calls the model and writes the cache
        p1 = profile.load_or_build(self.conn)
        self.assertEqual(p1["name"], "Test Candidate")
        self.assertEqual(self.fake.call_count, 1)
        self.assertTrue(self.profile_path.exists())
        self.assertEqual(db.get_meta(self.conn, "resume_hash"), profile.file_hash(self.resume))

        # 2. unchanged resume -> served from cache, no new model call
        profile.load_or_build(self.conn)
        self.assertEqual(self.fake.call_count, 1)

        # 3. --force -> rebuild
        profile.load_or_build(self.conn, force=True)
        self.assertEqual(self.fake.call_count, 2)

        # 4. resume file changes -> hash differs -> rebuild
        self.resume.write_bytes(b"resume version TWO is different " * 3)
        profile.load_or_build(self.conn)
        self.assertEqual(self.fake.call_count, 3)

    def test_cache_file_is_valid_json_with_meta(self):
        profile.load_or_build(self.conn)
        data = json.loads(self.profile_path.read_text())
        self.assertIn("_meta", data)
        self.assertEqual(data["_meta"]["resume_hash"], profile.file_hash(self.resume))

    def test_missing_resume_raises(self):
        self.resume.unlink()
        with self.assertRaises(FileNotFoundError):
            profile.load_or_build(self.conn)


if __name__ == "__main__":
    unittest.main(verbosity=2)
