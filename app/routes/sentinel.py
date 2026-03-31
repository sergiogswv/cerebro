"""
routes/sentinel.py — Endpoints de Sentinel (monitoreo, análisis Pro, monitor).

Rutas:
  GET  /sentinel/config
  POST /sentinel/config
  POST /sentinel/init
  POST /sentinel/command        ← acciones Pro
  POST /sentinel/monitor/pause
  POST /sentinel/monitor/daily-report
  GET  /sentinel/monitor/metrics
  POST /sentinel/monitor/testing
  POST /sentinel/monitor/reset-config
"""

import logging
import os
import uuid
from fastapi import APIRouter, HTTPException, Request
from app.models import ApiResponse
from app.orchestrator import orchestrator
from app.dispatcher import send_raw_command

router = APIRouter(tags=["sentinel"])
logger = logging.getLogger("cerebro.routes.sentinel")

_ready: dict[str, bool] = {"sentinel": False}

_PRO_MESSAGES = {
    "check":        "🔍 Quick Check: Análisis estático rápido en progreso...",
    "audit":        "🛡️ Audit: Auditoría con correcciones IA en progreso...",
    "report":       "📊 Report: Generando reporte de calidad...",
    "fix":          "⚡ Auto Fix: Corrigiendo bugs automáticamente...",
    "review":       "🔄 Review: Realizando review de arquitectura...",
    "clean-cache":  "🗑️ Clean Cache: Limpiando caché de IA...",
}


# ─── Config ───────────────────────────────────────────────────────────────────

@router.get("/sentinel/config", response_model=ApiResponse)
async def get_sentinel_config():
    result = await orchestrator.get_sentinel_config()
    if isinstance(result, dict) and "error" in result:
        return ApiResponse(ok=False, message=result["error"])
    return ApiResponse(ok=True, data=result)


@router.post("/sentinel/config", response_model=ApiResponse)
async def save_sentinel_config(request: Request):
    result = await orchestrator.save_sentinel_config(await request.json())
    if result.get("status") == "error":
        return ApiResponse(ok=False, message=result.get("message"))
    return ApiResponse(ok=True, message="Configuración de Sentinel guardada exitosamente")


@router.post("/sentinel/init", response_model=ApiResponse,
             summary="Lanzar Wizard de Sentinel para el proyecto activo")
async def sentinel_init():
    result = await orchestrator.sentinel_init()
    if isinstance(result, dict) and "error" in result:
        return ApiResponse(ok=False, message=result["error"])
    return ApiResponse(ok=True, message="Proceso de inicialización de Sentinel lanzado")


# ─── Pro Command ──────────────────────────────────────────────────────────────

@router.post("/sentinel/command", response_model=ApiResponse,
             summary="Enviar comando Pro a Sentinel")
async def sentinel_command(request: Request):
    from app.sockets import emit_agent_event

    data       = await request.json()
    action     = data.get("action", "pro")
    subcommand = data.get("subcommand", "check")
    target     = data.get("target", orchestrator.active_project)

    project_path = "."
    if target and target != "Ninguno":
        project_path = os.path.join(orchestrator.workspace_root, target).replace("\\", "/")

    ack = await send_raw_command("sentinel", {
        "action": action, "subcommand": subcommand,
        "target": project_path,
        "request_id": f"sentinel-{uuid.uuid4().hex[:8]}",
    })
    if isinstance(ack, dict) and ack.get("status") == "rejected":
        return ApiResponse(ok=False, message=ack.get("error", "Comando rechazado"))

    if not _ready["sentinel"]:
        _ready["sentinel"] = True
        await emit_agent_event({
            "source": "sentinel", "type": "sentinel_ready", "severity": "info",
            "payload": {"ready": True, "message": "Sentinel está listo para monitoreo"},
        })

    result_status = "error" if (isinstance(ack, dict) and ack.get("status") == "error") else "completed"
    is_file = target and "." in target.split("/")[-1]

    await emit_agent_event({
        "source": "sentinel",
        "type":   f"pro_{subcommand.replace('-', '_')}",
        "severity": "info",
        "payload": {
            "action": subcommand, "target": target,
            "scope":  "file" if is_file else "project",
            "status": result_status,
            "message": _PRO_MESSAGES.get(subcommand, f"🚀 Ejecutando: {subcommand}"),
            "result": ack.get("result") if isinstance(ack, dict) else None,
        },
    })
    return ApiResponse(ok=True, message=f"Comando '{subcommand}' enviado a Sentinel", data=ack)


