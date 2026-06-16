"""Offline tests for the email nudge content + send gating (no SMTP/network)."""
import unittest
from unittest import mock

from job_agent import config, notify

STATS = {"new": 3, "strong": 2, "look": 5, "companies": 4}


class TestNudge(unittest.TestCase):
    def test_render_includes_drafts_and_proposals(self):
        subj, body = notify.render_nudge(STATS, "https://site", ["Acme – Strategy Lead (80)"],
                                         drafted_roles=["Acme – Strategy Lead"], proposals=2)
        self.assertIn("3 new role(s)", subj)
        self.assertIn("Top 3:", body)
        self.assertIn("Drafts ready for:", body)
        self.assertIn("Acme – Strategy Lead", body)
        self.assertIn("New company proposals: 2", body)
        self.assertIn("https://site", body)

    def test_subject_switches_to_proposals_when_no_new(self):
        subj, _ = notify.render_nudge({"new": 0, "strong": 0, "look": 0}, "", [], proposals=4)
        self.assertIn("4 new company proposal", subj)

    def test_send_skips_when_nothing_new_or_proposed(self):
        with mock.patch.object(config, "SMTP_USER", "u@x"), \
             mock.patch.object(config, "SMTP_APP_PASSWORD", "pw"), \
             mock.patch.object(config, "NOTIFY_EMAIL", "to@x"):
            msg = notify.send_nudge({"new": 0, "strong": 0, "look": 0}, "", [], proposals=0)
        self.assertIn("0 new roles, 0 proposals", msg)

    def test_send_skips_when_no_creds(self):
        with mock.patch.object(config, "SMTP_USER", None), \
             mock.patch.object(config, "SMTP_APP_PASSWORD", None):
            msg = notify.send_nudge({"new": 5, "strong": 1, "look": 1}, "", [], proposals=0)
        self.assertIn("SMTP", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
