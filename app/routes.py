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


@router.post("/architect/command", response_model=ApiResponse, summary="Enviar comando a Architect")
async def architect_command(request: Request):
    """Envía un comando a Architect para el proyecto activo"""
    from app.sockets import emit_agent_event
    from app.dispatcher import send_raw_command
    import uuid
    import os

    data = await request.json()
    action = data.get("action", "lint")
    target = data.get("target", orchestrator.active_project)

    # Convertir nombre del proyecto a ruta completa
    project_path = "."
    if target and target != "Ninguno":
        project_path = os.path.join(orchestrator.workspace_root, target).replace("\\", "/")

    # Enviar comando a Architect vía dispatcher
    command = {
        "action": action,
        "target": project_path,
        "request_id": f"architect-{str(uuid.uuid4())[:8]}"
    }

    ack = await send_raw_command("architect", command)
    if isinstance(ack, dict) and ack.get("status") == "rejected":
        return ApiResponse(ok=False, message=ack.get("error", "Comando rechazado"))

    # Mensajes descriptivos para cada acción
    action_messages = {
        "lint": "🔍 Análisis de linting en progreso...",
        "deep-analysis": "🧠 Análisis profundo de arquitectura iniciado...",
        "check-circular": "🔄 Buscando dependencias circulares...",
        "full-report": "📊 Generando reporte completo de arquitectura...",
        "validate-config": "✅ Validando configuración de architect.json...",
        "analyze-stale": "🕰️ Buscando archivos stale con alta complejidad..."
    }

    # Determinar estado del resultado
    result_status = "completed"
    if isinstance(ack, dict):
        ack_status = ack.get("status", "")
        if ack_status == "error":
            result_status = "error"
        elif ack_status in ["accepted", "completed"]:
            result_status = "completed"

    # Emitir evento para que el Dashboard lo muestre
    # Convertir guiones a guiones bajos para coincidir con eventos de Architect (ej: deep-analysis -> deep_analysis)
    event_type = f"command_{action.replace('-', '_')}"
    await emit_agent_event({
        "source": "architect",
        "type": event_type,
        "severity": "info",
        "payload": {
            "action": action,
            "target": target,
            "status": result_status,
            "message": action_messages.get(action, f"🚀 Ejecutando acción: {action}"),
            "result": ack.get("result") if isinstance(ack, dict) else None
        }
    })

    return ApiResponse(ok=True, message=f"Comando '{action}' enviado a Architect", data=ack)


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


@router.post("/sentinel/command", response_model=ApiResponse, summary="Enviar comando Pro a Sentinel")
async def sentinel_command(request: Request):
    """Envía un comando Pro a Sentinel para el proyecto activo"""
    from app.sockets import emit_agent_event
    import uuid
    import os

    data = await request.json()
    action = data.get("action", "pro")
    subcommand = data.get("subcommand", "check")
    target = data.get("target", orchestrator.active_project)

    # Convertir nombre del proyecto a ruta completa
    project_path = "."
    if target and target != "Ninguno":
        project_path = os.path.join(orchestrator.workspace_root, target).replace("\\", "/")

    # Enviar comando a Sentinel vía dispatcher
    command = {
        "action": action,
        "subcommand": subcommand,
        "target": project_path,
        "request_id": f"sentinel-{str(uuid.uuid4())[:8]}"
    }

    from app.dispatcher import send_raw_command
    ack = await send_raw_command("sentinel", command)
    if isinstance(ack, dict) and ack.get("status") == "rejected":
        return ApiResponse(ok=False, message=ack.get("error", "Comando rechazado"))

    # Mensajes descriptivos para cada acción Pro
    action_messages = {
        "check": "🔍 Quick Check: Análisis estático rápido en progreso...",
        "audit": "🛡️ Audit: Auditoría con correcciones IA en progreso...",
        "report": "📊 Report: Generando reporte de calidad...",
        "fix": "⚡ Auto Fix: Corrigiendo bugs automáticamente...",
        "review": "🔄 Review: Realizando review de arquitectura...",
        "clean-cache": "🗑️ Clean Cache: Limpiando caché de IA..."
    }

    # Determinar estado del resultado
    result_status = "completed"
    if isinstance(ack, dict):
        ack_status = ack.get("status", "")
        if ack_status == "error":
            result_status = "error"
        elif ack_status in ["accepted", "completed"]:
            result_status = "completed"

    # Emitir evento para que el Dashboard lo muestre
    # Tipo: pro_check, pro_audit, pro_report, etc.
    event_type = f"pro_{subcommand.replace('-', '_')}"
    await emit_agent_event({
        "source": "sentinel",
        "type": event_type,
        "severity": "info",
        "payload": {
            "action": subcommand,
            "target": target,
            "status": result_status,
            "message": action_messages.get(subcommand, f"🚀 Ejecutando acción Pro: {subcommand}"),
            "result": ack.get("result") if isinstance(ack, dict) else None
        }
    })

    return ApiResponse(ok=True, message=f"Comando '{subcommand}' enviado a Sentinel", data=ack)


