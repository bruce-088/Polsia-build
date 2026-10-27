"""Research & Validation lead — the first phase of the venture lifecycle.

Two modes (task_metadata.mode):
- "plan" (default): plan validation work for one venture and delegate it.
- "ideate": generate candidate venture ideas against the founder's
  constraints, for Predictor to score and the founder to shortlist.

Proposal-only; never publishes a smoke test, contacts anyone, or spends.
"""
from app.agents.venture.base import VenturePhaseLeadAgent, task_mode

MAX_IDEAS = 10

IDEA_FIELDS = (
    "name",
    "one_liner",
    "target_customer",
    "business_model",
    "agent_fulfillment",
    "acquisition_channel",
    "smoke_test",
    "riskiest_assumption",
)


class ResearchValidationAgent(VenturePhaseLeadAgent):
    agent_type = "research_validation"
    phase_key = "research_validation"
    role_title = "Research & Validation lead"
    role_focus = """Kill bad ideas cheaply and early. Rank assumptions by risk,
test the riskiest first, and set every pass threshold before a test runs.
Problem evidence must be independent and citable. Interest (likes, page views)
is not commitment; only pre-orders, deposits, LOIs or booked calls count as
committed signals. If the evidence says kill or pivot, say so plainly."""
    company_os_instructions = """Own market and problem research, competitor
mapping, ICP definition, smoke-test design, and the proceed/pivot/kill
recommendation for a venture in validation. Ground every claim in cited
evidence and label hypotheses as hypotheses. Never publish pages, contact
people, collect payments, spend money, fabricate demand evidence, or advance a
venture's phase; route those to the founder."""

    def run(self, task: dict, context: dict) -> dict:
        if task_mode(task, "plan") == "ideate":
            return self.ideate(task, context)
        return super().run(task, context)

    def ideate(self, task: dict, context: dict) -> dict:
        prompt = f"""{self.framing(task, context)}

Generate up to {MAX_IDEAS} distinct venture ideas that satisfy the constraints
in the task. Each must be fully online, deliverable mostly by AI agents with
the founder approving only consequential decisions, low in regulatory and
licensing exposure, and testable with a smoke test in three weeks or less.
Do not claim market sizes or demand you cannot cite; state assumptions.

Respond with JSON only:
{{"summary": "<1-2 sentences>",
"ideas": [{{"name": "<short name>", "one_liner": "<what it is>",
  "target_customer": "<who pays>", "business_model": "<price and model>",
  "agent_fulfillment": "<what agents do day to day>",
  "acquisition_channel": "<how agents reach buyers online>",
  "smoke_test": "<the 2-3 week test and its pass threshold>",
  "riskiest_assumption": "<what would kill it>"}}]}}"""
        result = self.call_claude_structured(prompt)
        ideas = []
        seen = set()
        for item in result.get("ideas") if isinstance(result.get("ideas"), list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            ideas.append({field: str(item.get(field) or "").strip() for field in IDEA_FIELDS})
            if len(ideas) >= MAX_IDEAS:
                break
        return {
            "summary": result.get("summary") or "No ideas generated.",
            "mode": "ideate",
            "ideas": ideas,
            "status": "proposal",
        }
