"""Shared run-ledger dispatch for the locked coordinator and approval paths.

No commits and no completion flags live here. Consumer completion is exclusively
an executed transition/terminal event in the caller's current DB transaction.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import select

from app.agents.company_os_compliance import (
    ComplianceContentError,
    ComplianceResult,
    evaluate_outbound_eligibility,
    registered_channel,
    render_message,
)
from app.models.company_os_sandbox import CompanyOSSandboxApproval, CompanyOSSandboxEvent, CompanyOSSandboxRun
from app.services.company_os_synthetic_adapters import (
    SyntheticCapabilityError,
    SyntheticComplianceBlock,
    SyntheticWorld,
)

PERSON_WORKFLOWS = {"prospect_to_meeting", "missed_inquiry_recovery"}
AGGREGATE_WORKFLOWS = {"integration_failure_recovery", "estimate_followup", "stale_lead_reactivation"}
ENTITY_TYPES = {"prospect_to_meeting": "prospect", "missed_inquiry_recovery": "lead",
                "integration_failure_recovery": "integration_operation",
                "estimate_followup": "estimate", "stale_lead_reactivation": "lead_batch"}


class DispatchBlocked(SyntheticComplianceBlock):
    def __init__(self, result: ComplianceResult):
        super().__init__(", ".join(result.evidence["reason_codes"]))
        self.result = result


class RecipientReviewBlocked(SyntheticCapabilityError):
    """Outbound dispatch is permanently blocked for this recipient in the run."""


def is_completion(event: CompanyOSSandboxEvent, dispatch_id: str) -> bool:
    """The same predicate serves reconciliation and orphan detection."""
    return (event.payload.get("metadata", {}).get("dispatch_id") == dispatch_id
            and event.result == "executed_in_sandbox"
            and event.event_type in {"transition_completed", "terminal_outcome"})


async def consumer_events(db, instance) -> list[CompanyOSSandboxEvent]:
    return list(await db.scalars(select(CompanyOSSandboxEvent).where(
        CompanyOSSandboxEvent.workflow_instance_id == instance.id,
    )))


async def find_dispatch(db, instance, adapters, *, approval_id=None, snapshot=None, registry=None, action=None):
    events = await consumer_events(db, instance)
    run = await db.get(CompanyOSSandboxRun, instance.sandbox_run_id)
    candidates = []
    seen = set()
    for adapter in adapters.values():
        world = getattr(adapter, "world", None)
        if not isinstance(world, SyntheticWorld) or world.run_id != run.run_id or id(world) in seen:
            continue
        seen.add(id(world))
        instance_records = [
            (record, consumer)
            for record in world.dispatches.values()
            if record["status"] != "blocked"
            and (consumer := record["consumers"].get(instance.id)) is not None
            and not any(is_completion(e, record["dispatch_id"]) for e in events)
        ]
        my_recipient = None
        if approval_id is not None and action is not None and snapshot is not None:
            try:
                my_recipient, _ = resolve_recipient(world, instance, snapshot, action)
            except SyntheticComplianceBlock:
                my_recipient = None  # cannot resolve my own target -- no additional scope to add
        own_recipients = {
            record["recipient_id"] for record, consumer in instance_records
            if approval_id is None or consumer.get("approval_id") in (None, approval_id)
        }
        if my_recipient is not None:
            own_recipients.add(my_recipient)
        for record, consumer in instance_records:
            other_id = consumer.get("approval_id")
            # None means "no owning approval" (a direct send) -- reconcilable by any
            # approval on this recipient, never a foreign-approval conflict. Only a
            # different, concrete approval_id is a genuine competing claim.
            if approval_id is not None and other_id is not None and other_id != approval_id:
                if record["recipient_id"] not in own_recipients:
                    continue  # unrelated recipient on this instance -- not a conflict
                if record["status"] == "executed":
                    world.review_blocked_recipients.add(record["recipient_id"])
                    continue
                other = await db.get(CompanyOSSandboxApproval, other_id)
                if other is None or other.status in {"pending", "approved", "modified"}:
                    raise SyntheticCapabilityError("conflicting_prepared_dispatch_pending_resolution")
                continue  # other approval is terminal/closed -- its prepared dispatch is abandoned
            if (consumer["expected_state_before"] != instance.current_state
                    or consumer["expected_version"] != instance.version):
                world.review_blocked_recipients.add(record["recipient_id"])
                continue
            if snapshot is not None and "item_id" in snapshot:
                try:
                    _, item = resolve_recipient(world, instance, snapshot, consumer["consumer_decision"]["action"])
                    if item is None or (item.get("dispatch_id") or world.dispatch_id(item["item_id"])) != record["dispatch_id"]:
                        raise SyntheticComplianceBlock("evidence_missing_or_scope_unknown")
                except SyntheticComplianceBlock as exc:
                    blocked = DispatchBlocked(evaluate_outbound_eligibility(compliance_context(
                        world, record["recipient_id"], consumer["consumer_decision"], registry, {}, scope_valid=False,
                    )))
                    blocked.native = consumer["consumer_decision"]
                    blocked.metadata = metadata_for(record)
                    raise blocked from exc
            matching = [a for a in adapters.values()
                        if getattr(a, "world", None) is world and getattr(a, "kind", None) == record["adapter_kind"]]
            if not matching:
                raise SyntheticCapabilityError("dispatch adapter is no longer registered")
            candidates.append((matching[0], record))
    if len(candidates) > 1:
        raise SyntheticCapabilityError("multiple unfinished dispatches require review")
    return candidates[0] if candidates else None


def resolve_recipient(world, instance, snapshot, action):
    """Person workflows address their entity; aggregate workflows require a bound queue."""
    item_id = snapshot.get("item_id")
    item = None
    if ENTITY_TYPES.get(instance.workflow_id) != instance.entity_type:
        raise SyntheticComplianceBlock("evidence_missing_or_scope_unknown")
    person_workflow = instance.workflow_id in PERSON_WORKFLOWS
    aggregate_workflow = instance.workflow_id in AGGREGATE_WORKFLOWS
    if not person_workflow and not aggregate_workflow:
        raise SyntheticComplianceBlock("evidence_missing_or_scope_unknown")
    if aggregate_workflow:
        item = world.queue.get(instance.entity_id)
        if ("recipient_id" in snapshot or not item
                or item.get("workflow_id") != instance.workflow_id
                or item.get("entity_id") != instance.entity_id
                or ("item_id" in snapshot and item_id != item["item_id"])):
            raise SyntheticComplianceBlock("evidence_missing_or_scope_unknown")
        recipient = item["recipient_id"]
    else:
        recipient = instance.entity_id
        if "recipient_id" in snapshot and snapshot["recipient_id"] != recipient:
            raise SyntheticComplianceBlock("evidence_missing_or_scope_unknown")
        items = [q for q in world.queue.values()
                 if q.get("origin_workflow_id") == instance.workflow_id
                 and q.get("origin_entity_id") == instance.entity_id
                 and q["original_action"] == action]
        if len(items) > 1:
            raise SyntheticComplianceBlock("evidence_missing_or_scope_unknown")
        item = items[0] if items else None
        if ((item and item["recipient_id"] != recipient)
                or ("item_id" in snapshot and (not item or item_id != item["item_id"]))):
            raise SyntheticComplianceBlock("evidence_missing_or_scope_unknown")
    if not isinstance(recipient, str) or recipient not in world.contacts:
        raise SyntheticComplianceBlock("evidence_missing_or_scope_unknown")
    return recipient, item


def compliance_context(world, recipient, native, registry, payload, *, scope_valid=True):
    request = native.get("integration") or {}
    config = registry["integrations"].get(request.get("name"), {})
    template_id = payload.get("template_id") or (request.get("message") or {}).get("template_id")
    suppression = deepcopy(world.suppression_snapshots.get(recipient, {}))
    suppression = suppression if isinstance(suppression, dict) else {}
    suppression.update(
        suppressed=world.is_suppressed(recipient),
        normalized_addresses=sorted(world.suppressed_recipients),
        pending_opt_outs=deepcopy(world.pending_opt_outs),
    )
    return {
        "channel": registered_channel(config, request.get("use")), "purpose": "commercial",
        "recipient_id": recipient, "sender": deepcopy(config.get("sandbox_sender", {})),
        "contact": deepcopy(world.contacts.get(recipient, {})),
        "consent": deepcopy(world.consents.get(recipient, {})),
        "suppression": suppression, "template": deepcopy(world.templates.get(template_id, {})),
        "rendered_payload": deepcopy(payload), "evaluated_at": world.frozen_at.isoformat(),
        "policy_version": world.compliance_policy.get("version"),
        "policy_sha256": world.compliance_policy.get("sha256"), "scope_valid": scope_valid,
    }


def metadata_for(record):
    return {"dispatch_id": record["dispatch_id"], "recipient_id": record["recipient_id"],
            "adapter_idempotency_key": record["adapter_idempotency_key"]}


def register_consumer(record, instance, decision, *, approval_id=None):
    """Called only after the coordinator validates this consumer's own decision."""
    consumer = {
        "expected_state_before": instance.current_state, "expected_version": instance.version,
        "consumer_decision": deepcopy(decision), "approval_id": approval_id,
    }
    previous = record["consumers"].get(instance.id)
    if previous is not None and previous != consumer:
        raise SyntheticCapabilityError("consumer decision or occurrence cannot be replaced")
    record["consumers"][instance.id] = consumer


