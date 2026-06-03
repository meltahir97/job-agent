"""Offline tests for the Bay-Area / US-remote location filter."""
import unittest

from job_agent.sources.location import location_decision as D


class TestLocationFilter(unittest.TestCase):
    def test_bay_area_kept_onsite(self):
        for loc in ("San Francisco, CA", "Oakland, CA", "San Jose, CA", "Palo Alto", "SF"):
            d = D(loc)
            self.assertTrue(d.keep, loc)
            self.assertIsNone(d.remote, loc)         # in-office -> remote unknown, not False

    def test_bay_area_with_remote_signal(self):
        d = D("San Francisco (Remote OK)")
        self.assertTrue(d.keep)
        self.assertTrue(d.remote)

    def test_remote_us_kept_true(self):
        for loc in ("Remote - US", "Remote (United States)", "Remote", "Remote - Americas"):
            d = D(loc)
            self.assertTrue(d.keep, loc)
            self.assertTrue(d.remote, loc)

    def test_remote_us_and_intl_kept(self):
        d = D("Remote - US & UK")                    # includes US -> keep
        self.assertTrue(d.keep)
        self.assertTrue(d.remote)

    def test_remote_non_us_dropped(self):
        for loc in ("Remote - Europe", "Remote (India)", "Remote, EMEA"):
            self.assertFalse(D(loc).keep, loc)

    def test_non_us_onsite_dropped(self):
        for loc in ("London, UK", "Toronto, Canada", "Bangalore, India", "Berlin"):
            self.assertFalse(D(loc).keep, loc)

    def test_us_non_bay_onsite_dropped(self):
        for loc in ("New York, NY", "Austin, TX", "Seattle, WA", "Boston, MA"):
            self.assertFalse(D(loc).keep, loc)

    def test_ambiguous_kept_with_null_remote(self):
        for loc in ("", None, "Galaxy HQ", "Earth"):
            d = D(loc)
            self.assertTrue(d.keep, repr(loc))
            self.assertIsNone(d.remote, repr(loc))

    def test_remote_flag_overrides_to_us_keep(self):
        d = D(None, remote=True)                     # remote, unknown location -> keep
        self.assertTrue(d.keep)
        self.assertTrue(d.remote)

    def test_remote_flag_but_non_us_location_dropped(self):
        self.assertFalse(D("London", remote=True).keep)


if __name__ == "__main__":
    unittest.main(verbosity=2)
