"""Async SQLAlchemy engine/session plumbing.

Contract (see tests/conftest.py::async_db_session and CLAUDE.md): get_db()
auto-commits on a successful yield and rolls back on exception. Callers
(services, routes) must NOT call db.commit() themselves — only flush()+
refresh() when they need a DB-generated value before returning.
"""
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    pass


_engine = create_async_engine(settings.database_url, echo=False)
_SessionLocal = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with _SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
