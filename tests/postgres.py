"""Shared Docker PostgreSQL provider for unit locking and integration proofs."""

import pytest


@pytest.fixture(scope="session")
def postgres_url():
    """Spin up a real Postgres container for the test session."""
    try:
        from testcontainers.postgres import PostgresContainer
        with PostgresContainer("postgres:16-alpine") as pg:
            url = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql+asyncpg://")
            yield url
    except ImportError:
        pytest.skip("testcontainers not installed")


