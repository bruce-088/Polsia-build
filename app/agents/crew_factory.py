"""Dispatch layer — maps an agent_type string to its concrete agent class and
runs it. Deliberately synchronous (callers wrap it in asyncio.run themselves,
see celery_app/tasks/agent_tasks.py) and deliberately does not catch
agent-level exceptions itself — that's the Celery task's job."""
import importlib

AGENT_MAP: dict[str, tuple[str, str]] = {
    "orchestrator": ("app.agents.orchestrator.agent", "OrchestratorAgent"),
    "business_planning": ("app.agents.business_planning.agent", "BusinessPlanningAgent"),
    "competitor_research": ("app.agents.competitor_research.agent", "CompetitorResearchAgent"),
    "social_media": ("app.agents.social_media.agent", "SocialMediaAgent"),
    "ads_management": ("app.agents.ads_management.agent", "AdsManagementAgent"),
    "email_outreach": ("app.agents.email_outreach.agent", "EmailOutreachAgent"),
    "customer_support": ("app.agents.customer_support.agent", "CustomerSupportAgent"),
    "code_generation": ("app.agents.code_generation.agent", "CodeGenerationAgent"),
    "finance": ("app.agents.finance.agent", "FinanceAgent"),
}


def run_agent_for_task(agent_type: str, task: dict, context: dict) -> dict:
    if agent_type not in AGENT_MAP:
        raise ValueError(f"Unknown agent: {agent_type}")

    module_path, class_name = AGENT_MAP[agent_type]
    module = importlib.import_module(module_path)
    agent_class = getattr(module, class_name)
    agent = agent_class()
    metadata = task.get("task_metadata") or {}
    response_contract = metadata.get("response_contract")
    if response_contract is None:
        return agent.run(task, context)
    if response_contract != "company_os_stage1":
        raise ValueError(f"Unknown response contract: {response_contract}")
    return agent.run_company_os_decision(task, context)
