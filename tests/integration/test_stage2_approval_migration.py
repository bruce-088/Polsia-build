"""Exercise the Stage 2 approval DDL against a real disposable PostgreSQL."""

import asyncio
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from alembic.config import Config
from app.config import settings


def test_stage2_approval_migration_on_postgres(postgres_url, monkeypatch):
    monkeypatch.setattr(settings, "database_url", postgres_url)
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    command.upgrade(config, "head")

    async def check():
        engine = create_async_engine(postgres_url)
        try:
            async with engine.connect() as connection:
                table = await connection.scalar(text("SELECT to_regclass('company_os_sandbox_approvals')"))
                index = await connection.scalar(text("SELECT to_regclass('uq_company_os_sandbox_approval_pending_action')"))
                version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
                return table, index, version
        finally:
            await engine.dispose()

    try:
        assert asyncio.run(check()) == (
            "company_os_sandbox_approvals", "uq_company_os_sandbox_approval_pending_action", "0005",
        )
    finally:
        command.downgrade(config, "base")
