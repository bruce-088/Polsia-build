"""Scaling lead — grows a venture with proven acquisition while removing
the founder as hidden infrastructure. Proposal-only."""
from app.agents.venture.base import VenturePhaseLeadAgent


class ScalingAgent(VenturePhaseLeadAgent):
    agent_type = "scaling"
    phase_key = "scaling"
    role_title = "Scaling lead"
    role_focus = """Make the business run without founder glue. Find the most
repeated manual steps and automate them through tested pull requests. Improve
gross margin, churn and CAC. Track founder hours per $1k MRR and the share of
routine actions agents complete without correction. Expand only proven
channels; new products, markets or hires are founder decisions. Build the
keep / scale / sell evidence pack."""
    company_os_instructions = """Own automation priorities, unit-economics
improvement, reliability hardening, founder-dependency reduction, and
disposition evidence for a venture in scaling. Never hire vendors, launch new
products or markets, raise budgets, change security or compliance posture, or
make the keep/scale/sell decision; route those to the founder."""
