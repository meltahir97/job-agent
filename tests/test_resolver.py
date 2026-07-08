"""Offline tests for the auto-resolver + unresolved reporting (probe mocked)."""
import unittest
from unittest import mock

from job_agent.companies import Company
from job_agent.sources import ats as ats_mod
from job_agent.sources import resolver


class TestCandidateSlugs(unittest.TestCase):
    def test_single_word(self):
        self.assertEqual(resolver.candidate_slugs("Notion"), ["notion"])

    def test_strips_suffixes_and_offers_variants(self):
        cands = resolver.candidate_slugs("Acme Corp Inc")
        self.assertIn("acme", cands)          # suffixes dropped
        self.assertIn("acmecorpinc", cands)   # full concatenation kept too

    def test_hyphenates_multiword(self):
        self.assertIn("foo-bar", resolver.candidate_slugs("Foo Bar"))


class TestResolve(unittest.TestCase):
    def test_explicit_config_is_trusted_without_probing(self):
        with mock.patch.object(ats_mod, "probe", side_effect=AssertionError("should not probe")):
            r = resolver.resolve_company(Company("X", "greenhouse", "xboard"), session=None)
        self.assertEqual((r.status, r.ats, r.slug), ("configured", "greenhouse", "xboard"))
        self.assertTrue(r.ok)

    def test_auto_resolves_to_first_responding_board(self):
        def fake_probe(ats, slug, session, timeout):
            return 7 if (ats, slug) == ("lever", "acme") else None

        with mock.patch.object(ats_mod, "probe", side_effect=fake_probe):
            r = resolver.resolve_company(Company("Acme", "auto"), session=None)
        self.assertEqual((r.status, r.ats, r.slug, r.n_jobs), ("resolved", "lever", "acme", 7))
        self.assertTrue(r.ok)

    def test_auto_unresolved_when_nothing_matches(self):
        with mock.patch.object(ats_mod, "probe", return_value=None):
            r = resolver.resolve_company(Company("Ghost Co", "auto"), session=None)
        self.assertEqual((r.status, r.ats, r.slug), ("unresolved", None, None))
        self.assertFalse(r.ok)
        self.assertIn("manually", r.detail)

    def test_workable_empty_board_not_trusted(self):
        # Workable 200+empty (n=0) must NOT count as a match (no 404 to disprove it).
        with mock.patch.object(ats_mod, "probe", side_effect=lambda ats, *a: 0 if ats == "workable" else None):
            r = resolver.resolve_company(Company("Maybe", "auto"), session=None)
        self.assertEqual(r.status, "unresolved")

    def test_workable_with_jobs_is_trusted(self):
        with mock.patch.object(ats_mod, "probe", side_effect=lambda ats, *a: 5 if ats == "workable" else None):
            r = resolver.resolve_company(Company("Maybe", "auto"), session=None)
        self.assertEqual((r.status, r.ats, r.n_jobs), ("resolved", "workable", 5))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestOwnershipGate(unittest.TestCase):
    """A live board that belongs to a DIFFERENT company must never be accepted
    (regression: 'Thrive Fantasy' matched an unrelated 'thrive' Greenhouse board)."""

    def test_no_bare_first_word_candidate(self):
        self.assertNotIn("thrive", resolver.candidate_slugs("Thrive Fantasy"))
        self.assertIn("thrivefantasy", resolver.candidate_slugs("Thrive Fantasy"))

    def test_same_company_gate(self):
        self.assertFalse(resolver._same_company("Thrive Fantasy", "THRIVE"))
        self.assertTrue(resolver._same_company("Crunchyroll (Sony)", "Crunchyroll, LLC"))
        self.assertTrue(resolver._same_company("PlayStation", "PlayStation Global"))
        self.assertTrue(resolver._same_company("Fandom (formerly Wikia)", "Fandom"))
        self.assertTrue(resolver._same_company("Whatnot", None))  # unverifiable -> allowed
