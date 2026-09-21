import re
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import require_api_key
from app.services.github_service import open_pr

router = APIRouter(prefix="/api/v1/code", tags=["code"], dependencies=[Depends(require_api_key)])


class OpenPrRequest(BaseModel):
    file_path: str
    new_content: str
    pr_title: str
    pr_body: str = ""


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40] or "change"


@router.post("/open-pr")
def open_pr_endpoint(request: OpenPrRequest):
    """The only code path that ever opens a real PR — always an explicit,
    human-triggered call, never invoked automatically by an agent."""
    branch_name = f"agent/{_slugify(request.pr_title)}-{int(time.time())}"
    try:
        pr_url = open_pr(
            file_path=request.file_path,
            new_content=request.new_content,
            branch_name=branch_name,
            pr_title=request.pr_title,
            pr_body=request.pr_body,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GitHub error: {exc}")
    return {"status": "opened", "pr_url": pr_url, "branch": branch_name}
