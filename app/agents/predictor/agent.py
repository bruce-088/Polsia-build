"""Predictor — forecasting and idea scoring (shared across all phases).

Modes (task_metadata.mode):
- "forecast" (default): probability the venture passes its current gate
  within a horizon, with drivers, leading indicators and assumptions.
- "score_idea": 0-5 scores on the lifecycle idea rubric. The weighted
  total is computed deterministically by lifecycle.score_idea — the model
  supplies judgments, never the arithmetic.

Everything Predictor returns is labelled a forecast, not a fact.
"""
from app.agents.venture import lifecycle
from app.agents.venture.base import (
    VentureAgentBase,
    recompute_gate,
    task_mode,
    venture_snapshot,
)

CONFIDENCE = ("low", "medium", "high")


class PredictorAgent(VentureAgentBase):
    agent_type = "predictor"
    role_title = "Predictor"
    company_os_instructions = """Own forecasts and idea scoring: estimate gate
pass probability, project leading indicators, and score ideas on the rubric
with stated assumptions and confidence. Present every output as a forecast,
never as observed fact. Never record metrics, change gates, or recommend
committing money on a forecast alone."""

    def run(self, task: dict, context: dict) -> dict:
        if task_mode(task, "forecast") == "score_idea":
            return self.score(task, context)
        return self.forecast(task, context)

    def score(self, task: dict, context: dict) -> dict:
        rubric = "\n".join(
            f"- {name} (weight {weight:.2f}): {description}"
            for name, (weight, description) in lifecycle.IDEA_RUBRIC.items()
        )
        prompt = f"""{self.framing(task, context)}

Score this idea 0-5 on each rubric criterion (0 = terrible, 5 = excellent),
with a one-sentence rationale and your confidence. Base scores on evidence in
the snapshot where it exists, and say when you are assuming.
{rubric}

Respond with JSON only:
{{"summary": "<1-3 sentences>",
"scores": {{"<criterion>": {{"score": <0-5>, "rationale": "<why>",
  "confidence": "low|medium|high"}}}},
"kill_signals": ["<evidence that would make you drop this idea>"],
"key_assumptions": ["<assumption the scores depend on>"]}}"""
        result = self.call_claude_structured(prompt)
        raw_scores = result.get("scores") if isinstance(result.get("scores"), dict) else {}
        computed = lifecycle.score_idea(raw_scores)
        for name, entry in computed["criteria"].items():
            source = raw_scores.get(name) if isinstance(raw_scores.get(name), dict) else {}
            entry["rationale"] = str(source.get("rationale") or "")
            confidence = source.get("confidence")
            entry["confidence"] = confidence if confidence in CONFIDENCE else "low"
        return {
            "summary": result.get("summary") or "No idea score generated.",
            "mode": "score_idea",
            "forecast": True,
            "idea_score": computed,
            "kill_signals": result.get("kill_signals") if isinstance(result.get("kill_signals"), list) else [],
            "key_assumptions": result.get("key_assumptions")
            if isinstance(result.get("key_assumptions"), list)
            else [],
            "status": "proposal",
        }

    def forecast(self, task: dict, context: dict) -> dict:
        snapshot = venture_snapshot(task)
        gate = recompute_gate(snapshot)
        prompt = f"""{self.framing(task, context)}

Forecast whether this venture passes its {gate['gate']} gate. Reason from the
recorded metrics and their trend, not from optimism. Missing or unverified
criteria lower the probability.

Respond with JSON only:
{{"summary": "<1-3 sentences>",
"gate_pass_probability": <0.0-1.0>, "horizon_days": <integer>,
"drivers": ["<what most moves the probability>"],
"leading_indicators": ["<metric to watch weekly and its target>"],
"assumptions": ["<assumption>"],
"confidence": "low|medium|high"}}"""
        result = self.call_claude_structured(prompt)
        probability = lifecycle.coerce_metric_value(result.get("gate_pass_probability"))
        if probability is not None:
            probability = min(max(probability, 0.0), 1.0)
        horizon = lifecycle.coerce_metric_value(result.get("horizon_days"))
        confidence = result.get("confidence")

        def _list(key: str) -> list:
            return result.get(key) if isinstance(result.get(key), list) else []

        return {
            "summary": result.get("summary") or "No forecast generated.",
            "mode": "forecast",
            "forecast": True,
            "gate": gate["gate"],
            "gate_passed_now": gate["passed"],
            "gate_pass_probability": probability,
            "horizon_days": int(horizon) if horizon is not None and horizon > 0 else None,
            "drivers": _list("drivers"),
            "leading_indicators": _list("leading_indicators"),
            "assumptions": _list("assumptions"),
            "confidence": confidence if confidence in CONFIDENCE else "low",
            "status": "proposal",
        }
