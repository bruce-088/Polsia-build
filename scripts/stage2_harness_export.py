"""Export Stage 2 events: python -m scripts.stage2_harness_export.

Provider execution requires a separately authorized run. The CLI owns
an isolated SQLite database, commits each returned event before writing it, and
never opens the application's configured database or constructs live adapters.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents.company_os_stage2_provider import OwnerRoutedProvider
from app.config import settings
from app.models.company_os_sandbox import (
    CompanyOSSandboxApproval,
    CompanyOSSandboxEvent,
    CompanyOSSandboxRun,
    CompanyOSWorkflowInstance,
)
from app.services.company_os_stage2_harness_runner import (
    HarnessDefect,
    HarnessReport,
    run_stage2_fixture_pack,
    validate_artifact_shapes,
)
from app.services.company_os_stage2_inputs import Stage2InputError, build_stage2_snapshot
from app.services.company_os_synthetic_adapters import SyntheticAdapter


class DeterministicMockProvider:
    """Separate test-only decisions; never read decisions from an input-facts pack."""
    provider = "deterministic_mock"

    def __init__(self, decisions):
        self.decisions = deepcopy(decisions)
        self.positions = {}

    def prepare(self, context):
        case_id = context["case_id"]
        position = self.positions.get(case_id, 0)
        decision = deepcopy(self.decisions[case_id][position])
        self.positions[case_id] = position + 1

        async def decide(_):
            return deepcopy(decision)
        return decide


def read_json(path):
    return json.loads(Path(path).read_bytes())


def write_line(handle, value):
    handle.write(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
    handle.flush()


async def export(args) -> int:
    mock = args.provider == "deterministic_mock"
    if args.provider not in {"deterministic_mock", "stage2_structured"}:
        raise ValueError("unknown decision provider")
    if mock and (not args.infrastructure_only or not args.mock_decisions):
        raise ValueError("deterministic_mock requires infrastructure_only and mock_decisions")
    if not mock and (args.infrastructure_only or args.mock_decisions):
        raise ValueError("stage2_structured cannot use infrastructure_only or mock_decisions")
    if not settings.sandbox_mode:
        raise ValueError("SANDBOX_MODE must be enabled")
    paths = [Path(p).resolve() for p in (args.events, args.native_evidence, args.service_rejections, args.database)]
    report_path = Path(str(paths[0]) + "_driver_report.json")
    defect_path = Path(str(paths[0]) + "_harness_defect.json")
    environment_path = Path(str(paths[0]) + "_environment_failure.json")
    all_paths = [*paths, report_path, defect_path, environment_path]
    if len(set(all_paths)) != len(all_paths) or any(p.exists() for p in all_paths):
        raise ValueError("output paths must be distinct and unoccupied")
    pack, runtime = read_json(args.fixture_pack), read_json(args.runtime_inputs)
    if mock and pack.get("infrastructure_only") is not True:
        raise ValueError("mock CLI refuses a pack not tagged infrastructure_only")
    if not mock and pack.get("infrastructure_only") is True:
        raise ValueError("structured provider refuses an infrastructure_only pack")
    corrections = read_json(args.founder_resolutions) if args.founder_resolutions else []
    driver_controls = read_json(args.driver_controls) if args.driver_controls else {}
    provider = (DeterministicMockProvider(read_json(args.mock_decisions)) if mock else
                structured_provider(runtime["workflow_registry"]))
    report = HarnessReport()
    engine = None
    with ExitStack() as stack:
        events = stack.enter_context(paths[0].open("x", encoding="utf-8"))
        native = stack.enter_context(paths[1].open("x", encoding="utf-8"))
        rejections = stack.enter_context(paths[2].open("x", encoding="utf-8"))
        # Exclusive creation prevents accidentally connecting to any existing DB.
        paths[3].touch(exist_ok=False)
        try:
            # validate_artifact_shapes only checks outer container shapes
            # (pack is an object with a list of case objects, etc.); it does
            # not, and cannot cheaply, catch every malformed case (e.g. one
            # missing contact_record entirely). build_stage2_snapshot -- the
            # actual parser -- pre-builds this run's world outside
            # run_stage2_fixture_pack (so the same world can be shared with
            # the adapters below), so its own Stage2InputError must be caught
            # by this same try block, inside the classification boundary
            # below, and explicitly mapped to harness_defect (P13-REV-03) --
            # never left to fall through to the generic except Exception,
            # which would misfile a malformed artifact as an environment
            # failure instead of the code/fixture defect it actually is.
            validate_artifact_shapes(pack, corrections, driver_controls)
            world = build_stage2_snapshot(pack, runtime["integration_registry"], pack["approved_templates"],
                run_id=args.run_id, compliance_policy=runtime["compliance_policy"])
            adapters = {name: SyntheticAdapter(world, kind) for name, kind in runtime["synthetic_adapter_kinds"].items()}
            engine = create_async_engine("sqlite+aiosqlite:///" + str(paths[3]))
            async with engine.begin() as conn:
                for model in (CompanyOSSandboxRun, CompanyOSWorkflowInstance, CompanyOSSandboxEvent, CompanyOSSandboxApproval):
                    await conn.run_sync(lambda connection, table=model.__table__: table.create(connection))
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as db:
                try:
                    async for payload, evidence in run_stage2_fixture_pack(
                        db, fixture_pack=pack, run_id=args.run_id, decide=provider, world=world,
                        founder_resolutions=corrections, report=report, synthetic_adapters=adapters,
                        driver_controls=driver_controls,
                        **{k: runtime[k] for k in ("workflows", "policy", "integration_registry", "event_schema",
                                                  "canonical_agents", "canonical_handoffs", "canonical_actions")},
                    ):
                        await db.commit()
                        write_line(events, payload)
                        write_line(native, evidence)
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise
        except (HarnessDefect, Stage2InputError) as exc:
            # A malformed artifact (missing/invalid case data Stage2InputError
            # catches -- company_os_stage2_inputs.py) is a code/fixture
            # defect, never an environment failure (P13-REV-03).
            report.harness_defect = report.harness_defect or {"type": type(exc).__name__, "message": str(exc)}
        except Exception as exc:
            # A genuine database/infrastructure I/O failure (e.g. a real
            # sqlalchemy.exc.DBAPIError the runner deliberately left
            # unconverted -- P13-REV-03) lands here, keeping its
            # infrastructure/environment classification.
            report.environment_failure = {"type": type(exc).__name__, "message": str(exc)}
        finally:
            if engine is not None:
                await engine.dispose()
            for rejection in report.service_rejections:
                write_line(rejections, rejection)
        if report.harness_defect:
            with defect_path.open("x", encoding="utf-8") as handle:
                write_line(handle, report.harness_defect)
        elif report.environment_failure:
            with environment_path.open("x", encoding="utf-8") as handle:
                write_line(handle, report.environment_failure)
        summary = asdict(report)
        summary["raw_sha256"] = hashlib.sha256(paths[0].read_bytes()).hexdigest()
        summary["native_sha256"] = hashlib.sha256(paths[1].read_bytes()).hexdigest()
        summary["infrastructure_only"] = mock
        with report_path.open("x", encoding="utf-8") as handle:
            write_line(handle, summary)
    return 1 if report.harness_defect or report.environment_failure else 0


def structured_provider(registry):
    """Construct role owners only; constructors perform no decision calls."""
    from app.agents.customer_support.agent import CustomerSupportAgent
    from app.agents.email_outreach.agent import EmailOutreachAgent
    from app.agents.finance.agent import FinanceAgent
    from app.agents.orchestrator.agent import OrchestratorAgent
    from app.agents.revenue_ops.agent import RevenueOperationsAgent

    agents = [cls() for cls in (CustomerSupportAgent, EmailOutreachAgent, FinanceAgent,
                              OrchestratorAgent, RevenueOperationsAgent)]
    return OwnerRoutedProvider(registry, {agent.agent_type: agent for agent in agents})


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    for name in ("fixture-pack", "runtime-inputs", "run-id", "events", "native-evidence", "service-rejections", "database"):
        result.add_argument("--" + name, required=True)
    result.add_argument("--mock-decisions")
    result.add_argument("--founder-resolutions")
    result.add_argument("--driver-controls")
    result.add_argument("--provider", choices=["deterministic_mock", "stage2_structured"], required=True)
    result.add_argument("--infrastructure-only", action="store_true")
    return result


def main():
    args = parser().parse_args()
    try:
        return asyncio.run(export(args))
    except ValueError as exc:
        parser().error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
