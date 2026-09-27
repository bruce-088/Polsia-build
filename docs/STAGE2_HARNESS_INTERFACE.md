# Stage 2 native export interface

This is Part B of S2-P1.3. Acqivo owns the fixture pack, preflight, semantic
validator, manifest, gate and evaluation. No smoke or scoreable run is part of
this implementation's proof. Real decision-provider execution requires the
separate execution freeze described in the plan.

`run_stage2_fixture_pack` accepts an input-only pack, loaded canonical runtime
objects, a decision provider, and separate `driver_controls` and
`founder_resolutions` objects. It yields the unchanged canonical event payload
and a companion record copied from that event row's native fields. The caller
owns the transaction: commit each yielded pair before writing it. Replayed event
IDs are emitted once. Service rejections never create canonical events.

The CLI is `python -m scripts.stage2_harness_export`. Required arguments are
`--fixture-pack`, `--runtime-inputs`, `--run-id`, `--events`, `--native-evidence`,
`--service-rejections`, `--database`, and `--provider`. Optional artifacts are
`--driver-controls` and `--founder-resolutions`. Every output must be distinct and
unoccupied. The database is a newly created, isolated SQLite database; configured
application databases and live integration adapters are never used.

The runtime JSON contains `workflows` (keyed by workflow ID), `policy`,
`integration_registry`, `event_schema`, `canonical_agents`, `canonical_handoffs`,
`canonical_actions`, `compliance_policy`, and `synthetic_adapter_kinds` (integration
name to synthetic adapter kind). A structured-provider run also supplies the
canonical `workflow_registry`. The pack supplies `frozen_clock`,
`approved_templates`, `synthetic_only: true`, and `cases` in the existing input
format. The frozen clock governs synthetic facts and compliance; existing
governed services retain their wall-clock event timestamps and generated UUIDs.
Export does not rewrite either field.

Provider selection:

- `deterministic_mock` requires `--infrastructure-only`, a pack tagged
  `infrastructure_only: true`, and `--mock-decisions` pointing to a separate map
  of case IDs to decision lists. These runs are never scoreable.
- `stage2_structured` selects the workflow registry's owner agent and its
  inherited Stage 2 decision method. The method uses the existing Claude CLI
  structured-envelope transport with Stage 2 error types and schema. It refuses
  mock decision arguments and infrastructure-only packs. Tests replace the
  transport; this build does not invoke the real provider.

## Controls and ordering

Round-6 resolution: `attempts`, `control_commands`, and
`expected_service_rejection` live only in the separate driver-controls artifact,
keyed by case ID. The runner rejects those fields in the input pack. Corrections
remain in the separate founder-resolutions list. Neither artifact enters a
provider context. Acqivo preflight must freeze both artifacts along with the pack.

An attempt has `ordinal` (1–25), unique `ref`, and optional `decision_id`,
`scripted_failure: "ScriptedProviderTimeout"`, `replay_of` (earlier attempt ref),
or `expected_version` for deliberately stale coordinator delivery. The pack's
`scripted_failure_types` may declare only `ScriptedProviderTimeout`; transport or
method defects cannot be relabeled as scripted failures.

Each command has a unique `id` and one of these shapes:

```json
{"id":"resolve","command_type":"founder_resolution","decision_id":"D",
 "precondition":{"type":"approval_pending","decision_id":"D"}}
```

```json
{"id":"retry","command_type":"retry",
 "precondition":{"type":"failed_attempt_persisted","attempt_ref":"first"}}
```

```json
{"id":"resume-retry","command_type":"retry",
 "precondition":{"type":"failed_attempt_persisted","decision_id":"D","failure_ordinal":1}}
```

Resume retries resolve the actual approval row and its last failure at runtime;
fixtures never guess the DB-generated resume key. Founder corrections have
`case_id`, `decision_id`, `status`, `founder_id`, `founder_minutes`,
`corrected_decision`, and `manual_evidence_ref`. Only `modified` carries a
non-null corrected decision. All six lifecycle statuses are supported.

An expected service rejection specifies exact `call`, `exception_type`, and
`expects` message. The separate rejection stream contains `case_id`, `call`,
`exception_type`, `message`, and `expected: true`. Undeclared rejections abort as
harness defects.

Signals may use `sequence_hint` to order eligible arrivals and `after` barriers
with `signal:<delivery_id>` or `milestone:<case_id>:<email-action>`. Signal
barriers wait for ingestion; milestones wait for a nonblocked dispatch ledger
record, including a prepared record left by an adapter failure. A hint alone is
not a barrier. Eligible signals are drained globally to a fixed point between
individual service calls. Autonomous decisions advance round-robin.

The coordinator-call budget is 25 per case and the retry-command budget is 3.
Unknown references and signal cycles fail validation before the run. As approved
in the frozen plan, structurally reachable commands whose preconditions never
become true are explicit `unconsumed_control_command` runtime failures. Pending
signals are also reported. Budget exhaustion stays distinct from both.

## Evidence and failure reporting

The CLI writes `<events>_driver_report.json`, including provider identity,
per-case outcomes, pending work, failures, and raw canonical/native SHA-256.
`<events>_harness_defect.json` takes precedence over
`<events>_environment_failure.json`; partial evidence is preserved. Malformed
model output remains an exported, scoreable failure. Transport failures abort
as environment failures; typed method defects abort as harness defects.

The driver deliberately does not claim gate success or implement Acqivo's
validators. Modified-approval authorization must be checked against the resolved
effective decision and a real preceding `approval_resolved` event by that
validator (round-6 findings 3 and 4).
