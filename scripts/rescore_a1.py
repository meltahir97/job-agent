"""A1 re-score: judge every job against the Drive MASTER PROFILE, and report how
scores shifted vs. the previous (corp-dev-resume) scoring — i.e. roles that moved up
thanks to the broader background.

Run: ./.venv/bin/python scripts/rescore_a1.py
"""
import json
import sqlite3
import time

from job_agent import config, db, store
from job_agent.reasoning import profile as profile_mod, scoring
from job_agent.tiers import tier_for

RANK = {None: 0, "look": 1, "strong": 2}


def snapshot(conn):
    """Latest deep score per job, deduped by fingerprint (best per role)."""
    rows = conn.execute(
        """
        SELECT j.fingerprint, j.title, j.company, s.fit_score, s.label
        FROM jobs j JOIN scores s ON s.id = (
            SELECT id FROM scores s2 WHERE s2.job_id=j.id AND s2.stage='deep'
            ORDER BY s2.scored_at DESC, s2.id DESC LIMIT 1)
        LEFT JOIN feedback f ON f.job_id=j.id
        WHERE (f.decision IS NULL OR f.decision!='dismissed')
        """
    ).fetchall()
    out = {}
    for r in rows:
        fp = r["fingerprint"]
        if fp in out:
            continue
        out[fp] = {"title": r["title"], "company": r["company"],
                   "fit": r["fit_score"], "label": r["label"],
                   "tier": tier_for(r["fit_score"], r["label"])}
    return out


t0 = time.time()
conn = db.connect()
conn.row_factory = sqlite3.Row

before = snapshot(conn)
b_strong = sum(1 for v in before.values() if v["tier"] == "strong")
b_look = sum(1 for v in before.values() if v["tier"] == "look")
print(f"BEFORE: strong={b_strong} look={b_look} total={b_strong + b_look}", flush=True)

conn.execute("DELETE FROM scores")
conn.commit()
print(f"cleared scores; re-scoring against {config.MASTER_PROFILE_PATH.name}", flush=True)

prof = profile_mod.load_for_scoring(conn)
print(f"profile: {prof.get('name')} | threads={len(prof.get('experience_threads') or [])} "
      f"employers={len(prof.get('employers') or [])}", flush=True)
stats = scoring.run_scoring(conn, prof)
print("scoring:", stats, flush=True)

after = snapshot(conn)
a_strong = sum(1 for v in after.values() if v["tier"] == "strong")
a_look = sum(1 for v in after.values() if v["tier"] == "look")
print(f"AFTER:  strong={a_strong} look={a_look} total={a_strong + a_look}", flush=True)

# Movers: tier improved, or surfaced from nothing, or fit jumped >= 15.
movers = []
for fp, a in after.items():
    b = before.get(fp)
    b_rank = RANK[b["tier"]] if b else 0
    b_fit = (b["fit"] if b and b["fit"] is not None else 0)
    a_rank = RANK[a["tier"]]
    a_fit = a["fit"] if a["fit"] is not None else 0
    if a_rank > b_rank or (a_rank >= 1 and a_fit - b_fit >= 15):
        movers.append((a_fit - b_fit, b_fit, a_fit, b["tier"] if b else None, a["tier"],
                       a["company"], a["title"]))
movers.sort(key=lambda x: (RANK[x[4]], x[0]), reverse=True)

print(f"\nMOVERS UP ({len(movers)}): roles that rose with the broader profile", flush=True)
for delta, bf, af, bt, at_, co, title in movers[:25]:
    print(f"  {str(bt or '-'):>6} -> {at_:<6}  {bf:>3}->{af:<3}  {co} — {title}", flush=True)

json.dump({"before": {k: v for k, v in list(before.items())},
           "after": {k: v for k, v in list(after.items())}},
          open("/tmp/a1_scores.json", "w"))
print(f"\ndone in {time.time() - t0:.0f}s", flush=True)
conn.close()
