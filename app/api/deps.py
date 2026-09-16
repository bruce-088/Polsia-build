"""Shared FastAPI dependencies."""
from fastapi import Header, HTTPException

from app.config import settings


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if x_api_key is None or x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
