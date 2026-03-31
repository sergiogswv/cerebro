"""
routes/architect.py — Endpoints de Architect (análisis de arquitectura + IA).

Rutas:
  GET  /config
  POST /config
  GET  /patterns
  GET  /ai-config
  POST /ai-config
  POST /validate-ai
  POST /init
  GET  /ai-rules
  GET  /ai-suggestions
  POST /command
"""

import logging
import os
import uuid
from fastapi import APIRouter, Request
from app.models import ApiResponse
from app.orchestrator import orchestrator
from app.dispatcher import send_raw_command

router = APIRouter(tags=["architect"])
logger = logging.getLogger("cerebro.routes.architect")

# Shared ready-status tracker (module-level singleton)
_ready: dict[str, bool] = {"architect": False}


@router.get("/config", response_model=ApiResponse)
async def get_architect_config():
    result = await orchestrator.get_architect_config()
    if isinstance(result, dict) and "error" in result:
        return ApiResponse(ok=False, message=result["error"])
    return ApiResponse(ok=True, data=result)


@router.post("/config", response_model=ApiResponse)
async def save_architect_config(request: Request):
    config = await request.json()
    result = await orchestrator.save_architect_config(config)
    if result.get("status") == "error":
        return ApiResponse(ok=False, message=result.get("message"))
    return ApiResponse(ok=True, message="Configuración guardada exitosamente")


@router.get("/patterns", response_model=ApiResponse)
async def get_architect_patterns():
    patterns = await orchestrator.get_architect_patterns()
    return ApiResponse(ok=True, data=patterns)


@router.get("/ai-config", response_model=ApiResponse)
async def get_ai_config():
    return ApiResponse(ok=True, data=await orchestrator.get_ai_config())


@router.post("/ai-config", response_model=ApiResponse)
async def save_ai_config(request: Request):
    result = await orchestrator.save_ai_config(await request.json())
    if result.get("status") == "error":
        return ApiResponse(ok=False, message=result.get("message"))
    return ApiResponse(ok=True, message="Configuración de IA guardada")


@router.post("/validate-ai", response_model=ApiResponse)
async def validate_ai(request: Request):
    data = await request.json()
    logger.info(f"📥 /validate-ai request: provider={data.get('provider')}, url={data.get('url')}, key={'SET' if data.get('key') else 'NOT SET'}")
    result = await orchestrator.validate_ai_provider(
        data.get("url"), data.get("key"), data.get("provider")
    )
    logger.info(f"📤 /validate-ai result: ok={result.get('ok')}, error={result.get('error')}")
    if result.get("ok"):
        return ApiResponse(ok=True, data=result.get("models"))
    return ApiResponse(ok=False, message=result.get("error"))


@router.post("/init", response_model=ApiResponse,
             summary="Lanzar Wizard de Architect para el proyecto activo")
async def architect_init(request: Request):
    data = await request.json()
    result = await orchestrator.architect_init(pattern=data.get("pattern"))
    if isinstance(result, dict) and "error" in result:
        return ApiResponse(ok=False, message=result["error"])
    return ApiResponse(ok=True, message="Proceso de inicialización lanzado")


@router.get("/ai-rules", response_model=ApiResponse,
            summary="Generar reglas con IA para un patrón")
async def get_ai_rules(pattern: str = None, project: str = None):
    logger.info(f"🔍 /ai-rules pattern={pattern}, project={project}")
    try:
        result = await orchestrator.generate_ai_rules_for_pattern(pattern, project)
        if isinstance(result, dict) and "error" in result:
            return ApiResponse(ok=False, message=result["error"])
        return ApiResponse(ok=True, data=result)
    except Exception:
        logger.exception("❌ Excepción en ai-rules")
        raise


@router.get("/ai-suggestions", response_model=ApiResponse,
            summary="Obtener sugerencias de arquitecturas desde IA")
async def get_ai_suggestions(project: str = None):
    result = await orchestrator.get_ai_architecture_suggestions(project)
    if isinstance(result, dict) and "error" in result:
        return ApiResponse(ok=False, message=result["error"])
    return ApiResponse(ok=True, data=result)


@router.post("/command", response_model=ApiResponse,
             summary="Enviar comando a Architect")
async def architect_command(request: Request):
    from app.sockets import emit_agent_event

    data   = await request.json()
    action = data.get("action", "lint")
    target = data.get("target", orchestrator.active_project)

    project_path = "."
    if target and target != "Ninguno":
        project_path = os.path.join(orchestrator.workspace_root, target).replace("\\", "/")

    ack = await send_raw_command("architect", {
        "action": action, "target": project_path,
        "request_id": f"architect-{uuid.uuid4().hex[:8]}",
    })
    if isinstance(ack, dict) and ack.get("status") == "rejected":
        return ApiResponse(ok=False, message=ack.get("error", "Comando rechazado"))

    if not _ready["architect"]:
        _ready["architect"] = True
        await emit_agent_event({
            "source": "architect", "type": "architect_ready", "severity": "info",
            "payload": {"ready": True, "message": "Architect está listo para análisis"},
        })

    _messages = {
        "lint":            "🔍 Análisis de linting en progreso...",
        "deep-analysis":   "🧠 Análisis profundo de arquitectura iniciado...",
        "check-circular":  "🔄 Buscando dependencias circulares...",
        "full-report":     "📊 Generando reporte completo de arquitectura...",
        "validate-config": "✅ Validando configuración de architect.json...",
        "analyze-stale":   "🕰️ Buscando archivos stale con alta complejidad...",
    }

    result_status = "completed"
    if isinstance(ack, dict):
        s = ack.get("status", "")
        if s == "error":
            result_status = "error"

    await emit_agent_event({
        "source": "architect",
        "type":   f"command_{action.replace('-', '_')}",
        "severity": "info",
        "payload": {
            "action": action, "target": target, "status": result_status,
            "message": _messages.get(action, f"🚀 Ejecutando: {action}"),
            "result": ack.get("result") if isinstance(ack, dict) else None,
        },
    })
    return ApiResponse(ok=True, message=f"Comando '{action}' enviado a Architect", data=ack)
