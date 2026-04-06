import logging
from typing import Optional, List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("cerebro.routes.interactive")

router = APIRouter()

class FeatureRequest(BaseModel):
    instruction: str
    target_file: Optional[str] = ""
    context_files: Optional[List[str]] = []

class BugfixRequest(BaseModel):
    instruction: str
    target_file: str
    context_files: Optional[List[str]] = []

@router.post("/feature")
async def request_feature(req: FeatureRequest):
    """Solicita la implementación de una nueva característica al Executor."""
    try:
        from app.autofix_client import get_autofix_client
        client = get_autofix_client()
        result = await client.trigger_interactive_job(
            action="feature",
            target_file=req.target_file,
            instruction=req.instruction,
            context_files=req.context_files
        )
        return {"ok": True, "message": "Feature request encolado", "data": result}
    except Exception as e:
        logger.exception("Error en /feature")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/bugfix")
async def request_bugfix(req: BugfixRequest):
    """Solicita la reparación de un bug específico al Executor."""
    try:
        from app.autofix_client import get_autofix_client
        client = get_autofix_client()
        result = await client.trigger_interactive_job(
            action="bugfix",
            target_file=req.target_file,
            instruction=req.instruction,
            context_files=req.context_files
        )
        return {"ok": True, "message": "Bugfix request encolado", "data": result}
    except Exception as e:
        logger.exception("Error en /bugfix")
        raise HTTPException(status_code=500, detail=str(e))