async def execute_dispatch(
    db, instance, native, snapshot, registry, adapter, metadata: dict[str, Any],
    *, existing=None, approval_id=None,
):
    """Prepare, evaluate freshly, and execute without an intervening await.

    Programming exceptions propagate. The concrete provider marks its ledger
    executed before a simulated after-effect timeout can escape.
    """
    world = getattr(adapter, "world", None)
    if not isinstance(world, SyntheticWorld):
        raise SyntheticCapabilityError("registered adapter requires a run-scoped synthetic world")
    run = await db.get(CompanyOSSandboxRun, instance.sandbox_run_id)
    if world.run_id != run.run_id:
        raise SyntheticCapabilityError("adapter run differs from workflow run")
    record = existing
    if record is not None and record["status"] == "executed":
        metadata.update(metadata_for(record))
        metadata["reconciled"] = True
        return record["receipt"], None
    scope_valid = True
    try:
        recipient, item = resolve_recipient(world, instance, snapshot, native["action"])
    except SyntheticComplianceBlock:
        recipient, item, scope_valid = instance.entity_id, None, False
    metadata["recipient_id"] = recipient
    if not scope_valid:
        raise DispatchBlocked(evaluate_outbound_eligibility(
            compliance_context(world, recipient, native, registry, {}, scope_valid=False),
        ))
    if item and item["status"] == "blocked":
        raise DispatchBlocked(evaluate_outbound_eligibility(
            compliance_context(world, recipient, native, registry, {}, scope_valid=False),
        ))
    if recipient in world.review_blocked_recipients:
        events = await consumer_events(db, instance)
        for orphan in world.dispatches.values():
            if (orphan["recipient_id"] == recipient and instance.id in orphan["consumers"]
                    and not any(e.payload.get("metadata", {}).get("orphan_dispatch_id") == orphan["dispatch_id"] for e in events)):
                metadata["orphan_dispatch_id"] = orphan["dispatch_id"]
                break
        raise RecipientReviewBlocked("orphan dispatch: outbound sends to recipient blocked for the rest of this run")

    if record is None and item:
        record = world.dispatches.get(item.get("dispatch_id") or world.dispatch_id(item["item_id"]))
        if record:
            if record["recipient_id"] != recipient or record["adapter_kind"] != adapter.kind:
                raise SyntheticCapabilityError("queued dispatch identity differs")
            register_consumer(record, instance, native, approval_id=approval_id)
    if record is not None:
        metadata.update(metadata_for(record))
        if record["status"] == "executed":
            if item:
                item.update(status="sent", receipt=record["receipt"])
            metadata["reconciled"] = True
            return record["receipt"], None
        payload = record["rendered_payload"]
    else:
        config = registry["integrations"][native["integration"]["name"]]
        message = native["integration"].get("message") or {}
        try:
            payload = render_message(
                message, world.templates.get(message.get("template_id"), {}),
                config.get("sandbox_sender", {}), world.contacts[recipient],
            )
        except ComplianceContentError:
            payload = {}
        events = await consumer_events(db, instance)
        n = sum(e.result == "executed_in_sandbox" and e.state_before != e.state_after for e in events)
        key = (f"stage2-approval-resume:{approval_id}" if approval_id is not None else
               f"stage2-action:{instance.sandbox_run_id}:{instance.id}:{instance.current_state}:"
               f"{native['action']}:{recipient}:{n}")
        message_key = item["item_id"] if item else (
            f"{instance.id}:{instance.current_state}:{instance.version}:{native['action']}")
        if not item:
            message_key += f":approval:{approval_id}" if approval_id is not None else ":direct"
            base_message_key = message_key
            attempt = 0
            # A blocked attempt sent nothing. Preserve its evidence but permit a fresh decision.
            while (previous := world.dispatches.get(world.dispatch_id(message_key))) is not None:
                if previous["status"] != "blocked":
                    break
                attempt += 1
                message_key = f"{base_message_key}:attempt:{attempt}"
        record = world.prepare_dispatch(
            message_key=message_key, recipient_id=recipient, adapter_kind=adapter.kind,
            adapter_idempotency_key=key, origin_workflow_instance_id=instance.id,
            origin_state_before=instance.current_state, origin_workflow_version=instance.version,
            frozen_native_decision=native, rendered_payload=payload, consumers={},
            origin_workflow_id=item.get("origin_workflow_id") if item else instance.workflow_id,
            origin_entity_id=item.get("origin_entity_id") if item else instance.entity_id,
        )
        register_consumer(record, instance, native, approval_id=approval_id)
        metadata.update(metadata_for(record))
    if item:
        item["dispatch_id"] = record["dispatch_id"]
        item["original_adapter_key"] = record["adapter_idempotency_key"]
    context = compliance_context(world, recipient, record["frozen_native_decision"], registry, payload,
                                 scope_valid=record["status"] != "blocked" and (not item or item["status"] != "blocked"))
    result = evaluate_outbound_eligibility(context)
    if not result.eligible:
        record["status"] = "blocked"
        if item:
            item["status"] = "blocked"
        raise DispatchBlocked(result)
    world.arm_action_failure(instance.workflow_id, instance.entity_id, native["action"],
                             adapter.kind, record["adapter_idempotency_key"])
    try:
        metadata["dispatch_attempted"] = True
        receipt = await adapter.execute(
            {**deepcopy(record["frozen_native_decision"]), "rendered_payload": deepcopy(payload)},
            idempotency_key=record["adapter_idempotency_key"], run_id=run.run_id,
            recipient_id=recipient,
        )
    except SyntheticComplianceBlock as exc:
        record["status"] = "blocked"
        if item:
            item["status"] = "blocked"
        # Re-read trusted records to capture the adapter's defense-in-depth refusal.
        result = evaluate_outbound_eligibility(compliance_context(
            world, recipient, record["frozen_native_decision"], registry, payload, scope_valid=False,
        ))
        raise DispatchBlocked(result) from exc
    if not isinstance(receipt, str) or not receipt.startswith("sandbox://"):
        raise SyntheticCapabilityError("synthetic adapter did not return sandbox evidence")
    record.update(status="executed", receipt=receipt)
    if item:
        item.update(status="sent", receipt=receipt)
    return receipt, result.evidence


def refusal_evidence(adapter, instance, native, registry, metadata):
    world = getattr(adapter, "world", None)
    if not isinstance(world, SyntheticWorld):
        return evaluate_outbound_eligibility({}).evidence
    recipient = metadata.get("recipient_id", instance.entity_id)
    return evaluate_outbound_eligibility(compliance_context(
        world, recipient, native, registry, {}, scope_valid=False,
    )).evidence
