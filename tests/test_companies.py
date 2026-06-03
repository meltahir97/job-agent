"""Offline tests for the companies.yaml loader + validation."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from job_agent.companies import CompaniesError, Company, load_companies


def _write(tmp: Path, text: str) -> Path:
    p = tmp / "companies.yaml"
    p.write_text(text, encoding="utf-8")
    return p


class TestCompaniesLoader(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_loads_valid_mix(self):
        p = _write(self.d, """
companies:
  - name: Stripe
    ats: greenhouse
    slug: stripe
  - name: Notion
    ats: auto
""")
        cos = load_companies(p)
        self.assertEqual(cos[0], Company("Stripe", "greenhouse", "stripe"))
        self.assertEqual(cos[1], Company("Notion", "auto", None))

    def test_defaults_ats_to_auto(self):
        p = _write(self.d, "companies:\n  - name: Acme\n")
        self.assertEqual(load_companies(p)[0], Company("Acme", "auto", None))

    def test_missing_name_raises(self):
        p = _write(self.d, "companies:\n  - ats: lever\n    slug: x\n")
        with self.assertRaises(CompaniesError):
            load_companies(p)

    def test_invalid_ats_raises(self):
        p = _write(self.d, "companies:\n  - name: Acme\n    ats: bamboo\n    slug: acme\n")
        with self.assertRaises(CompaniesError):
            load_companies(p)

    def test_explicit_ats_requires_slug(self):
        p = _write(self.d, "companies:\n  - name: Acme\n    ats: greenhouse\n")
        with self.assertRaises(CompaniesError):
            load_companies(p)

    def test_empty_file_raises(self):
        p = _write(self.d, "companies: []\n")
        with self.assertRaises(CompaniesError):
            load_companies(p)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_companies(self.d / "nope.yaml")


if __name__ == "__main__":
    unittest.main(verbosity=2)
