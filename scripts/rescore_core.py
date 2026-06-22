"""Re-score everything with the CORE-FUNCTION priority (corp dev / M&A / strategy /
operations / BD / partnerships first; non-core only on high conviction). Also refreshes
Google with the new function-targeted queries. Reports the new composition.
"""
import re
import sqlite3
import time
from collections import Counter

from job_agent import db, store, website
from job_agent.companies import load_companies
from job_agent.reasoning import profile as profile_mod, scoring
from job_agent.sources.watchlist import WatchlistSource

t0 = time.time()
conn = db.connect()
conn.row_factory = sqlite3.Row
db.init_db(conn)

# 1. refresh Google with the new function-targeted queries
google = [c for c in load_companies() if c.ats == "google"]
gjobs, _ = WatchlistSource(google).collect()
gnew = sum(int(store.upsert_job(conn, j)[1]) for j in gjobs)
print(f"Google refreshed: {len(gjobs)} in-scope, {gnew} new", flush=True)

# 2. clear scores + re-score all with the core-function prompts
before = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
conn.execute("DELETE FROM scores")
conn.commit()
print(f"cleared {before} scores; re-scoring all jobs with core-function priority…", flush=True)
prof = profile_mod.load_for_scoring(conn)
stats = scoring.run_scoring(conn, prof)
print("scoring:", stats, flush=True)

# 3. report composition
rows = website.select_master(conn)
strong = sum(1 for r in rows if (r["fit_score"] or 0) >= 75)
look = sum(1 for r in rows if 30 <= (r["fit_score"] or 0) < 75)
print(f"\nSURFACED: {len(rows)}  (strong {strong} / worth-a-look {look})", flush=True)

CORE = re.compile(
    r"strateg|operations|operating|partnership|business development|corporate development|"
    r"\bm&a\b|\bdeals?\b|alliance|ventures|\bbd\b|go.?to.?market|\bgtm\b|chief of staff|"
    r"corp dev|dealmaking|investment", re.I)
core_rows = [r for r in rows if CORE.search(r["title"] or "")]
print(f"core-function titles: {len(core_rows)}/{len(rows)} ({100 * len(core_rows) // max(1, len(rows))}%)", flush=True)

print("\nby company:", flush=True)
for co, n in Counter(r["company"] for r in rows).most_common():
    print(f"  {n:3}  {co}", flush=True)

print("\nNON-CORE-title roles that still surfaced (should be few, high-conviction):", flush=True)
for r in sorted([r for r in rows if not CORE.search(r["title"] or "")], key=lambda x: -(x["fit_score"] or 0))[:20]:
    print(f"  [{r['fit_score']}] {r['company']} — {r['title'][:55]}", flush=True)

print(f"\ndone {time.time() - t0:.0f}s", flush=True)
conn.close()
