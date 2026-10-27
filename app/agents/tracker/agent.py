"""Tracker — the venture's scorekeeper (shared across all phases).

Turns supplied evidence (task description and task_metadata.evidence) into
proposed metric updates, and flags anomalies and data gaps. Every update must
name a numeric value and a source. Tracker cannot verify its own numbers:
all of its updates are recorded unverified, and unverified metrics never pass
a gate until the founder or a trusted integration confirms them.
"""
import json

from app.agents.venture import lifecycle
from app.agents.venture.base import VentureAgentBase

MAX_UPDATES = 20


class TrackerAgent(VentureAgentBase):
    agent_type = "tracker"
    role_title = "Tracker"
    company_os_instructions = """Own venture metric bookkeeping: extract
metric values from supplied evidence, cite the source of each, and flag
anomalies and missing data. Never invent, estimate, round up, or carry forward
a metric without evidence, mark a metric verified, or alter gate thresholds."""

    def run(self, task: dict, context: dict) -> dict:
        metadata = task.get("task_metadata") or {}
        evidence = metadata.get("evidence") or []
        gate_metrics = sorted(
            {c.metric for phase in lifecycle.PHASES.values() for c in phase.exit_gate}
        )
        prompt = f"""{self.framing(task, context)}

Evidence supplied for this run:
{json.dumps(evidence, indent=2, default=str)}

Extract metric values that the evidence directly supports. Prefer these
gate metric names when they apply: {", ".join(gate_metrics)}. Other metric
names must be snake_case. Rates are fractions (5% = 0.05). Money is USD.
If the evidence does not support a number, list it under data_gaps instead.

Respond with JSON only:
{{"summary": "<1-3 sentences>",
"metric_updates": [{{"metric": "<name>", "value": <number>,
  "source": "<where it came from: URL, system, or document>",
  "evidence": "<the specific fact that supports it>"}}],
"anomalies": ["<something that looks wrong or surprising>"],
"data_gaps": ["<metric that matters but lacks evidence>"]}}"""
        result = self.call_claude_structured(prompt)
        return self.postprocess(result)

    @staticmethod
    def postprocess(result: dict) -> dict:
        accepted, rejected = [], []
        for item in result.get("metric_updates") if isinstance(result.get("metric_updates"), list) else []:
            if not isinstance(item, dict):
                rejected.append({"item": item, "reason": "not an object"})
                continue
            metric = item.get("metric")
            value = lifecycle.coerce_metric_value(item.get("value"))
            source = str(item.get("source") or "").strip()
            if not lifecycle.is_valid_metric_name(metric):
                rejected.append({**item, "reason": "invalid metric name"})
            elif value is None:
                rejected.append({**item, "reason": "value is not a finite number"})
            elif not source:
                rejected.append({**item, "reason": "no source cited"})
            elif len(accepted) >= MAX_UPDATES:
                rejected.append({**item, "reason": f"over the {MAX_UPDATES}-update limit"})
            else:
                accepted.append(
                    {
                        "metric": metric,
                        "value": value,
                        "source": source[:500],
                        "evidence": str(item.get("evidence") or "")[:1000],
                        "verified": False,
                    }
                )
        return {
            "summary": result.get("summary") or "No metric updates generated.",
            "metric_updates": accepted,
            "rejected_updates": rejected,
            "anomalies": result.get("anomalies") if isinstance(result.get("anomalies"), list) else [],
            "data_gaps": result.get("data_gaps") if isinstance(result.get("data_gaps"), list) else [],
            "status": "proposal",
        }
