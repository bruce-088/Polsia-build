"""Import every model submodule so Base.metadata is fully populated before
create_all runs (required by alembic/env.py and tests/integration/conftest.py)."""
from app.models import (  # noqa: F401
    ads,
    agent_run,
    company,
    competitor,
    email,
    finance,
    memory,
    prospect,
    report,
    social,
    task,
)
