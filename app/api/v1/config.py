from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_api_key
from app.core.database import get_db
from app.models.company import CompanyConfig

router = APIRouter(prefix="/api/v1/config", tags=["config"], dependencies=[Depends(require_api_key)])


class ConfigUpdate(BaseModel):
    name: str | None = None
    mission: str | None = None
    vision: str | None = None
    description: str | None = None
    target_market: str | None = None
    value_prop: str | None = None
    pricing_model: dict | None = None
    goals: dict | None = None
    kpis: dict | None = None
    website_url: str | None = None
    github_repo: str | None = None
    product_type: str | None = None
    industry: str | None = None
    timezone: str | None = None
    daily_cycle_hour: int | None = None


def _company_to_dict(company: CompanyConfig) -> dict:
    return {
        "id": company.id,
        "name": company.name,
        "mission": company.mission,
        "vision": company.vision,
        "description": company.description,
        "target_market": company.target_market,
        "value_prop": company.value_prop,
        "pricing_model": company.pricing_model,
        "goals": company.goals,
        "kpis": company.kpis,
        "website_url": company.website_url,
        "github_repo": company.github_repo,
        "product_type": company.product_type,
        "industry": company.industry,
        "timezone": company.timezone,
        "daily_cycle_hour": company.daily_cycle_hour,
    }


async def _get_company(db: AsyncSession) -> CompanyConfig | None:
    result = await db.execute(select(CompanyConfig).limit(1))
    return result.scalars().first()


@router.get("")
async def get_config(db: AsyncSession = Depends(get_db)):
    company = await _get_company(db)
    if company is None:
        raise HTTPException(status_code=404, detail="No company config set")
    return _company_to_dict(company)


@router.put("")
async def update_config(body: ConfigUpdate, db: AsyncSession = Depends(get_db)):
    company = await _get_company(db)
    if company is None:
        company = CompanyConfig(name=body.name or "Unnamed Company")
        db.add(company)
        await db.flush()

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(company, field, value)

    await db.flush()
    await db.refresh(company)
    return _company_to_dict(company)
