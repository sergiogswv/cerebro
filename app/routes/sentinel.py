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
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from app.models import ApiResponse
from app.orchestrator import orchestrator
from app.dispatcher import send_raw_command
from app.sockets import emit_agent_event

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

def _upsert_env(text: str, key: str, val: str) -> str:
    """Reemplaza o agrega una variable en el archivo .env."""
    import re
    pattern = rf'^{key}=.*$'
    replacement = f'{key}={val}'
    if re.search(pattern, text, flags=re.MULTILINE):
        return re.sub(pattern, replacement, text, flags=re.MULTILINE)
    return text + f'\n{replacement}'


@router.get("/sentinel/config", response_model=ApiResponse)
async def get_sentinel_config():
    """Retorna configuración de Sentinel incluyendo modo (core/adk)."""
    from app.config import get_settings
    s = get_settings()

    result = await orchestrator.get_sentinel_config()
    if isinstance(result, dict) and "error" in result:
        return ApiResponse(ok=False, message=result["error"])

    # Agregar información de modo
    result["sentinel_mode"] = s.sentinel_mode
    result["sentinel_adk_url"] = s.sentinel_adk_url
    result["active_url"] = s.sentinel_adk_url if s.sentinel_mode == "adk" else s.sentinel_url

    return ApiResponse(ok=True, data=result)


@router.post("/sentinel/config", response_model=ApiResponse)
async def save_sentinel_config(request: Request):
    data = await request.json()

    # Handle mode switch (core/adk) if provided
    new_mode = data.get("sentinel_mode")
    new_provider = data.get("sentinel_llm_provider")
    ollama_url = data.get("ollama_base_url")
    ollama_model = data.get("ollama_model")

    if new_mode:
        if new_mode not in ("core", "adk"):
            raise HTTPException(status_code=400, detail="sentinel_mode debe ser 'core' o 'adk'")

        env_path = Path(__file__).parent.parent.parent / ".env"
        env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""

        env_text = _upsert_env(env_text, "SENTINEL_MODE", new_mode)
        if new_provider:
            env_text = _upsert_env(env_text, "SENTINEL_LLM_PROVIDER", new_provider)
        if ollama_url:
            env_text = _upsert_env(env_text, "OLLAMA_BASE_URL", ollama_url)
        if ollama_model:
            env_text = _upsert_env(env_text, "OLLAMA_MODEL", ollama_model)
        env_path.write_text(env_text, encoding="utf-8")

        await emit_agent_event({
            "source": "sentinel", "type": "config_updated", "severity": "info",
            "payload": {
                "sentinel_mode": new_mode, "sentinel_llm_provider": new_provider,
                "message": f"Sentinel → modo '{new_mode}'. Reinicia Cerebro para aplicar.",
            },
        })
        return ApiResponse(ok=True,
                           message=f"Guardado. Reinicia Cerebro para activar modo '{new_mode}'.",
                           data={"sentinel_mode": new_mode})

    # Legacy: save sentinel config via orchestrator
    result = await orchestrator.save_sentinel_config(data)
    if result.get("status") == "error":
        return ApiResponse(ok=False, message=result.get("message"))
    return ApiResponse(ok=True, message="Configuración de Sentinel guardada exitosamente")


@router.post("/sentinel/init", response_model=ApiResponse,
             summary="Lanzar Wizard de Sentinel para el proyecto activo")
async def sentinel_init():
    result = await orchestrator.sentinel_init()
    if isinstance(result, dict) and "error" in result:
        return ApiResponse(ok=False, message=result["error"])
    # Devolver también el wizard_id para que el frontend pueda hacer seguimiento
    return ApiResponse(ok=True, message="Proceso de inicialización de Sentinel lanzado", data=result)


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

    # Obtener configuración para determinar si auto_mode está habilitado
    from app.config_manager import UnifiedConfigManager
    manager = UnifiedConfigManager.get_instance()
    unified_config = manager.get_config()
    cerebro_config = unified_config.cerebro if hasattr(unified_config, 'cerebro') else None
    is_auto = cerebro_config.auto_fix_enabled if cerebro_config else False

    logger.info(f"🔧 Sentinel command: subcommand={subcommand}, auto_mode={is_auto}")

    # Enviar siempre al Core (sentinel_core), no al ADK
    # Los comandos pro/monitor solo existen en el Core, no en el ADK
    ack = await send_raw_command("sentinel_core", {
        "action": action, "subcommand": subcommand,
        "target": project_path,
        "request_id": f"sentinel-{uuid.uuid4().hex[:8]}",
        "options": {"auto": is_auto},
    })
    if isinstance(ack, dict) and ack.get("status") == "rejected":
        return ApiResponse(ok=False, message=ack.get("error", "Comando rechazado"))

    # Emitir sentinel_ready la primera vez que Sentinel responde exitosamente
    # (Esto asegura que el badge "ready" aparezca en el Dashboard)
    if not _ready["sentinel"]:
        _ready["sentinel"] = True
        await emit_agent_event({
            "source": "sentinel", "type": "sentinel_ready", "severity": "info",
            "payload": {"ready": True, "message": "Sentinel Core está listo para análisis"},
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

    # Obtener configuración para determinar si auto_mode está habilitado
    from app.config_manager import UnifiedConfigManager
    manager = UnifiedConfigManager.get_instance()
    unified_config = manager.get_config()
    cerebro_config = unified_config.cerebro if hasattr(unified_config, 'cerebro') else None
    is_auto = cerebro_config.auto_fix_enabled if cerebro_config else False

    # Enviar siempre al Core (sentinel_core), no al ADK
    # Los comandos monitor/* solo existen en el Core, no en el ADK
    ack = await send_raw_command("sentinel_core", {
        "action": f"monitor/{action}", "target": t,
        "request_id": f"{action}-{uuid.uuid4().hex[:8]}",
        "options": {"auto": is_auto},
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


@router.get("/sentinel/memory", response_model=ApiResponse,
             summary="Obtener contexto de memoria de Sentinel")
async def get_sentinel_memory():
    """Retorna datos históricos de análisis de Sentinel desde ContextDB."""
    try:
        # Obtener patrones y hallazgos recientes del ContextDB
        patterns = orchestrator.context_db.get_recent_patterns(
            source_filter="sentinel_analysis",
            limit=20
        )

        # Construir lista de archivos calientes (hot files)
        hot_files = []
        file_scores = {}
        for p in patterns:
            fp = p.get("file_path")
            if fp:
                if fp not in file_scores:
                    file_scores[fp] = {"count": 0, "severity": p.get("severity", "info"), "events": []}
                file_scores[fp]["count"] += 1
                file_scores[fp]["events"].append(p)

        # Ordenar por frecuencia y severidad
        sorted_files = sorted(file_scores.items(), key=lambda x: (x[1]["count"], x[1]["severity"]), reverse=True)
        for fp, data in sorted_files[:10]:
            hot_files.append({
                "file_path": fp,
                "total_events": data["count"],
                "severity": data["severity"],
            })

        # Hallazgos críticos recientes
        critical_findings = [
            {
                "event_type": p.get("pattern_type", "finding"),
                "timestamp": p.get("created_at", ""),
                "severity": p.get("severity", "info"),
                "file": p.get("file_path", ""),
            }
            for p in patterns if p.get("severity") in ("critical", "error")
        ][:5]

        return ApiResponse(ok=True, data={
            "hot_files": hot_files,
            "recent_findings": critical_findings,
            "total_patterns": len(patterns),
        })
    except Exception as e:
        logger.exception("Error obteniendo memoria de Sentinel")
        return ApiResponse(ok=False, message=str(e))
