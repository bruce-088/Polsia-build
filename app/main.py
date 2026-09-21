from fastapi import FastAPI

from app.api.v1 import agents, approvals, code, config, dashboard, emails, finance, memory, social, tasks

app = FastAPI(title="Polsia")

app.include_router(dashboard.health_router)
app.include_router(agents.router)
app.include_router(approvals.router)
app.include_router(code.router)
app.include_router(config.router)
app.include_router(dashboard.router)
app.include_router(emails.router)
app.include_router(finance.router)
app.include_router(finance.webhook_router)
app.include_router(memory.router)
app.include_router(social.router)
app.include_router(tasks.router)
