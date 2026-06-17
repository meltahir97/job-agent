"""Offline tests for the OAuth Drive-writer (no network, no real Google calls)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from job_agent import config, db, oauth


class _Exec:
    def __init__(self, v):
        self._v = v

    def execute(self):
        return self._v


class _FakeFiles:
    """Records create() calls; returns canned ids/links. Folders 'don't exist' yet."""
    def __init__(self):
        self.created = []

    def create(self, body=None, media_body=None, fields=None, **k):
        self.created.append(body)
        fid = (body.get("name", "x").replace(" ", "_")[:12]) + str(len(self.created))
        return _Exec({"id": fid, "webViewLink": "https://doc/" + fid})

    def list(self, **k):
        return _Exec({"files": []})

    def get(self, **k):
        return _Exec({"id": "x"})

    def delete(self, **k):
        return _Exec({})


class _FakeSvc:
    def __init__(self):
        self._f = _FakeFiles()

    def files(self):
        return self._f


class TestOAuth(unittest.TestCase):
    def test_is_authorized_tracks_token_file(self):
        with tempfile.TemporaryDirectory() as td:
            tok = Path(td) / "token.json"
            with mock.patch.object(config, "GOOGLE_OAUTH_TOKEN_PATH", tok):
                self.assertFalse(oauth.is_authorized())
                tok.write_text("{}")
                self.assertTrue(oauth.is_authorized())

    def test_run_auth_flow_needs_client_secret(self):
        with mock.patch.object(config, "GOOGLE_OAUTH_CLIENT_SECRET", None):
            with self.assertRaises(oauth.OAuthError):
                oauth.run_auth_flow()

    def test_upload_drafts_without_token_raises(self):
        conn = db.connect(":memory:")
        db.init_db(conn)
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(config, "GOOGLE_OAUTH_TOKEN_PATH", Path(td) / "nope.json"):
                with self.assertRaises(oauth.OAuthError):
                    oauth.upload_drafts(conn, "Roku", "Director", b"R", b"C")
        conn.close()

    def test_upload_drafts_creates_folder_and_docs(self):
        conn = db.connect(":memory:")
        db.init_db(conn)
        fake = _FakeSvc()
        with mock.patch.object(oauth, "user_service", return_value=fake), \
             mock.patch.object(config, "GOOGLE_DRIVE_FOLDER_ID", None):
            links = oauth.upload_drafts(conn, "Roku", "Director, Corp Dev", b"RESUME", b"COVER")
        self.assertIn("drive.google.com/drive/folders/", links["folder"])
        self.assertTrue(links["resume_url"].startswith("https://doc/"))
        self.assertTrue(links["cover_url"].startswith("https://doc/"))
        self.assertIsNotNone(db.get_meta(conn, "drive_oauth_folder_id"))  # folder cached
        # created: app folder + role subfolder + 2 docs
        self.assertGreaterEqual(len(fake.files().created), 4)
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
