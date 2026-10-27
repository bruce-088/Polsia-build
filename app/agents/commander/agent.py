"""Commander — top of the venture lifecycle.

Reads a venture's phase, gate status, Tracker metrics and Predictor scores,
then decides what happens next: which specialist to dispatch (the current
phase lead, Tracker, or Predictor) and whether to recommend advance, pivot,
kill or pause. Phase leads delegate onward to the worker agents.

Commander never changes a venture's phase or status. "advance" is only
passed through when the deterministic gate has passed on verified metrics,
and even then it becomes a RED founder decision.
"""
from app.agents.venture import lifecycle
from app.agents.venture.base import (
    VentureAgentBase,
    recompute_gate,
    snapshot_phase,
    venture_snapshot,
)

MAX_DISPATCH = 3
INACTIVE_STATUSES = ("paused", "killed", "graduated")


class CommanderAgent(VentureAgentBase):
    agent_type = "commander"
    role_title = "Commander"
    company_os_instructions = """Own venture-level prioritization: decide which
phase specialist, Tracker or Predictor runs next, and recommend continue,
advance, pivot, kill or pause from gate evidence. Delegate domain work to the
most specific specialist. Never change a venture's phase or status, approve
your own recommendation, bypass a failed or unverified gate, or report work as
done without evidence; advancement, pivots, kills and pauses are founder
decisions."""

    def run(self, task: dict, context: dict) -> dict:
        snapshot = venture_snapshot(task)
        phase = snapshot_phase(snapshot)
        gate = recompute_gate(snapshot)
        status = snapshot.get("status")
        if status in INACTIVE_STATUSES:
            return {
                "summary": f"Venture is {status}; Commander dispatched no work.",
                "phase": phase,
                "dispatch": [],
                "rejected_dispatch": [],
                "phase_recommendation": "continue",
                "founder_decisions": [],
                "guard_notes": [f"venture status {status!r} is inactive"],
                "gate": gate,
                "status": "proposal",
            }

        allowed = lifecycle.commander_dispatchable(phase)
        lead = lifecycle.get_phase(phase).lead_agent
        prompt = f"""{self.framing(task, context)}

You coordinate this venture. You may dispatch only these specialists:
- {lead}: the {lifecycle.get_phase(phase).name} lead; plans the phase and
  delegates to worker agents.
- tracker: turns new evidence into metric updates and flags data gaps.
- predictor: scores the idea (mode "score_idea") or forecasts the gate
  (mode "forecast").

Pick the one to three dispatches that most move the venture toward its gate
this cycle. Dispatch tracker when evidence exists that is not yet in the
metrics; dispatch predictor when the idea has no scores or the plan needs a
forecast. Recommend "advance" only if the gate status above says PASSED.

Respond with JSON only:
{{"summary": "<2-4 sentences>", "focus": "<this cycle's single priority>",
"dispatch": [{{"agent_type": "{lead}|tracker|predictor", "title": "<task>",
  "description": "<self-contained brief>", "mode": "<optional>"}}],
"phase_recommendation": "continue|advance|pivot|kill|pause",
"rationale": "<why, citing gate criteria>",
"founder_decisions": [{{"decision": "<RED decision>", "why": "<reason>"}}]}}"""
        result = self.call_claude_structured(prompt)
        decisions = self.clean_founder_decisions(result.get("founder_decisions"))
        dispatch, rejected = self.filter_delegations(result.get("dispatch"), allowed, MAX_DISPATCH)
        recommendation, notes = self.guard_recommendation(
            result.get("phase_recommendation"), gate, decisions
        )
        return {
            "summary": result.get("summary") or "No command plan generated.",
            "focus": result.get("focus"),
            "phase": phase,
            "dispatch": dispatch,
            "rejected_dispatch": rejected,
            "phase_recommendation": recommendation,
            "rationale": result.get("rationale"),
            "founder_decisions": decisions,
            "guard_notes": notes,
            "gate": gate,
            "status": "proposal",
        }
