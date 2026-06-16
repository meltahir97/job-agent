"""One-off: clear all scores and re-score from scratch with the updated prompts
(credential disqualifiers in triage + deep; pros/cons bullet output).

Keeps jobs / notifications / feedback intact so NEW-state and dismissals survive.
Run: ./.venv/bin/python scripts/rescore.py
"""
import json
import sqlite3
import time

from job_agent import config, store
from job_agent.reasoning import scoring
from job_agent.tiers import tier_for

t0 = time.time()
conn = sqlite3.connect(config.DB_PATH)
conn.row_factory = sqlite3.Row

before = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
conn.execute("DELETE FROM scores")
conn.commit()
print(f"cleared {before} scores", flush=True)

profile = json.loads(config.PROFILE_PATH.read_text(encoding="utf-8"))
stats = scoring.run_scoring(conn, profile)
print("scoring:", stats, flush=True)

# Tier counts over the latest deep score per job (deduped by fingerprint).
rows = conn.execute(
    """
    SELECT j.fingerprint, s.fit_score, s.label
    FROM jobs j JOIN scores s ON s.id = (
        SELECT id FROM scores s2 WHERE s2.job_id=j.id AND s2.stage='deep'
        ORDER BY s2.scored_at DESC, s2.id DESC LIMIT 1)
    LEFT JOIN feedback f ON f.job_id=j.id
    WHERE (f.decision IS NULL OR f.decision != 'dismissed')
    """
).fetchall()
seen, strong, look = set(), 0, 0
for r in rows:
    if r["fingerprint"] in seen:
        continue
    seen.add(r["fingerprint"])
    t = tier_for(r["fit_score"], r["label"])
    if t == "strong":
        strong += 1
    elif t == "look":
        look += 1
print(f"TIERS  strong={strong}  look={look}  total={strong+look}", flush=True)
print(f"done in {time.time()-t0:.0f}s", flush=True)
conn.close()
