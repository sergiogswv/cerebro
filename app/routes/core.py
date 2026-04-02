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
    from app.orchestrator import orchestrator

    try:
        body = await request.json()
        prompt_id = body.get("prompt_id")
        answer = body.get("answer")
    except Exception as e:
        logger.error(f"❌ Error parsing JSON: {e}")
        raise HTTPException(status_code=400, detail=f"Error parsing request: {e}")

    logger.info(f"📨 /interaction-response recibido: prompt_id={prompt_id}, answer={answer}")

    if not prompt_id or not answer:
        raise HTTPException(status_code=400, detail="prompt_id y answer son requeridos")

    # Limpiar de eventos pendientes
    pending_interaction_events[:] = [
        e for e in pending_interaction_events
        if e.get("payload", {}).get("prompt_id") != prompt_id
    ]
    logger.info(f"✅ Evento de interacción {prompt_id} respondido y eliminado de pendientes")

    # Detectar si es un wizard de Sentinel
    if prompt_id.startswith("sentinel-wizard-"):
        try:
            # Extraer wizard_id y step del prompt_id
            # Format: sentinel-wizard-{uuid}-{step}
            # Ejemplo: sentinel-wizard-a1b2c3d4-framework
            parts = prompt_id.split("-")
            if len(parts) >= 4:
                wizard_id = f"{parts[0]}-{parts[1]}-{parts[2]}"
                step = parts[3]
            else:
                wizard_id = prompt_id
                step = "unknown"

            logger.info(f"🛡️ Routing respuesta de Sentinel wizard: wizard_id={wizard_id}, step={step}, answer={answer}")

            # Mapear step a formato esperado por el orchestrator
            step_mapping = {
                "framework": "framework_detection",
                "ai": "ai_provider",
                "ai-provider-select": "ai_provider",
                "provider": "ai_provider",
                "testing": "testing_config"
            }
            mapped_step = step_mapping.get(step, step)
            logger.info(f"🛡️ Step mapeado: {step} -> {mapped_step}")

            # Manejar respuesta del wizard
            result = await orchestrator.handle_sentinel_wizard_response(wizard_id, mapped_step, answer)
            logger.info(f"🛡️ Resultado de handle_sentinel_wizard_response: {result}")
            return ApiResponse(ok=True, message="Respuesta de wizard procesada", data=result)
        except Exception as e:
            logger.exception(f"❌ Error en wizard response: {e}")
            raise HTTPException(status_code=500, detail=f"Error procesando wizard: {str(e)}")

    # Fallback: enviar a Sentinel como comando de respuesta normal
    await send_command(
        "sentinel",
        OrchestratorCommand(action="answer", options={"prompt_id": prompt_id, "answer": answer})
    )
    return ApiResponse(ok=True, message="Respuesta de interacción enviada")


@router.get("/logs", response_model=ApiResponse, summary="Obtener logs recientes de Cerebro")
async def get_logs(lines: int = 50):
    """Devuelve las últimas N líneas del log de Cerebro para debugging"""
    import subprocess
    try:
        # Intentar leer del archivo de log si existe
        log_paths = [
            "/tmp/cerebro.log",
            "/var/log/cerebro.log",
            str(Path.home() / ".cerebro" / "cerebro.log"),
        ]

        for log_path in log_paths:
            if os.path.exists(log_path):
                result = subprocess.run(
                    ["tail", "-n", str(lines), log_path],
                    capture_output=True,
                    text=True
                )
                if result.returncode == 0:
                    return ApiResponse(ok=True, data={"logs": result.stdout.split("\n")})

        # Si no hay archivo, intentar con journalctl o pm2 logs
        result = subprocess.run(
            ["journalctl", "-u", "cerebro", "-n", str(lines), "--no-pager"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            return ApiResponse(ok=True, data={"logs": result.stdout.split("\n")})

        return ApiResponse(ok=False, message="No se pudo acceder a los logs", data={"logs": []})
    except Exception as e:
        return ApiResponse(ok=False, message=f"Error leyendo logs: {str(e)}", data={"logs": []})
