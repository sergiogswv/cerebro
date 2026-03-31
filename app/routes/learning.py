"""
routes/learning.py — Endpoints de Feedback, Aprendizaje y Gestión de Cambios.

Rutas:
  POST /feedback
  GET  /learn
  GET  /feedback/stats
  GET  /learned-rules
  GET  /changes/pending
  GET  /changes/approved-batch
  POST /changes/approve
  POST /changes/reject
  POST /changes/apply
  GET  /changes/stats
"""

import logging
from fastapi import APIRouter, HTTPException, Request
from app.models import ApiResponse

router = APIRouter(tags=["learning"])
logger = logging.getLogger("cerebro.routes.learning")


# ─── Feedback ─────────────────────────────────────────────────────────────────

@router.post("/feedback", response_model=ApiResponse,
             summary="Registrar feedback de usuario sobre decisión de Cerebro")
async def submit_feedback(request: Request):
    from app.context_db import get_context_db

    body          = await request.json()
    event_id      = body.get("event_id")
    feedback_type = body.get("feedback_type")
    reason        = body.get("reason")
    suggested     = body.get("suggested_action")

    if not event_id or not feedback_type:
        raise HTTPException(status_code=400, detail="event_id y feedback_type son requeridos")

    feedback_id = get_context_db().record_feedback(
        event_id=event_id, feedback_type=feedback_type,
        reason=reason, suggested_action=suggested,
    )
    return ApiResponse(ok=True, message="Feedback registrado", data={"feedback_id": feedback_id})


@router.get("/learn", response_model=ApiResponse,
            summary="Analizar feedback y sugerir ajustes de reglas")
async def get_learning_suggestions(limit: int = 100):
    from app.context_db import get_context_db
    return ApiResponse(ok=True, data=get_context_db().analyze_learning(limit=limit))


@router.get("/feedback/stats", response_model=ApiResponse,
            summary="Obtener estadísticas de feedback")
async def get_feedback_stats(event_id: str = None):
    from app.context_db import get_context_db
    return ApiResponse(ok=True, data=get_context_db().get_feedback_stats(event_id=event_id))


@router.get("/learned-rules", response_model=ApiResponse,
            summary="Obtener reglas aprendidas")
async def get_learned_rules(active_only: bool = True):
    from app.context_db import get_context_db
    return ApiResponse(ok=True, data=get_context_db().get_learned_rules(active_only=active_only))


# ─── Change Management ────────────────────────────────────────────────────────

@router.get("/changes/pending", response_model=ApiResponse,
            summary="Obtener cambios pendientes de aprobación")
async def get_pending_changes():
    from app.change_manager import get_change_manager
    changes = get_change_manager().get_pending_changes()
    return ApiResponse(ok=True, data={"changes": changes, "count": len(changes)})


@router.get("/changes/approved-batch", response_model=ApiResponse,
            summary="Obtener cambios aprobados listos para aplicar")
async def get_approved_batch():
    from app.change_manager import get_change_manager
    changes = get_change_manager().get_approved_batch()
    return ApiResponse(ok=True, data={"changes": changes, "count": len(changes)})


@router.post("/changes/approve", response_model=ApiResponse,
             summary="Aprobar cambio(s) pendiente(s)")
async def approve_change(request: Request):
    from app.change_manager import get_change_manager

    body = await request.json()
    cm   = get_change_manager()

    if body.get("approve_all"):
        count = await cm.approve_all_pending()
        return ApiResponse(ok=True, message=f"{count} cambios aprobados")

    change_id = body.get("change_id")
    if not change_id:
        raise HTTPException(status_code=400, detail="change_id requerido")
    if not await cm.approve_change(change_id):
        raise HTTPException(status_code=404, detail="Cambio no encontrado")
    return ApiResponse(ok=True, message="Cambio aprobado")


@router.post("/changes/reject", response_model=ApiResponse,
             summary="Rechazar cambio(s) pendiente(s)")
async def reject_change(request: Request):
    from app.change_manager import get_change_manager

    body = await request.json()
    cm   = get_change_manager()

    if body.get("reject_all"):
        count = await cm.reject_all_pending()
        return ApiResponse(ok=True, message=f"{count} cambios rechazados")

    change_id = body.get("change_id")
    if not change_id:
        raise HTTPException(status_code=400, detail="change_id requerido")
    if not await cm.reject_change(change_id):
        raise HTTPException(status_code=404, detail="Cambio no encontrado")
    return ApiResponse(ok=True, message="Cambio rechazado")


@router.post("/changes/apply", response_model=ApiResponse,
             summary="Aplicar cambios aprobados")
async def apply_changes(request: Request):
    from app.change_manager import get_change_manager

    result = await get_change_manager().apply_approved_changes()
    if result.get("status") == "failed":
        return ApiResponse(ok=False, message=result.get("error"))
    return ApiResponse(ok=True, message="Cambios aplicados", data=result)


@router.get("/changes/stats", response_model=ApiResponse,
            summary="Obtener estadísticas de ChangeManager")
async def get_change_stats():
    from app.change_manager import get_change_manager
    return ApiResponse(ok=True, data=get_change_manager().get_stats())
