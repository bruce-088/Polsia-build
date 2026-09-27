"""Stage 2 structured decisions; execution requires a separate run authorization.

Tests inject the structured transport and never invoke a real provider.
"""
from __future__ import annotations

import json
from copy import deepcopy

from jsonschema import Draft202012Validator

from app.agents.company_os_contract import CompanyOSContractError


class CompanyOSTransportError(CompanyOSContractError):
    """The provider was unavailable or could not complete a transport request."""


class CompanyOSMalformedOutputError(CompanyOSContractError):
    """The provider completed but did not produce a valid structured decision."""


class CompanyOSDecisionMethodDefect(CompanyOSContractError):
    """A defect in decision-method code, distinct from provider failure."""


class ScriptedProviderTimeout(CompanyOSContractError):
    """Explicit synthetic timeout; never a real provider outage."""


def stage2_json_schema(context: dict) -> dict:
    string = {"type": "string", "minLength": 1}
    properties = {
        "action": {"enum": context["canonical_actions"]},
        "risk_level": {"enum": ["GREEN", "YELLOW", "RED"]},
        "state_after": {"enum": context["workflow"]["states"]},
        "agent_type": {"enum": context["canonical_agents"]},
        "handoff_to": {"enum": context["canonical_handoffs"]},
        "policy_intent": {"type": "object", "required": ["action_type", "risk_level", "external_write"],
                          "properties": {"action_type": string, "risk_level": {"enum": ["GREEN", "YELLOW", "RED"]},
                                         "external_write": {"type": "boolean"}}},
        "integration": {"anyOf": [{"type": "null"}, {
            "type": "object", "additionalProperties": False, "required": ["name", "phase"],
            "properties": {"name": string, "phase": {"enum": ["draft", "propose", "read", "execute"]},
                           "use": string, "scope": string, "message": {
                               "type": "object", "required": ["template_id", "fills"], "additionalProperties": False,
                               "properties": {"template_id": string, "fills": {
                                   "type": "object", "additionalProperties": {"type": "string"}}}}}}]},
    }
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


async def stage2_decision(agent, task: dict, context: dict, *, structured_transport=None) -> dict:
    """Use the existing structured envelope path through an explicitly injected transport."""
    try:
        schema = stage2_json_schema(context)
        prompt = (f"COMPANY OS STAGE 2. You are {agent.agent_type}.\n{agent.company_os_instructions}\n"
                  "Choose one native decision using only supplied evidence. Missing evidence stays missing. "
                  "Do not invent consent, authority, requirements, recipients or completion. "
                  "Messages use approved template_id and fills only. Return only the schema object.\n"
                  + json.dumps({"task": task, "context": context}, allow_nan=False))
        if structured_transport is None:
            payload, raw = agent._run_claude_structured(prompt, schema, stage2_errors=True)
        else:
            payload, raw = structured_transport(prompt, schema)
        errors = list(Draft202012Validator(schema).iter_errors(payload))
        if errors:
            raise CompanyOSMalformedOutputError(errors[0].message, raw_output=raw)
        return deepcopy(payload)
    except (CompanyOSTransportError, CompanyOSMalformedOutputError):
        raise
    except Exception as exc:
        raise CompanyOSDecisionMethodDefect(f"{type(exc).__name__}: {exc}") from exc


class OwnerRoutedProvider:
    """Prepare a minimal callback outside the coordinator's exception boundary."""
    provider = "stage2_structured"

    def __init__(self, workflow_registry: dict, agents: dict, *, structured_transport=None):
        self.registry = workflow_registry
        self.agents = agents
        self.transport = structured_transport

    def prepare(self, context: dict):
        entries = self.registry["workflows"]
        if isinstance(entries, list):
            entry = next(e for e in entries if e.get("id", e.get("workflow_id")) == context["workflow"]["id"])
        else:
            entry = entries[context["workflow"]["id"]]
        owner = entry["owner_agent"]
        agent = self.agents[owner]
        if agent.agent_type != owner or context["workflow"]["owner_agent"] != owner:
            raise ValueError("registry, workflow and agent ownership differ")
        task = {"case_id": context["case_id"], "current_state": context["current_state"]}
        frozen = deepcopy(context)

        async def decide(_):
            return await agent.run_company_os_stage2_decision(task, frozen, structured_transport=self.transport)
        return decide
