import logging
from fastapi import APIRouter, HTTPException, Request
from app.models import AgentEvent, ApiResponse, OrchestratorCommand
from app.orchestrator import orchestrator
from app.dispatcher import send_command, send_raw_command

router = APIRouter(prefix="/api", tags=["events"])
logger = logging.getLogger("cerebro.routes")


@router.post("/events", response_model=ApiResponse, summary="Recibir evento de un agente")
async def receive_event(event: AgentEvent):
    """
    Endpoint principal. Los agentes (Sentinel, Architect, Warden, Ejecutor)
    envían sus eventos aquí. El Orquestador evalúa y actúa.
    """
    try:
        result = await orchestrator.handle_event(event)
        return ApiResponse(ok=True, message="evento procesado", data=result)
    except Exception as e:
        logger.exception("Error procesando evento")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/command/{agent}", response_model=ApiResponse, summary="Enviar comando a un agente")
async def dispatch_command(agent: str, request: Request):
    """
    Proxy transparente: reenvía el JSON tal cual al agente.
    Cada agente define su propio contrato (Ejecutor usa 'service', Warden usa 'target', etc.)
    """
    body = await request.json()
    ack = await send_raw_command(agent, body)
    return ApiResponse(ok=True, message=f"comando enviado a {agent}", data=ack)


@router.get("/health", summary="Health check del Orquestador")
async def health():
    return {"status": "ok", "service": "cerebro"}


@router.get("/status", response_model=ApiResponse, summary="Estado actual del Orquestador")
async def get_status():
    """
    Retorna el estado actual, incluido el proyecto activo.
    """
    return ApiResponse(ok=True, message="Estado obtenido", data={
        "active_project": orchestrator.active_project,
        "workspace_root": orchestrator.workspace_root
    })


@router.post("/bootstrap", response_model=ApiResponse, summary="Iniciar proceso de seleccion de proyecto")
async def bootstrap():
    """
    Escanea proyectos y dispara los botones en Telegram.
    """
    result = await orchestrator.bootstrap()
    return ApiResponse(ok=True, message="Bootstrap iniciado", data=result)


@router.get("/projects", response_model=ApiResponse, summary="Listar proyectos disponibles")
async def get_projects():
    import os
    projects = [d for d in os.listdir(orchestrator.workspace_root) 
                if os.path.isdir(os.path.join(orchestrator.workspace_root, d)) 
                and not d.startswith(".")]
    return ApiResponse(ok=True, message="Proyectos obtenidos", data={"projects": sorted(projects)})

@router.post("/select-project", response_model=ApiResponse, summary="Establecer proyecto activo")
async def select_project(request: Request):
    """
    Recibe la seleccion desde el Notificador (Telegram) o el Dashboard.
    """
    body = await request.json()
    project_name = body.get("project")
    if not project_name:
        raise HTTPException(status_code=400, detail="project_name requerido")

    result = await orchestrator.set_active_project(project_name)
    return ApiResponse(ok=True, message="Proyecto seleccionado", data=result)


@router.post("/interaction-response", response_model=ApiResponse, summary="Responder a una interacción de usuario")
async def interaction_response(request: Request):
    """
    Recibe la respuesta de una interacción de usuario (e.g., desde Telegram)
    y la reenvía al agente Sentinel.
    """
    body = await request.json()
    prompt_id = body.get("prompt_id")
    answer = body.get("answer")

    if not prompt_id or not answer:
        raise HTTPException(status_code=400, detail="prompt_id y answer son requeridos")

    # Reenviar al agente (por ahora asumimos sentinel como origen principal de interacciones)
    await send_command(
        "sentinel",
        OrchestratorCommand(
            action="answer",
            options={"prompt_id": prompt_id, "answer": answer}
        )
    )

    return ApiResponse(ok=True, message="Respuesta de interacción enviada")
