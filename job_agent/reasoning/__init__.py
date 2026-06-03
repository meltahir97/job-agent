"""Reasoning layer (Claude via claude-agent-sdk).

Operates ONLY on real records produced by the data layer. Every call here is
hermetic and tool-free (see llm.py) so the model cannot browse, read files, or
otherwise invent data.
"""
