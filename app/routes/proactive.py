"""
routes/proactive.py — Endpoints de control del Modo Proactivo/Autómata.

Rutas:
  GET  /proactive/status        — Estado actual del scheduler + modo nocturno
  GET  /proactive/config        — Configuración actual
  POST /proactive/config        — Actualizar configuración en caliente
  POST /proactive/pause         — Pausar análisis proactivos
  POST /proactive/resume        — Reanudar análisis proactivos
  POST /proactive/trigger       — Disparar análisis inmediato (modo manual)
  GET  /proactive/night-summary — Resumen del batch nocturno más reciente
"""

import logging
from fastapi import APIRouter, HTTPException, Request
from app.models import ApiResponse
from app.proactive_scheduler import get_proactive_scheduler

router = APIRouter(prefix="/proactive", tags=["proactive"])
logger = logging.getLogger("cerebro.routes.proactive")


@router.get("/status", response_model=ApiResponse, summary="Estado del Scheduler Proactivo")
async def get_proactive_status():
    """Devuelve el estado actual del scheduler y si está en modo nocturno."""
    scheduler = get_proactive_scheduler()
    return ApiResponse(ok=True, message="Estado proactivo", data=scheduler.get_status())


@router.get("/config", response_model=ApiResponse, summary="Obtener configuración proactiva")
async def get_proactive_config(project: str = None):
    """Devuelve la configuración del modo proactivo para el proyecto activo."""
    try:
        from app.orchestrator import orchestrator
        target_project = project or orchestrator.active_project or "default"
        scheduler = get_proactive_scheduler()
        config = scheduler.get_config(target_project)
        return ApiResponse(ok=True, message="Configuración proactiva", data={
            "project": target_project,
            "config": config,
        })
    except Exception as e:
        logger.exception("Error obteniendo config proactiva")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/config", response_model=ApiResponse, summary="Actualizar configuración proactiva en caliente")
async def update_proactive_config(request: Request):
    """
    Actualiza la configuración proactiva en caliente.
    El scheduler adapta su comportamiento inmediatamente sin reiniciar.
    """
    try:
        body = await request.json()
        project = body.get("project")
        config = body.get("config")

        if not config:
            raise HTTPException(status_code=400, detail="'config' es requerido")

        from app.orchestrator import orchestrator
        target_project = project or orchestrator.active_project or "default"

        scheduler = get_proactive_scheduler()
        # Merge con config existente (no reemplazar completamente)
        existing = scheduler.get_config(target_project)
        _deep_merge(existing, config)
        scheduler.save_config(target_project, existing)
        # Actualizar config en memoria del scheduler activo
        scheduler.config = existing

        logger.info(f"✅ Config proactiva actualizada para '{target_project}'")
        return ApiResponse(ok=True, message="Configuración actualizada", data={
            "project": target_project,
            "config": existing,
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error actualizando config proactiva")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/pause", response_model=ApiResponse, summary="Pausar el scheduler proactivo")
async def pause_scheduler():
    """Pausa el scheduler. Los análisis en curso terminan pero no se lanzan nuevos."""
    scheduler = get_proactive_scheduler()
    scheduler.pause()
    return ApiResponse(ok=True, message="Scheduler pausado", data={"state": scheduler.state})


@router.post("/resume", response_model=ApiResponse, summary="Reanudar el scheduler proactivo")
async def resume_scheduler():
    """Reanuda el scheduler después de una pausa."""
    scheduler = get_proactive_scheduler()
    scheduler.resume()
    return ApiResponse(ok=True, message="Scheduler reanudado", data={"state": scheduler.state})


@router.post("/trigger", response_model=ApiResponse, summary="Trigger manual de análisis inmediato")
async def trigger_analysis(request: Request):
    """
    Dispara un análisis proactivo inmediato sin esperar al intervalo programado.
    Body: { "mode": "hot_files" | "debt_analysis" | "new_implementation" }
    """
    try:
        body = await request.json()
        mode = body.get("mode", "hot_files")

        valid_modes = ["hot_files", "debt_analysis", "new_implementation"]
        if mode not in valid_modes:
            raise HTTPException(
                status_code=400,
                detail=f"Modo inválido. Opciones: {valid_modes}"
            )

        scheduler = get_proactive_scheduler()
        if not scheduler._running:
            # Iniciar scheduler si no está corriendo
            from app.orchestrator import orchestrator
            project = orchestrator.active_project or "default"
            await scheduler.start(project)

        await scheduler.trigger_now(mode=mode)

        return ApiResponse(ok=True, message=f"Análisis '{mode}' triggereado", data={
            "mode": mode,
            "batch_id": scheduler._current_batch_id,
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error en trigger manual")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/night-summary", response_model=ApiResponse, summary="Resumen del análisis nocturno")
async def get_night_summary(project: str = None):
    """
    Devuelve un resumen del batch nocturno más reciente:
    cuántos autofixes se aplicaron, cuántos están pendientes de revisión, etc.
    """
    try:
        import sqlite3
        from app.orchestrator import orchestrator
        from app.proactive_scheduler import get_proactive_scheduler

        target_project = project or orchestrator.active_project or "default"
        scheduler = get_proactive_scheduler()

        with sqlite3.connect(scheduler.db_path) as conn:
            rows = conn.execute(
                """SELECT status, last_autofix_result, COUNT(*) as cnt
                   FROM proactive_analysis_state
                   WHERE project = ? AND last_analyzed_at >= datetime('now', '-12 hours')
                   GROUP BY status, last_autofix_result""",
                (target_project,)
            ).fetchall()

        summary = {
            "autofixes_applied": 0,
            "pending_review": 0,
            "failed": 0,
            "analyzed": 0,
        }
        for status, result, cnt in rows:
            if status == "autofixed" and result == "success":
                summary["autofixes_applied"] += cnt
            elif status == "autofixed" and result == "pending_review":
                summary["pending_review"] += cnt
            elif status == "failed" or result == "failed":
                summary["failed"] += cnt
            else:
                summary["analyzed"] += cnt

        has_activity = summary["autofixes_applied"] > 0 or summary["pending_review"] > 0

        # Si el proyecto es 'default' (no seleccionado) y no hay nada real, no molestar
        if target_project == "default" and not has_activity:
            return ApiResponse(ok=True, message="Sin actividad relevante", data={
                "has_activity": False,
                "night_mode_active": scheduler.is_night_mode_active()
            })

        return ApiResponse(ok=True, message="Resumen nocturno", data={
            "project": target_project,
            "has_activity": has_activity,
            "night_mode_active": scheduler.is_night_mode_active(),
            **summary,
        })
    except Exception as e:
        logger.exception("Error obteniendo resumen nocturno")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────

def _deep_merge(base: dict, override: dict) -> dict:
    """Merge recursivo: override aplica encima de base sin borrar claves omitidas."""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base
