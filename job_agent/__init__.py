"""Personal job-discovery agent.

Two strictly separated layers:
  * data layer (plain Python, no LLM): fetch + normalize + store listings
  * reasoning layer (Claude via claude-agent-sdk): triage, score, explain
"""

__version__ = "0.1.0"
