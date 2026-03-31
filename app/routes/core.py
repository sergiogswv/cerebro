"""
routes/core.py — Endpoints fundamentales del Orquestador Cerebro.

Rutas:
  GET  /health
  GET  /status
  POST /events
  POST /bootstrap
  GET  /projects
  POST /select-project
  POST /command/{agent}   (proxy transparente)
  POST /interaction-response
"""

import logging
from fastapi import APIRouter, HTTPException, Request
from app.models import AgentEvent, ApiResponse, OrchestratorCommand
from app.orchestrator import orchestrator
from app.dispatcher import send_command, send_raw_command

router = APIRouter(tags=["core"])
logger = logging.getLogger("cerebro.routes.core")


@router.get("/health", summary="Health check del Orquestador")
async def health():
    return {"status": "ok", "service": "cerebro"}


@router.get("/status", response_model=ApiResponse, summary="Estado actual del Orquestador")
async def get_status():
    return ApiResponse(ok=True, message="Estado obtenido", data={
        "active_project":  orchestrator.active_project,
        "workspace_root":  orchestrator.workspace_root,
    })


@router.post("/events", response_model=ApiResponse, summary="Recibir evento de un agente")
async def receive_event(event: AgentEvent):
    """Endpoint principal. Los agentes envían sus eventos aquí."""
    try:
        logger.info(f"🔔 EVENTO [{event.source}] {event.type}: {event.payload}")
        result = await orchestrator.handle_event(event)
        return ApiResponse(ok=True, message="evento procesado", data=result)
    except Exception as e:
        logger.exception("Error procesando evento")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/bootstrap", response_model=ApiResponse, summary="Iniciar proceso de selección de proyecto")
async def bootstrap():
    result = await orchestrator.bootstrap()
    return ApiResponse(ok=True, message="Bootstrap iniciado", data=result)


@router.get("/projects", response_model=ApiResponse, summary="Listar proyectos disponibles")
async def get_projects():
    import os
    projects = [
        d for d in os.listdir(orchestrator.workspace_root)
        if os.path.isdir(os.path.join(orchestrator.workspace_root, d)) and not d.startswith(".")
    ]
    return ApiResponse(ok=True, message="Proyectos obtenidos", data={"projects": sorted(projects)})


@router.post("/select-project", response_model=ApiResponse, summary="Establecer proyecto activo")
async def select_project(request: Request):
    body = await request.json()
    project_name = body.get("project")
    if not project_name:
        raise HTTPException(status_code=400, detail="project_name requerido")
    result = await orchestrator.set_active_project(project_name)
    return ApiResponse(ok=True, message="Proyecto seleccionado", data=result)


@router.post("/command/{agent}", response_model=ApiResponse, summary="Enviar comando a un agente")
async def dispatch_command(agent: str, request: Request):
    """Proxy transparente: reenvía el JSON tal cual al agente."""
    body = await request.json()
    ack = await send_raw_command(agent, body)
    return ApiResponse(ok=True, message=f"comando enviado a {agent}", data=ack)


@router.post("/interaction-response", response_model=ApiResponse, summary="Responder a una interacción de usuario")
async def interaction_response(request: Request):
    from app.sockets import pending_interaction_events

    body = await request.json()
    prompt_id = body.get("prompt_id")
    answer = body.get("answer")

    if not prompt_id or not answer:
        raise HTTPException(status_code=400, detail="prompt_id y answer son requeridos")

    pending_interaction_events[:] = [
        e for e in pending_interaction_events
        if e.get("payload", {}).get("prompt_id") != prompt_id
    ]
    logger.info(f"✅ Evento de interacción {prompt_id} respondido y eliminado de pendientes")

    await send_command(
        "sentinel",
        OrchestratorCommand(action="answer", options={"prompt_id": prompt_id, "answer": answer})
    )
    return ApiResponse(ok=True, message="Respuesta de interacción enviada")
