"""Venture lifecycle layer — Commander, Tracker, Predictor and the four phase
leads (Research & Validation, Launch, Marketing, Scaling).

This layer sits *above* the existing operating agents. Phase leads plan and
delegate; the existing agents (competitor_research, email_outreach, ...) stay
the workers. Everything here is proposal-only: no agent in this package sends,
posts, spends, or changes a venture's phase. Phase changes go through a RED
founder approval and a deterministic gate re-check (see venture_service).
"""