# ─── Sentinel Monitor Commands ────────────────────────────────────────────────

@router.post("/sentinel/monitor/pause", response_model=ApiResponse, summary="Pausar/Reanudar monitoreo de Sentinel")
async def sentinel_monitor_pause(request: Request):
    """Pausa o reanuda el monitoreo de cambios en tiempo real de Sentinel"""
    from app.sockets import emit_agent_event
    import uuid

    data = await request.json() if await request.body() else {}
    target = data.get("target", orchestrator.active_project)

    # Enviar comando a Sentinel vía dispatcher usando el formato correcto
    command = {
        "action": "monitor/pause",
        "target": target,
        "request_id": f"monitor-{str(uuid.uuid4())[:8]}"
    }

    from app.dispatcher import send_raw_command
    ack = await send_raw_command("sentinel", command)

    # Emitir evento con el resultado
    await emit_agent_event({
        "source": "sentinel",
        "type": "monitor_pause",
        "severity": "info",
        "payload": {
            "message": "Estado del monitoreo actualizado",
            "paused": ack.get("result", {}).get("paused") if isinstance(ack, dict) else None,
            "result": ack
        }
    })

    return ApiResponse(ok=True, message="Comando pause enviado a Sentinel", data=ack)


@router.post("/sentinel/monitor/daily-report", response_model=ApiResponse, summary="Generar reporte diario de productividad")
async def sentinel_monitor_daily_report(request: Request):
    """Genera reporte diario de productividad basado en commits de Git"""
    from app.sockets import emit_agent_event
    import uuid

    data = await request.json() if await request.body() else {}
    target = data.get("target", orchestrator.active_project)

    command = {
        "action": "monitor/daily-report",
        "target": target,
        "request_id": f"daily-report-{str(uuid.uuid4())[:8]}"
    }

    from app.dispatcher import send_raw_command
    ack = await send_raw_command("sentinel", command)

    await emit_agent_event({
        "source": "sentinel",
        "type": "daily_report",
        "severity": "info",
        "payload": {
            "message": "Generando reporte diario de productividad...",
            "result": ack
        }
    })

    return ApiResponse(ok=True, message="Reporte diario solicitado", data=ack)


@router.get("/sentinel/monitor/metrics", response_model=ApiResponse, summary="Obtener métricas de Sentinel")
async def sentinel_monitor_metrics():
    """Obtiene dashboard de métricas (bugs, costos, tokens) de Sentinel"""
    from app.sockets import emit_agent_event

    # Métricas de ejemplo (en producción venir de SentinelStats)
    metrics = {
        "bugs_evitados": 0,
        "costo_acumulado": 0.0,
        "tokens_usados": 0,
        "tiempo_ahorrado_mins": 0
    }

    await emit_agent_event({
        "source": "sentinel",
        "type": "metrics",
        "severity": "info",
        "payload": {
            "message": "Métricas de Sentinel obtenidas",
            "metrics": metrics
        }
    })

    return ApiResponse(ok=True, message="Métricas obtenidas", data=metrics)


@router.post("/sentinel/monitor/testing", response_model=ApiResponse, summary="Obtener sugerencias de testing")
async def sentinel_monitor_testing(request: Request):
    """Obtiene sugerencias de testing complementarias para el proyecto activo"""
    from app.sockets import emit_agent_event
    import uuid
    import logging

    logger = logging.getLogger(__name__)

    try:
        data = await request.json() if await request.body() else {}
    except Exception as e:
        logger.warning(f"Error leyendo request body: {e}")
        data = {}

    target = data.get("target", orchestrator.active_project)

    logger.info(f"🎯 Target: {target}, Active project: {orchestrator.active_project}")

    command = {
        "action": "monitor/testing",
        "target": target,
        "request_id": f"testing-{str(uuid.uuid4())[:8]}"
    }

    logger.info(f"🔧 Enviando comando testing a Sentinel: {command}")

    from app.dispatcher import send_raw_command
    try:
        ack = await send_raw_command("sentinel", command)
    except Exception as e:
        logger.error(f"❌ Error ejecutando comando: {e}")
        ack = {"status": "error", "error": str(e)}

    logger.info(f"📥 Respuesta de Sentinel: {ack}")

    # Solo emitir evento si hay resultado exitoso
    if isinstance(ack, dict) and ack.get("status") in ["completed", "accepted"]:
        event_payload = {
            "source": "sentinel",
            "type": "testing_suggestions",
            "severity": "info",
            "payload": {
                "message": ack.get("result", {}).get("message", "Sugerencias de testing generadas"),
                "result": ack
            }
        }

        logger.info(f"📤 Emitiendo evento: {event_payload}")
        try:
            await emit_agent_event(event_payload)
            logger.info("✅ Evento emitido exitosamente")
        except Exception as e:
            logger.error(f"❌ Error emitiendo evento: {e}")
    else:
        logger.error(f"❌ Error en comando testing: {ack}")

    return ApiResponse(ok=True, message="Sugerencias de testing solicitadas", data=ack)


