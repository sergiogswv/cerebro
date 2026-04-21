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
    from app.sockets import emit_agent_event
    from datetime import datetime, timezone

    body          = await request.json()
    event_id      = body.get("event_id")
    feedback_type = body.get("feedback_type")
    reason        = body.get("reason")
    suggested     = body.get("suggested_action")
    decision_context = body.get("decision_context", {})

    if not event_id or not feedback_type:
        raise HTTPException(status_code=400, detail="event_id y feedback_type son requeridos")

    # Normalizar feedback_type: el dashboard envía 'approval'/'rejection',
    # el sistema interno usa 'thumbs_up'/'thumbs_down'
    _ALIAS_MAP = {
        "approval": "thumbs_up",
        "rejection": "thumbs_down",
        "approve": "thumbs_up",
        "reject": "thumbs_down",
    }
    normalized_type = _ALIAS_MAP.get(feedback_type.lower(), feedback_type)

    feedback_id = get_context_db().record_feedback(
        event_id=event_id, feedback_type=normalized_type,
        reason=reason, suggested_action=suggested,
    )

    # Emitir evento de vuelta al dashboard para animar el timeline
    is_positive = normalized_type == "thumbs_up"
    await emit_agent_event({
        "source": "cerebro",
        "type": "decision_feedback_recorded",
        "severity": "info",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "original_event_id": event_id,
            "feedback_type": normalized_type,
            "feedback_id": feedback_id,
            "is_positive": is_positive,
            "message": f"🧠 Feedback {'aprobado' if is_positive else 'rechazado'} registrado para evento {event_id[:8]}...",
            **decision_context,
        }
    })

    logger.info(f"📊 Feedback registrado: {normalized_type} para evento {event_id} → ID {feedback_id}")
    return ApiResponse(ok=True, message="Feedback registrado", data={"feedback_id": feedback_id, "normalized_type": normalized_type})


@router.get("/learn", response_model=ApiResponse,
            summary="Analizar feedback y sugerir ajustes de reglas")
async def get_learning_suggestions(limit: int = 100):
    from app.context_db import get_context_db
    return ApiResponse(ok=True, data=get_context_db().analyze_learning(limit=limit))


@router.post("/learn/apply", response_model=ApiResponse,
             summary="Forzar ciclo de aprendizaje y aplicar reglas al DecisionEngine")
async def force_learning_cycle(request: Request):
    """
    TASK-04: Dispara manualmente el ciclo de auto-aprendizaje.
    Útil para probar el sistema sin esperar las 24h del scheduler.
    Acepta opcionalmente: min_samples (int), min_consistency (float).
    """
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    min_samples     = body.get("min_samples", 5)
    min_consistency = body.get("min_consistency", 0.70)

    try:
        from app.context_db import get_context_db
        from app.orchestrator import orchestrator
        from app.sockets import emit_agent_event
        from datetime import datetime, timezone

        db       = get_context_db()
        analysis = db.analyze_learning(limit=500)
        rule_adjustments = analysis.get("rule_adjustments", [])

        result = orchestrator.decision_engine.apply_learned_adjustments(
            rule_adjustments,
            min_samples=min_samples,
            min_consistency=min_consistency,
        )

        # Emitir al dashboard
        await emit_agent_event({
            "source":    "cerebro",
            "type":      "learning_cycle_completed",
            "severity":  "info",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": {
                "triggered_by": "manual_api",
                "total_feedback":   analysis.get("total_feedback", 0),
                "applied_count":    result["applied_count"],
                "skipped_count":    result["skipped_count"],
                "applied":          result["applied"],
                "message":          (
                    f"🧠 Ciclo manual: {result['applied_count']} reglas aplicadas, "
                    f"{result['skipped_count']} sin suficientes datos."
                ),
            }
        })

        return ApiResponse(
            ok=True,
            message=f"{result['applied_count']} reglas aplicadas al DecisionEngine",
            data={
                "analysis_summary": {
                    "total_feedback":   analysis.get("total_feedback", 0),
                    "positive":         analysis.get("positive_feedback_count", 0),
                    "negative":         analysis.get("negative_feedback_count", 0),
                    "rule_candidates":  len(rule_adjustments),
                },
                **result,
            }
        )

    except Exception as e:
        logger.exception(f"Error en ciclo de aprendizaje manual: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/learn/thresholds", response_model=ApiResponse,
            summary="Umbrales de confianza activos del DecisionEngine (aprendidos)")
async def get_learned_thresholds():
    """
    Retorna los umbrales de confianza que el DecisionEngine tiene activos.
    Los umbrales por defecto son 0.8 (80%). Los aprendidos pueden diferir.
    """
    from app.orchestrator import orchestrator
    thresholds = orchestrator.decision_engine.get_learned_thresholds()
    default    = orchestrator.decision_engine.rules.get("autofix_rules", {}).get("confidence_threshold", 0.8)
    return ApiResponse(ok=True, data={
        "default_threshold": default,
        "learned_thresholds": thresholds,
        "total_overrides": len(thresholds),
    })


@router.get("/learn/stats", response_model=ApiResponse,
            summary="Resumen completo del sistema de aprendizaje")
async def get_learning_stats():
    """Combina feedback stats, reglas aprendidas y umbrales activos en un solo endpoint."""
    from app.context_db import get_context_db
    from app.orchestrator import orchestrator

    db = get_context_db()
    analysis   = db.analyze_learning(limit=200)
    raw_stats  = db.get_feedback_stats() # Get global totals including orphans
    rules      = db.get_learned_rules(active_only=True) if hasattr(db, "get_learned_rules") else []
    thresholds = orchestrator.decision_engine.get_learned_thresholds()

    return ApiResponse(ok=True, data={
        "feedback": {
            "total":    raw_stats.get("total", 0),
            "positive": raw_stats.get("thumbs_up", 0),
            "negative": raw_stats.get("thumbs_down", 0),
        },
        "patterns_analyzed": len(analysis.get("patterns", {})),
        "rule_candidates":   len(analysis.get("rule_adjustments", [])),
        "applied_rules":     len(rules),
        "active_thresholds": thresholds,
        "suggestions":       analysis.get("suggestions", [])[:5],  # Top 5
    })


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

    # continue_pipeline: si True, Cerebro evalúa si continuar con Architect/Warden
    continue_pipeline = body.get("continue_pipeline", False)

    if not await cm.reject_change(change_id, continue_pipeline):
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
