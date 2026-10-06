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

An expected service rejection specifies exact `call`, `exception_type`,
`expects` message, and a `binding` (an attempt `ref` or approval `decision_id`
-- see "Evidence and failure reporting" below). The separate rejection stream
contains `case_id`, `call`, `exception_type`, `message`, and `expected: true`.
Undeclared, unbound, or repeated rejections abort as harness defects.

Signals may use `sequence_hint` to order eligible arrivals and `after` barriers
with `signal:<delivery_id>` or `milestone:<case_id>:<email-action>`. Signal
barriers wait for ingestion; milestones wait for a nonblocked dispatch ledger
record, including a prepared record left by an adapter failure, matched on
that record's own origin fields and frozen action -- the identical predicate
`SyntheticWorld.receive` uses to bind a `queued_message`, never on which case
happens to be a registered consumer of the record (that diverges for a
chained recovery). A `queued_message` signal's own origin facts must agree
with its declared milestone; a mismatch is rejected at pack-build time. A hint
alone is not a barrier. Eligible signals are drained globally to a fixed
point between individual service calls, and reset a blocked/waiting case to
ready only when the arriving signal actually changes compliance/policy-
relevant evidence -- never on an unrelated signal. Autonomous decisions
advance round-robin. Once an aggregate-workflow case's queue item is bound,
its `item_id` is carried at the snapshot's top level, so
`find_dispatch`/`resolve_recipient`'s queue-scope cross-check actually runs.

`verified_requirements` comes from an explicit, per-string predicate mapping
(`REQUIREMENT_RULES`) grounded in genuinely available evidence -- contact/
consent/suppression are keyed on the contact record's own `recipient_id` for
person (and otherwise-unscoped) cases, or the bound queue item's
`recipient_id` for aggregate cases, matching `resolve_recipient`. A fixture
fact key literally named after a requirement string is rejected at pack-build
time as self-fulfilling; every reachable transition requirement must have a
rule, or pack-build fails loudly rather than silently passing. Recovery retry
requirements count persisted integration failures for the bound dispatch only;
`provider health verified` additionally requires a later successful sandbox
adapter attempt on that same provider. Registry verification alone does not
establish post-failure health.

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
as environment failures; typed method defects abort as harness defects. Pack,
founder-resolution, and driver-controls shape is validated before
`build_stage2_snapshot` even runs, so a malformed artifact is always a harness
defect, never an environment failure.

`outcomes` is the authoritative per-case final classification. `failures`
additionally lists every case whose final outcome was not `terminal`, except
for the run's intentionally-scored non-terminal endpoints -- a
compliance/policy block, a credited service rejection, or a completed approval
script (`script_complete`), each a deliberate endpoint, not a defect. Every other
non-terminal outcome (a waiting/defect/collision/budget-exhaustion/unconsumed
signal or command) gets a `failures` entry, so nothing that kept a case from
reaching terminal is visible only in `outcomes`. `unscripted_founder_request`
is such a failure: once a case's scripted attempts are exhausted the real
provider proposed a new founder-gated action that has no approval row and no
scripted decision ID (nothing executed). It is distinct from
`approval_collision`, which covers re-requesting an action that already has an
approval row, an unresolved request, or a scripted ordinal missing its
decision ID.

`expected_wait` is the other scored endpoint: a driver-control entry may declare
`"expected_wait": {"final_state": "<state>"}` for a case whose workflow has no canonical
transition out of that non-terminal state. The harness rejects a declaration naming an
unknown, terminal or outbound-transition state, and rejects `expected_wait` inside a fixture
case. A `waiting_on_evidence` outcome becomes `expected_wait` (and is not a failure) only
when the case ended in exactly that state with no pending signal or command and no other
failure; it is listed in `expected_waits`. Any other outcome stays a failure.

A declaration may also carry `"blocked_action": "<action>"` for a state whose only outbound
canonical transition is that action and declares requirements (a meeting-ready hand-off waiting
for confirmation). The wait is then credited only when the case's last recorded event is the
service's own refusal of that action: a `failure_detected` event, gate `transition`, result
`blocked`, state unchanged at `final_state`, error `canonical transition requirements lack
evidence`. The declaration never excuses an action the service accepted; the `expected_waits`
row carries both keys.

`script_complete` is the one exception among these: an `unscripted_founder_request`
becomes `script_complete` (and is not a failure) only when the case's driver-control
entry is a pure approval-lifecycle script -- keys limited to `attempts`, `control_commands` and
`expected_wait`, attempts limited to `ordinal`/`ref`/`decision_id`, every command a
`founder_resolution`, no `expected_service_rejection` -- and that script has fully run:
all commands done and none dropped, no resume due, every signal ingested, at least one
approval row, every row `approved`/`modified`/`rejected`/`expired`/`cancelled`, and every
`approved`/`modified` row resumed and followed by a later `transition_completed` event
(the approved action actually ran). The provider's final blocked request stays in the
event log and the existing metrics. Retry, replay, rejection-expectation, signal, mixed,
and no-script cases never receive it, and `needs_more_evidence` or unresumed approvals
keep the failure outcome. It exists because every approval-case fixture carries an
over-ceiling discount request that `prospect_to_meeting` has no transition to decide.

A retry/founder-resolution
control command dropped by budget exhaustion (marked consumed without ever
dispatching) is reported explicitly as `dropped_commands`, distinct from
`commands` (truly unconsumed ones), inside the same `unconsumed_control_command`
failure entry.

An expected service rejection is additionally bound to one specific attempt
`ref` (for `coordinate_sandbox_action`) or `decision_id` (for
`resolve_sandbox_approval`/`resume_sandbox_approval`) via a required `binding`
field, and is credited at most once per case. The coordinator's duplicate/
stale/terminal raise persists an identical message for three distinct causes;
matching on message text alone could credit an unrelated scheduler bug as if
it were the one deliberately-scripted scenario, so the driver additionally
verifies the real runtime cause (idempotency-key reuse, version mismatch, or
a terminal instance) before crediting. Any further or non-matching rejection
is a harness defect.

The driver deliberately does not claim gate success or implement Acqivo's
validators. Modified-approval authorization must be checked against the resolved
effective decision and a real preceding `approval_resolved` event by that
validator (round-6 findings 3 and 4).

## Non-determinism

`occurred_at` and `event_id` on every persisted event are wall-clock and
random respectively (`app/services/company_os_sandbox_coordinator.py`'s
`_append`). Two runs of the same frozen pack therefore never share a
canonical NDJSON SHA-256, even byte-for-byte identical inputs and outcomes --
`raw_sha256`/`native_sha256` in the driver report identify one run's own
exported files, not a fingerprint of the pack's behavior. Anything that needs
to compare two runs for behavioral equivalence must diff parsed, order-
independent event content (or a subset of fields excluding `occurred_at`/
`event_id`), never compare `event_log` hashes directly.
