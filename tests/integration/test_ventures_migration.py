"""Exercise the ventures DDL (0006) against a real disposable PostgreSQL."""

import asyncio
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from alembic.config import Config
from app.config import settings


def test_ventures_migration_on_postgres(postgres_url, monkeypatch):
    monkeypatch.setattr(settings, "database_url", postgres_url)
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    command.upgrade(config, "0006")

    async def check():
        engine = create_async_engine(postgres_url)
        try:
            async with engine.connect() as connection:
                table = await connection.scalar(text("SELECT to_regclass('ventures')"))
                index = await connection.scalar(text("SELECT to_regclass('ix_ventures_status')"))
                version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
                return table, index, version
        finally:
            await engine.dispose()

    try:
        assert asyncio.run(check()) == ("ventures", "ix_ventures_status", "0006")
    finally:
        command.downgrade(config, "base")
