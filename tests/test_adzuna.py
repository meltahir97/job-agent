"""Offline tests for AdzunaSource normalization + job upsert/dedup.

No network and no API keys: AdzunaSource._request is monkeypatched to return a
captured, realistic Adzuna payload.

Run:  python -m unittest tests.test_adzuna   (from the project root, in the venv)
"""
import unittest

from job_agent import db, store
from job_agent.sources.adzuna import AdzunaSource
from job_agent.sources.base import JobQuery

# A realistic Adzuna /search response shape, covering the cases we care about.
SAMPLE = {
    "count": 4,
    "results": [
        {  # 0: full record, real (non-predicted) salary
            "id": "1111",
            "title": "Director of Corporate Development",
            "company": {"display_name": "Acme Corp"},
            "location": {"display_name": "San Francisco, CA", "area": ["US", "California"]},
            "description": "Lead M&A and strategic partnerships.",
            "redirect_url": "https://www.adzuna.com/land/ad/1111",
            "salary_min": 180000,
            "salary_max": 240000,
            "salary_is_predicted": "0",
            "category": {"label": "Consultancy Jobs", "tag": "consultancy-jobs"},
            "contract_type": "permanent",
            "created": "2026-05-20T12:00:00Z",
        },
        {  # 1: predicted salary -> must be dropped to null
            "id": "2222",
            "title": "VP, Business Operations",
            "company": {"display_name": "Globex"},
            "location": {"display_name": "Oakland, CA"},
            "description": "Own GTM operations.",
            "redirect_url": "https://www.adzuna.com/land/ad/2222",
            "salary_min": 150000,
            "salary_max": 200000,
            "salary_is_predicted": "1",
            "created": "2026-05-21T09:30:00Z",
        },
        {  # 2: remote signalled in the title; missing salary entirely
            "id": "3333",
            "title": "Head of Strategy (Remote)",
            "company": {"display_name": "Initech"},
            "location": {"display_name": "United States"},
            "description": "Set company strategy.",
            "redirect_url": "https://www.adzuna.com/land/ad/3333",
            "created": "2026-05-22T08:00:00Z",
        },
        {  # 3: sparse record -> nulls, must not crash
            "id": "4444",
            "title": "Operations Manager",
            "created": "2026-05-23T08:00:00Z",
        },
    ],
}


def _source():
    src = AdzunaSource("dummy_id", "dummy_key", country="us")
    src._request = lambda query, page, per_page: SAMPLE  # type: ignore[assignment]
    return src


class TestNormalize(unittest.TestCase):
    def setUp(self):
        self.jobs = _source().fetch(JobQuery(keywords="x", max_results=50))

    def test_count(self):
        self.assertEqual(len(self.jobs), 4)

    def test_real_salary_mapped_with_currency(self):
        j = self.jobs[0]
        self.assertEqual((j.salary_min, j.salary_max, j.salary_currency), (180000.0, 240000.0, "USD"))
        self.assertEqual(j.company, "Acme Corp")
        self.assertEqual(j.url, "https://www.adzuna.com/land/ad/1111")
        self.assertEqual(j.category, "Consultancy Jobs")

    def test_predicted_salary_dropped(self):
        j = self.jobs[1]
        self.assertIsNone(j.salary_min)
        self.assertIsNone(j.salary_max)
        self.assertIsNone(j.salary_currency)

    def test_remote_detected_true_else_none(self):
        self.assertTrue(self.jobs[2].remote)          # "Remote" in title
        self.assertIsNone(self.jobs[0].remote)        # no remote signal -> unknown

    def test_sparse_record_is_all_null_not_crash(self):
        j = self.jobs[3]
        self.assertEqual(j.title, "Operations Manager")
        self.assertIsNone(j.company)
        self.assertIsNone(j.url)
        self.assertIsNone(j.salary_min)

    def test_fingerprint_stable_and_distinct(self):
        self.assertEqual(self.jobs[0].fingerprint, self.jobs[0].fingerprint)
        self.assertNotEqual(self.jobs[0].fingerprint, self.jobs[1].fingerprint)


class TestUpsertDedup(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.jobs = _source().fetch(JobQuery(keywords="x", max_results=50))

    def tearDown(self):
        self.conn.close()

    def test_insert_then_reinsert_is_idempotent(self):
        first = [store.upsert_job(self.conn, j)[1] for j in self.jobs]
        self.assertEqual(first, [True, True, True, True])      # all new
        self.assertEqual(store.count_jobs(self.conn), 4)

        second = [store.upsert_job(self.conn, j)[1] for j in self.jobs]
        self.assertEqual(second, [False, False, False, False])  # all seen
        self.assertEqual(store.count_jobs(self.conn), 4)        # no duplicates

    def test_first_seen_preserved_on_update(self):
        job_id, _ = store.upsert_job(self.conn, self.jobs[0])
        before = self.conn.execute(
            "SELECT first_seen_at, last_seen_at FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        store.upsert_job(self.conn, self.jobs[0])
        after = self.conn.execute(
            "SELECT first_seen_at, last_seen_at FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        self.assertEqual(before["first_seen_at"], after["first_seen_at"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