@router.post("/sentinel/monitor/reset-config", response_model=ApiResponse, summary="Reiniciar configuración de Sentinel")
async def sentinel_monitor_reset_config(request: Request):
    """Reinicia la configuración de Sentinel del proyecto activo"""
    from app.sockets import emit_agent_event
    import uuid

    data = await request.json() if await request.body() else {}
    target = data.get("target", orchestrator.active_project)

    command = {
        "action": "monitor/reset-config",
        "target": target,
        "request_id": f"reset-config-{str(uuid.uuid4())[:8]}"
    }

    from app.dispatcher import send_raw_command
    ack = await send_raw_command("sentinel", command)

    await emit_agent_event({
        "source": "sentinel",
        "type": "reset_config",
        "severity": "warning",
        "payload": {
            "message": "Reiniciando configuración de Sentinel...",
            "result": ack
        }
    })

    return ApiResponse(ok=True, message="Reinicio de configuración solicitado", data=ack)


# ─── Warden Endpoints ─────────────────────────────────────────────────────────

@router.post("/warden/scan", response_model=ApiResponse, summary="Ejecutar escaneo de Warden")
async def warden_scan():
    """Ejecuta un escaneo completo de Warden sobre el proyecto activo"""
    from app.sockets import emit_agent_event

    result = await orchestrator.warden_scan()
    if isinstance(result, dict) and result.get("error"):
        return ApiResponse(ok=False, message=result["error"])

    # Emitir evento para que el Dashboard lo muestre
    await emit_agent_event({
        "source": "warden",
        "type": "scan_completed",
        "severity": "info",
        "payload": {
            "message": "Warden scan completado",
            "result": result
        }
    })

    return ApiResponse(ok=True, message="Escaneo de Warden ejecutado", data=result)


@router.post("/warden/predict-critical", response_model=ApiResponse, summary="Predecir archivos críticos")
async def warden_predict_critical():
    """Predice archivos que se volverán críticos"""
    from app.sockets import emit_agent_event

    result = await orchestrator.warden_predict_critical()
    if isinstance(result, dict) and result.get("error"):
        return ApiResponse(ok=False, message=result["error"])

    # Emitir evento para que el Dashboard lo muestre
    await emit_agent_event({
        "source": "warden",
        "type": "predict_critical_completed",
        "severity": "info",
        "payload": {
            "message": "Predicción de críticos completada",
            "result": result
        }
    })

    return ApiResponse(ok=True, message="Predicción de críticos ejecutada", data=result)


@router.post("/warden/risk-assess", response_model=ApiResponse, summary="Evaluar riesgos del proyecto")
async def warden_risk_assess():
    """Evalúa riesgos de archivos del proyecto activo"""
    from app.sockets import emit_agent_event

    result = await orchestrator.warden_risk_assess()
    if isinstance(result, dict) and result.get("error"):
        return ApiResponse(ok=False, message=result["error"])

    # Emitir evento para que el Dashboard lo muestre
    await emit_agent_event({
        "source": "warden",
        "type": "risk_assess_completed",
        "severity": "info",
        "payload": {
            "message": "Evaluación de riesgos completada",
            "result": result
        }
    })

    return ApiResponse(ok=True, message="Evaluación de riesgos completada", data=result)


@router.post("/warden/churn-report", response_model=ApiResponse, summary="Generar reporte de churn")
async def warden_churn_report():
    """Genera reporte de churn del proyecto activo"""
    from app.sockets import emit_agent_event

    result = await orchestrator.warden_churn_report()
    if isinstance(result, dict) and result.get("error"):
        return ApiResponse(ok=False, message=result["error"])

    # Emitir evento para que el Dashboard lo muestre
    await emit_agent_event({
        "source": "warden",
        "type": "churn_report_completed",
        "severity": "info",
        "payload": {
            "message": "Reporte de churn generado",
            "result": result
        }
    })

    return ApiResponse(ok=True, message="Reporte de churn generado", data=result)
