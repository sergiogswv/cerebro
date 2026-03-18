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
        logger.info(f"🔔 EVENTO [{event.source}] {event.type}: {event.payload}")
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
    from app.sockets import pending_interaction_events

    body = await request.json()
    prompt_id = body.get("prompt_id")
    answer = body.get("answer")

    if not prompt_id or not answer:
        raise HTTPException(status_code=400, detail="prompt_id y answer son requeridos")

    # Limpiar el evento de interacción de la cola de pendientes
    pending_interaction_events[:] = [e for e in pending_interaction_events if e.get('payload', {}).get('prompt_id') != prompt_id]
    logger.info(f"✅ Evento de interacción {prompt_id} respondido y eliminado de pendientes")

    # Reenviar al agente (por ahora asumimos sentinel como origen principal de interacciones)
    await send_command(
        "sentinel",
        OrchestratorCommand(
            action="answer",
            options={"prompt_id": prompt_id, "answer": answer}
        )
    )

    return ApiResponse(ok=True, message="Respuesta de interacción enviada")


@router.get("/architect/config", response_model=ApiResponse, summary="Obtener config de Architect del proyecto activo")
async def get_architect_config():
    result = await orchestrator.get_architect_config()
    if isinstance(result, dict) and "error" in result:
        return ApiResponse(ok=False, message=result["error"])
    return ApiResponse(ok=True, data=result)


@router.post("/architect/config", response_model=ApiResponse, summary="Guardar config de Architect del proyecto activo")
async def save_architect_config(request: Request):
    config = await request.json()
    result = await orchestrator.save_architect_config(config)
    if result.get("status") == "error":
        return ApiResponse(ok=False, message=result.get("message"))
    return ApiResponse(ok=True, message="Configuración guardada exitosamente")


@router.get("/architect/patterns", response_model=ApiResponse)
async def get_architect_patterns():
    patterns = await orchestrator.get_architect_patterns()
    return ApiResponse(ok=True, data=patterns)


@router.get("/architect/ai-config", response_model=ApiResponse)
async def get_ai_config():
    config = await orchestrator.get_ai_config()
    return ApiResponse(ok=True, data=config)


@router.post("/architect/ai-config", response_model=ApiResponse)
async def save_ai_config(request: Request):
    config = await request.json()
    result = await orchestrator.save_ai_config(config)
    if result.get("status") == "error":
        return ApiResponse(ok=False, message=result.get("message"))
    return ApiResponse(ok=True, message="Configuración de IA guardada")


@router.post("/architect/validate-ai", response_model=ApiResponse)
async def validate_ai(request: Request):
    data = await request.json()
    url = data.get("url")
    key = data.get("key")
    provider = data.get("provider")
    
    result = await orchestrator.validate_ai_provider(url, key, provider)
    if result.get("ok"):
        return ApiResponse(ok=True, data=result.get("models"))
    return ApiResponse(ok=False, message=result.get("error"))


@router.post("/architect/init", response_model=ApiResponse, summary="Lanzar Wizard de Architect para el proyecto activo")
async def architect_init(request: Request):
    data = await request.json()
    pattern = data.get("pattern")
    result = await orchestrator.architect_init(pattern=pattern)
    if isinstance(result, dict) and "error" in result:
        return ApiResponse(ok=False, message=result["error"])
    return ApiResponse(ok=True, message="Proceso de inicialización lanzado")


@router.get("/sentinel/config", response_model=ApiResponse, summary="Obtener config de Sentinel del proyecto activo")
async def get_sentinel_config():
    result = await orchestrator.get_sentinel_config()
    if isinstance(result, dict) and "error" in result:
        return ApiResponse(ok=False, message=result["error"])
    return ApiResponse(ok=True, data=result)


@router.post("/sentinel/config", response_model=ApiResponse, summary="Guardar config de Sentinel del proyecto activo")
async def save_sentinel_config(request: Request):
    config = await request.json()
    result = await orchestrator.save_sentinel_config(config)
    if result.get("status") == "error":
        return ApiResponse(ok=False, message=result.get("message"))
    return ApiResponse(ok=True, message="Configuración de Sentinel guardada exitosamente")


@router.post("/sentinel/init", response_model=ApiResponse, summary="Lanzar Wizard de Sentinel para el proyecto activo")
async def sentinel_init():
    result = await orchestrator.sentinel_init()
    if isinstance(result, dict) and "error" in result:
        return ApiResponse(ok=False, message=result["error"])
    return ApiResponse(ok=True, message="Proceso de inicialización de Sentinel lanzado")
