# AGENTS.md — Polsia Stage 2 (Company OS sandbox) map

Read this before touching Stage 2 / Company OS code. It exists so builders and reviewers do not have to rediscover the layout. Keep it current when you move responsibilities between files.

## Scope

Stage 2 runs Acqivo (Company Test #1) against **synthetic** people, messages and providers only. No production integration, live write, or external side effect is ever allowed. The canonical Company OS (workflows, policy, registry, schemas, fixtures, compliance policy) lives in the separate repo `bruce-088/acqivo-company-os`; this repo only vendors pinned copies for tests.

## Who owns what

| File | Owns |
|---|---|
| `app/services/company_os_sandbox_coordinator.py` | `coordinate_sandbox_action`: the one governed path for a direct Stage 2 action. Row lock, replay/stale checks, dispatch lookup, model decision, validation, policy, integration capability, approval stop, compliance gate, adapter call, event append. |
| `app/services/company_os_sandbox_approval_service.py` | `resolve_sandbox_approval` (founder resolution) and `resume_sandbox_approval` (governed resume of an approved/modified RED action; same gates, stable key `stage2-approval-resume:{approval.id}`). |
| `app/services/company_os_sandbox_dispatch.py` | Send ledger logic: `find_dispatch` (unfinished dispatch lookup, cross-path), `resolve_recipient` (recipient rules), `execute_dispatch` (prepare → fresh compliance check → adapter), `is_completion` (DB-authoritative completion predicate), consumer registration, typed dispatch exceptions. |
| `app/services/company_os_synthetic_adapters.py` | `SyntheticWorld` (run-scoped fake provider state: contacts, consent, suppression, pending opt-outs, templates, queue items, dispatch ledger, outcomes, signal history) and `SyntheticAdapter` (in-memory executor). Opt-out ingestion and phrase detection live here. |
| `app/agents/company_os_compliance.py` | `evaluate_outbound_eligibility` (BASE-01, EMAIL-01..04 from COMPLIANCE_POLICY.md v0.1.0), `render_message` (content only from approved templates + trusted sender/contact), `is_email_execute`, evidence hashing. |
| `app/services/company_os_sandbox_service.py` | Persistence: `create_sandbox_run`, `create_workflow_instance`, `append_sandbox_event` (schema validation, version bump only on state change, terminal handling, blocked-event invariant), `export_sandbox_event`. |
| `app/services/company_os_stage2_inputs.py` | `build_stage2_snapshot` builds canonical contact/template snapshots; `apply_stage2_signal` applies one ordered signal; `load_stage2_inputs` preserves eager loading via both, with no invented values. |
| `app/services/company_os_stage2_harness_runner.py` | Part B orchestration: round-robin decisions, signal barriers, separate control commands, approval lifecycle, budgets, exactly-once native/canonical export and driver report. See `docs/STAGE2_HARNESS_INTERFACE.md`. |
| `app/agents/company_os_stage2_provider.py` | Owner-routed Stage 2 structured decisions and typed provider failure provenance; inherits the existing structured transport through `BasePolsiaAgent`. |
| `scripts/stage2_harness_export.py` | Isolated-database CLI, commit-before-export, three evidence streams, raw hashes and failure markers. Real provider execution requires the separately authorized freeze; tests use mocked transports only. |
| `app/agents/company_os_{workflow,policy,integration}.py` | Canonical transition validation, action policy, integration capability. |
| `app/models/company_os_sandbox.py` | Tables: run, workflow instance, event, approval. |

## Order inside `coordinate_sandbox_action`

lock instance `FOR UPDATE` → reject duplicate/stale/terminal delivery → `find_dispatch` (reconcile executed dispatch receipt-only, or replay a prepared one from its frozen decision without calling the model) → model `decide` → validate native decision → canonical transition → policy → integration capability → unresumed-approval check → RED approval stop → `execute_dispatch` for email executes (compliance gate immediately before the adapter) → append event (terminal event when `state_after` is terminal).

## Rules that must always hold

- The recipient never comes from the model. Person workflows (`prospect_to_meeting`, `missed_inquiry_recovery`) send to `instance.entity_id`; queue workflows (`integration_failure_recovery`, `estimate_followup`, `stale_lead_reactivation`) send only via a queue item bound to both workflow and entity; anything else fails closed.
- One queued message has exactly one dispatch identity, whichever workflow sends or recovers it.
- A new send happens only through the ledger and only after a fresh compliance check. A dispatch that was already executed is only ever reconciled (receipt reuse), never re-sent.
- Message text comes only from an approved template; the model supplies `template_id` + fills.
- Compliance failure = RED, `result: blocked`, `approval: null`, non-approvable, no adapter call, state unchanged. Founder approval never overrides it.
- A blocked event never changes workflow state (`append_sandbox_event` enforces this).
- Every outcome leaves an event; only programming errors propagate as exceptions (and roll back).
- Opt-outs are recorded by normalized address and never dropped, even for unknown contacts.
- Only a governed `execute` may touch an email integration; any other phase is refused before the adapter.
- An orphan dispatch (unfinished, from a superseded workflow version) blocks outbound sends to its recipient for the rest of the run; it is reported once, at the first attempted send. Review-required dispatch conditions are recorded as `dispatch_review` events, never raised.

## Tests and proof

```bash
.venv/bin/python -m pytest -q -o addopts=""        # full suite; needs Docker for DB-backed tests (testcontainers)
.venv/bin/python -m pytest -q tests/unit/test_company_os_dispatch_integrity.py tests/unit/test_company_os_stage2_required_paths.py
git diff --name-only <base> | grep '\.py$' | xargs .venv/bin/ruff check   # lint changed files; the tree has pre-existing Ruff errors
```

- The pyproject `addopts` includes `-q`; pass `-o addopts=""` to see the pass/fail summary line.
- Sandboxes without Docker access cannot run DB-backed tests; say which ones you could not run rather than skipping them.
- Key test files: `test_company_os_dispatch_integrity.py` (ledger, rollback, concurrency, cross-path, compliance), `test_company_os_stage2_required_paths.py` (four required paths on unmodified canonical inputs), `test_company_os_compliance.py`, `test_company_os_synthetic_adapters.py`, `test_company_os_sandbox_coordinator.py`, `test_company_os_stage2_inputs.py`.

## Vendored canonical inputs

`tests/fixtures/company_os/acqivo/` holds byte-for-byte copies from `acqivo-company-os`; `MANIFEST.json` pins the source commit and SHA-256 per file, and a test checks them. To update: `git -C <acqivo checkout> show <commit>:<path>` for each file, then update `source_commit` and hashes. Never edit vendored files by hand, and never patch canonical inputs inside tests.

## Claude ↔ Codex loop roles

When Claude and Codex work together on this repo (claudex-loop), the provider with more usage headroom builds and the other only reviews the plan and inspects the final code. Default model split: the lighter model (Codex `gpt-6-sol`, Claude Sonnet 5) for chat/coordination; the coding-tier model (Codex `gpt-6-astra`, Claude Opus 5.5) for actual builds, plan reviews, and code inspections. When either side's usage is genuinely tight, drop to the lighter model for that side even for build/review work, and say so. Cap plan review at about 3 rounds, keep work orders short and file-scoped, and never spend paid credits without the founder's approval. A provider never counts as the independent reviewer of code it wrote.

## Never

- Call production services or real providers in Stage 2 code or tests.
- Edit frozen Stage 1 evidence (lives in the Acqivo repo under `simulations/results`).
- Push, merge, or open PRs without the founder's explicit approval.