# ─── Monitor Commands ─────────────────────────────────────────────────────────

async def _sentinel_monitor(action: str, target: str, request: Request):
    """Helper que envía un comando monitor/* a Sentinel y emite el evento WS."""
    from app.sockets import emit_agent_event

    try:
        body = await request.json() if await request.body() else {}
    except Exception:
        body = {}

    proj = body.get("target", target) if body else target

    # Para daily-report / testing convertimos nombre → ruta completa
    t = "."
    if proj and proj != "Ninguno":
        full = os.path.join(orchestrator.workspace_root, proj)
        t = full.replace("\\", "/") if os.path.isabs(full) else proj

    ack = await send_raw_command("sentinel", {
        "action": f"monitor/{action}", "target": t,
        "request_id": f"{action}-{uuid.uuid4().hex[:8]}",
    })

    event_type = action.replace("-", "_")
    msg_map = {
        "pause":        "Estado del monitoreo actualizado",
        "daily-report": "Reporte diario de productividad generado",
        "metrics":      "Métricas de Sentinel obtenidas",
        "testing":      "Sugerencias de testing generadas",
        "reset-config": "Reiniciando configuración de Sentinel...",
    }
    severity = "warning" if action == "reset-config" else "info"

    await emit_agent_event({
        "source": "sentinel", "type": event_type, "severity": severity,
        "payload": {"message": msg_map.get(action, action), "result": ack},
    })
    return ack


@router.post("/sentinel/monitor/pause", response_model=ApiResponse)
async def sentinel_monitor_pause(request: Request):
    ack = await _sentinel_monitor("pause", orchestrator.active_project, request)
    return ApiResponse(ok=True, message="Comando pause enviado a Sentinel", data=ack)


@router.post("/sentinel/monitor/daily-report", response_model=ApiResponse)
async def sentinel_monitor_daily_report(request: Request):
    if not orchestrator.active_project:
        raise HTTPException(status_code=400, detail="No hay proyecto activo seleccionado")
    ack = await _sentinel_monitor("daily-report", orchestrator.active_project, request)
    return ApiResponse(ok=True, message="Reporte diario solicitado", data=ack)


@router.get("/sentinel/monitor/metrics", response_model=ApiResponse)
async def sentinel_monitor_metrics():
    from app.sockets import emit_agent_event
    metrics = {"bugs_evitados": 0, "costo_acumulado": 0.0, "tokens_usados": 0, "tiempo_ahorrado_mins": 0}
    await emit_agent_event({
        "source": "sentinel", "type": "metrics", "severity": "info",
        "payload": {"message": "Métricas de Sentinel obtenidas", "metrics": metrics},
    })
    return ApiResponse(ok=True, message="Métricas obtenidas", data=metrics)


@router.post("/sentinel/monitor/testing", response_model=ApiResponse)
async def sentinel_monitor_testing(request: Request):
    if not orchestrator.active_project:
        raise HTTPException(status_code=400, detail="No hay proyecto activo seleccionado")
    ack = await _sentinel_monitor("testing", orchestrator.active_project, request)
    return ApiResponse(ok=True, message="Sugerencias de testing solicitadas", data=ack)


@router.post("/sentinel/monitor/reset-config", response_model=ApiResponse)
async def sentinel_monitor_reset_config(request: Request):
    ack = await _sentinel_monitor("reset-config", orchestrator.active_project, request)
    return ApiResponse(ok=True, message="Reinicio de configuración solicitado", data=ack)
