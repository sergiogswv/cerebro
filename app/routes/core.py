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
  GET  /browse-directory
  POST /set-workspace-root
  GET  /current-workspace
"""

import logging
import os
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request, BackgroundTasks
from app.models import AgentEvent, ApiResponse, OrchestratorCommand
from app.orchestrator import orchestrator
from app.dispatcher import send_command, send_raw_command
from app.config import get_settings

router = APIRouter(tags=["core"])
logger = logging.getLogger("cerebro.routes.core")


@router.get("/health", summary="Health check del Orquestador")
async def health():
    return {"status": "ok", "service": "cerebro"}


@router.get("/status", response_model=ApiResponse, summary="Estado actual del Orquestador")
async def get_status():
    try:
        return ApiResponse(ok=True, message="Estado obtenido", data={
            "active_project": orchestrator.active_project,
            "workspace_root": orchestrator.workspace_root,
        })
    except Exception as e:
        logger.exception("Error getting status")
        return ApiResponse(ok=False, message=f"Error: {str(e)}", data={})


@router.get("/status/full", response_model=ApiResponse, summary="Estado completo y de salud del sistema")
async def get_full_status():
    import asyncio
    import httpx
    
    agents = ["sentinel_core", "sentinel_adk", "architect", "warden", "ejecutor"]
    health = {}
    from app.dispatcher import AGENT_URLS
    
    async def check_agent_health(agent_name):
        url = AGENT_URLS.get(agent_name)
        if not url:
            return agent_name, {"status": "error", "error": "Not registered"}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(f"{url}/health")
                res.raise_for_status()
                return agent_name, {"status": "ok", **res.json()}
        except Exception as e:
            return agent_name, {"status": "error", "error": str(e)}

    results = await asyncio.gather(*(check_agent_health(a) for a in agents), return_exceptions=True)
    for r in results:
        if isinstance(r, tuple):
            health[r[0]] = r[1]
        else:
            logger.error(f"Error checking health: {r}")

    # Gather learning stats safely
    learning_stats = {}
    if hasattr(orchestrator, 'context_db') and orchestrator.context_db:
        try:
            learning_stats = orchestrator.context_db.get_feedback_stats()
        except: pass

    data = {
        "cerebro": "ok",
        "agents": health,
        "active_project": getattr(orchestrator, 'active_project', None),
        "active_locks": len(getattr(orchestrator._events, '_active_processing', {})),
        "initializing_projects": list(getattr(orchestrator, '_initializing_projects', [])),
        "learning_stats": learning_stats,
    }

    return ApiResponse(ok=True, message="Health check completo", data=data)



@router.post("/events", response_model=ApiResponse, summary="Recibir evento de un agente")
async def receive_event(event: AgentEvent, background_tasks: BackgroundTasks):
    """
    Endpoint principal. Los agentes envían sus eventos aquí.
    Ahora usa BackgroundTasks para evitar bloquear al emisor con procesos pesados
    como merges de Git o cadenas de análisis de agentes.
    """
    try:
        logger.info(f"🔔 EVENTO [{event.source}] {event.type}: {event.payload}")
        
        # Procesar de forma asíncrona para liberar al emisor inmediatamente
        async def process_task(ev: AgentEvent):
            try:
                await orchestrator.handle_event(ev)
                logger.debug(f"✅ Evento {ev.id} procesado internamente")
            except Exception as e:
                logger.error(f"❌ Error diferido procesando evento {ev.id}: {e}")

        background_tasks.add_task(process_task, event)

        return ApiResponse(
            ok=True, 
            message="evento recibido y encolado para procesamiento", 
            data={"event_id": event.id, "status": "enqueued"}
        )
    except Exception as e:
        logger.exception("Error recibiendo evento")
        return ApiResponse(ok=False, message=f"Error recibiendo evento: {str(e)}")


@router.post("/bootstrap", response_model=ApiResponse, summary="Iniciar proceso de selección de proyecto")
async def bootstrap():
    result = await orchestrator.bootstrap()
    return ApiResponse(ok=True, message="Bootstrap iniciado", data=result)


@router.get("/projects", response_model=ApiResponse, summary="Listar proyectos disponibles")
async def get_projects():
    try:
        import os
        workspace = orchestrator.workspace_root
        logger.info(f"[Projects] Scanning workspace: {workspace}")

        if not os.path.exists(workspace):
            logger.warning(f"[Projects] Workspace does not exist: {workspace}")
            return ApiResponse(ok=True, message="Workspace not found", data={"projects": []})

        projects = [
            d for d in os.listdir(workspace)
            if os.path.isdir(os.path.join(workspace, d)) and not d.startswith(".")
        ]
        logger.info(f"[Projects] Found {len(projects)} projects")
        return ApiResponse(ok=True, message="Proyectos obtenidos", data={"projects": sorted(projects)})
    except Exception as e:
        logger.exception(f"[Projects] Error scanning projects: {e}")
        return ApiResponse(ok=False, message=f"Error: {str(e)}", data={"projects": []})


@router.post("/select-project", response_model=ApiResponse, summary="Establecer proyecto activo")
async def select_project(request: Request):
    try:
        body = await request.json()
        project_name = body.get("project")
        restart_agents = body.get("restart_agents", True)  # True por defecto para compatibilidad

        if not project_name:
            raise HTTPException(status_code=400, detail="project_name requerido")

        logger.info(f"[SelectProject] project={project_name} | restart_agents={restart_agents}")

        if not restart_agents:
            # Solo actualizar el nombre del proyecto activo sin iniciar/reiniciar agentes
            orchestrator._projects._active_project = project_name
            logger.info(f"[SelectProject] Sync silencioso: proyecto activo = '{project_name}' (sin reiniciar agentes)")
            return ApiResponse(ok=True, message="Proyecto sincronizado (sin reiniciar agentes)", data={
                "project": project_name,
                "active_project": project_name,
                "restart_agents": False
            })

        result = await orchestrator.set_active_project(project_name)
        logger.info(f"[SelectProject] Result: {result}")
        return ApiResponse(ok=True, message="Proyecto seleccionado", data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[SelectProject] Error: {e}")
        return ApiResponse(ok=False, message=f"Error: {str(e)}", data={})


@router.post("/projects/new", response_model=ApiResponse, summary="Crear un nuevo proyecto")
async def create_project(request: Request):
    try:
        body = await request.json()
        name = body.get("name")
        project_type = body.get("project_type", "generic")
        description = body.get("description", "")
        base_path = body.get("base_path")
        
        if not name:
            raise HTTPException(status_code=400, detail="name es requerido")

        result = await orchestrator.create_project(name, project_type, description, base_path)
        if result.get("status") == "error":
            return ApiResponse(ok=False, message=result.get("message"))

        return ApiResponse(ok=True, message="Proyecto creado", data=result)
    except Exception as e:
        logger.exception("Error creando proyecto")
        return ApiResponse(ok=False, message=f"Error: {str(e)}")


@router.post("/command/{agent}", response_model=ApiResponse, summary="Enviar comando a un agente")
async def dispatch_command(agent: str, request: Request):
    """Proxy transparente con inyección de contexto para el Ejecutor."""
    body = await request.json()
    
    # ── 💉 INYECCIÓN DE CONTEXTO PARA EJECUTOR ──
    # Si manda al ejecutor un autofix/feature/bugfix y no hay ruta, inyectar el active_project
    if agent == "ejecutor":
        project_name = orchestrator.active_project
        if project_name:
            project_path = orchestrator._projects.get_project_path(project_name)
            # Solo inyectar si el comando parece de ejecución/escritura y viene vacío de target
            if "target" not in body or not body["target"]:
                body["project_path"] = project_path
                logger.debug(f"💉 Inyectada ruta de proyecto activo: {project_path}")
        else:
            # Si no hay proyecto activo, no permitir comandos de escritura al ejecutor
            action = body.get("action", "")
            if action in ("autofix", "feature", "bugfix", "run"):
                logger.warning("⛔ Comando al ejecutor bloqueado: No hay proyecto activo.")
                return ApiResponse(ok=False, message="Debes seleccionar un proyecto activo antes de enviar tareas de ejecución.")

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


# ═══════════════════════════════════════════════════════════════════════════════
# NAVEGACIÓN DE DIRECTORIOS
# ═══════════════���═══════════════════════════════════════════════════════════════

@router.get("/browse-directory", response_model=ApiResponse, summary="Navegar directorios del sistema")
async def browse_directory(path: str = None):
    """
    Lista directorios disponibles en una ruta dada.
    Si no se especifica path, usa el workspace_root actual.
    """
    try:
        settings = get_settings()
        base_path = path or settings.workspace_root

        # Normalizar la ruta
        base_path = os.path.expanduser(base_path)
        base_path = os.path.abspath(base_path)

        # Verificar que el directorio existe
        if not os.path.exists(base_path):
            return ApiResponse(ok=False, message=f"Ruta no existe: {base_path}", data=None)

        if not os.path.isdir(base_path):
            return ApiResponse(ok=False, message=f"No es un directorio: {base_path}", data=None)

        # Listar directorios
        items = []
        for item in sorted(os.listdir(base_path)):
            item_path = os.path.join(base_path, item)
            # Solo incluir directorios que no empiecen con .
            if os.path.isdir(item_path) and not item.startswith("."):
                # Detectar si parece un proyecto (tiene archivos típicos)
                is_project = _is_likely_project(item_path)
                items.append({
                    "name": item,
                    "path": item_path.replace("\\", "/"),
                    "is_project": is_project
                })

        # Obtener directorio padre
        parent = os.path.dirname(base_path)
        # En Windows, no subir más allé de las unidades
        if os.name == 'nt' and len(parent) <= 3 and parent.endswith(':\\'):
            parent = None
        elif base_path == parent or base_path == "/":
            parent = None

        return ApiResponse(ok=True, message="Directorios listados", data={
            "current_path": base_path.replace("\\", "/"),
            "parent_path": parent.replace("\\", "/") if parent else None,
            "directories": items,
            "is_workspace_root": base_path == settings.workspace_root
        })

    except PermissionError:
        return ApiResponse(ok=False, message="Sin permisos para acceder a esta ruta", data=None)
    except Exception as e:
        logger.exception("Error navegando directorios")
        return ApiResponse(ok=False, message=f"Error: {str(e)}", data=None)


@router.get("/project-tree", response_model=ApiResponse, summary="Explorar árbol del proyecto activo")
async def get_project_tree(rel_path: str = ""):
    """
    Devuelve los archivos y carpetas del proyecto activo dada una sub-ruta (rel_path).
    Sirve para mostrar un file browser del proyecto activo en el UI.
    """
    try:
        settings = get_settings()
        active_project = orchestrator.active_project
        workspace = settings.workspace_root

        if not active_project or not workspace:
            return ApiResponse(ok=False, message="No hay proyecto activo", data=None)

        base_path = os.path.join(workspace, active_project)
        target_path = os.path.join(base_path, rel_path.lstrip("/\\"))

        # Seguridad: evitar path traversal fuera del proyecto
        if not os.path.abspath(target_path).startswith(os.path.abspath(base_path)):
            return ApiResponse(ok=False, message="Ruta inválida (path traversal)", data=None)

        if not os.path.exists(target_path):
            return ApiResponse(ok=False, message=f"La ruta no existe: {rel_path}", data=None)

        if not os.path.isdir(target_path):
            return ApiResponse(ok=False, message="No es un directorio", data=None)

        items = []
        for item in sorted(os.listdir(target_path)):
            if item.startswith(".git") or item == "node_modules":
                continue  # ignorar directorios pesados/internos

            full_item_path = os.path.join(target_path, item)
            is_dir = os.path.isdir(full_item_path)
            items.append({
                "name": item,
                "is_dir": is_dir,
                "rel_path": os.path.relpath(full_item_path, base_path).replace("\\", "/")
            })

        # Ordenar: primero carpetas, luego archivos
        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))

        return ApiResponse(ok=True, message="Directorio leído", data={
            "current_rel_path": rel_path,
            "items": items
        })

    except Exception as e:
        logger.exception(f"Error en /project-tree: {e}")
        return ApiResponse(ok=False, message=f"Error: {str(e)}", data=None)




def _is_likely_project(path: str) -> bool:
    """Detecta si un directorio parece ser un proyecto basado en archivos típicos."""
    project_indicators = [
        # Git
        ".git",
        # JavaScript/Node
        "package.json", "node_modules",
        # Python
        "requirements.txt", "pyproject.toml", "setup.py", "Pipfile",
        # Rust
        "Cargo.toml",
        # Go
        "go.mod",
        # Java/Maven/Gradle
        "pom.xml", "build.gradle",
        # .NET
        "*.csproj", "*.sln",
        # Ruby
        "Gemfile",
        # PHP
        "composer.json",
        # Docker
        "Dockerfile", "docker-compose.yml",
        # Configuración general
        ".vscode", ".idea", ".github"
    ]

    try:
        for item in os.listdir(path):
            # Verificar coincidencia exacta o patrón
            if item in project_indicators:
                return True
            # Verificar archivos con extensión
            for indicator in project_indicators:
                if indicator.startswith("*") and item.endswith(indicator[1:]):
                    return True
        return False
    except:
        return False


@router.get("/current-workspace", response_model=ApiResponse, summary="Obtener workspace actual")
async def get_current_workspace():
    """Devuelve el workspace_root actualmente configurado."""
    settings = get_settings()
    return ApiResponse(ok=True, message="Workspace actual", data={
        "workspace_root": settings.workspace_root.replace("\\", "/"),
        "workspace_name": os.path.basename(settings.workspace_root)
    })


@router.post("/set-workspace-root", response_model=ApiResponse, summary="Cambiar workspace root")
async def set_workspace_root(request: Request):
    """
    Cambia el workspace_root dinámicamente.
    Esto actualiza la configuración en tiempo de ejecución.
    """
    try:
        body = await request.json()
        new_workspace = body.get("workspace_root")
        project_name = body.get("project_name")  # Opcional: nombre del proyecto a seleccionar

        if not new_workspace:
            raise HTTPException(status_code=400, detail="workspace_root es requerido")

        # Normalizar la ruta
        new_workspace = os.path.expanduser(new_workspace)
        new_workspace = os.path.abspath(new_workspace)

        # Verificar que existe
        if not os.path.exists(new_workspace):
            return ApiResponse(ok=False, message=f"La ruta no existe: {new_workspace}", data=None)

        if not os.path.isdir(new_workspace):
            return ApiResponse(ok=False, message=f"No es un directorio: {new_workspace}", data=None)

        # Actualizar configuración
        settings = get_settings()
        # Nota: En Pydantic v2, necesitamos modificar el objeto directamente
        object.__setattr__(settings, 'workspace_root', new_workspace)

        # Actualizar el project_manager del orchestrator
        orchestrator._projects.workspace_root = new_workspace

        logger.info(f"Workspace root cambiado a: {new_workspace}")

        # Si se proporcionó un nombre de proyecto, seleccionarlo
        if project_name:
            result = await orchestrator.set_active_project(project_name)
            return ApiResponse(ok=True, message="Workspace y proyecto actualizados", data={
                "workspace_root": new_workspace.replace("\\", "/"),
                "project": result
            })

        return ApiResponse(ok=True, message="Workspace root actualizado", data={
            "workspace_root": new_workspace.replace("\\", "/")
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error cambiando workspace root")
        return ApiResponse(ok=False, message=f"Error: {str(e)}", data=None)


@router.post("/select-custom-project", response_model=ApiResponse, summary="Seleccionar proyecto desde ruta arbitraria")
async def select_custom_project(request: Request):
    """
    Selecciona un proyecto desde cualquier ruta del sistema de archivos.
    El proyecto se registra con el nombre del directorio.
    El inicio de agentes se hace en segundo plano para responder rápido.
    """
    import asyncio
    from app.sockets import emit_agent_event

    try:
        body = await request.json()
        project_path = body.get("project_path")

        if not project_path:
            raise HTTPException(status_code=400, detail="project_path es requerido")

        # Normalizar la ruta
        project_path = os.path.expanduser(project_path)
        project_path = os.path.abspath(project_path)

        # Verificar que existe
        if not os.path.exists(project_path):
            return ApiResponse(ok=False, message=f"La ruta no existe: {project_path}", data=None)

        if not os.path.isdir(project_path):
            return ApiResponse(ok=False, message=f"No es un directorio: {project_path}", data=None)

        # Obtener el nombre del proyecto (último componente de la ruta)
        project_name = os.path.basename(project_path)

        # Actualizar workspace_root al directorio padre
        parent_dir = os.path.dirname(project_path)
        settings = get_settings()
        object.__setattr__(settings, 'workspace_root', parent_dir)
        orchestrator._projects.workspace_root = parent_dir
        orchestrator._agents.workspace_root = parent_dir

        # Establecer el proyecto activo (solo el nombre, sin iniciar agentes aún)
        # Usamos set_active en lugar de set_active_project para evitar iniciar sentinel inmediatamente
        orchestrator._projects._active_project = project_name
        orchestrator._projects._monitored_project = None  # Reset monitoring

        logger.info(f"Proyecto personalizado seleccionado: {project_name} en {parent_dir}")

        # Iniciar agentes en segundo plano (no bloquea la respuesta)
        async def background_init():
            try:
                await asyncio.sleep(0.5)  # Pequeño delay para que la respuesta HTTP se envíe primero

                # Obtener configuración de auto-start con prioridad
                from app.config_manager import UnifiedConfigManager
                manager = UnifiedConfigManager.get_instance()
                unified_config = manager.get_config()
                cerebro_config = unified_config.cerebro if hasattr(unified_config, 'cerebro') else None
                auto_start_agents = cerebro_config.auto_start_agents if cerebro_config else ["sentinel"]

                logger.info(f"Iniciando agentes en orden de prioridad: {auto_start_agents}")

                # Iniciar cada agente en el orden especificado
                for agent_name in auto_start_agents:
                    try:
                        # Verificar el modo del agente desde cerebro config
                        agent_mode = cerebro_config.agent_modes.get(agent_name, "core") if cerebro_config else "core"
                        core_service_name = agent_name  # e.g., "architect"
                        adk_service_name = f"{agent_name}_adk"  # e.g., "architect_adk"
                        is_adk_mode = agent_mode == "adk"

                        logger.info(f"Iniciando {agent_name} en modo {agent_mode} (prioridad: {auto_start_agents.index(agent_name) + 1})")

                        if agent_name == "sentinel":
                            sentinel_rc_path = Path(project_path) / ".sentinelrc.toml"
                            if not sentinel_rc_path.exists():
                                logger.info(f"🛡️ .sentinelrc.toml not found para {project_name}. Generando config headless AI primero...")
                                try:
                                    import toml
                                    from app.orchestrator import orchestrator
                                    config = await orchestrator._agents._generate_sentinel_config(
                                        Path(project_path), False, "headless-custom"
                                    )
                                    with open(sentinel_rc_path, "w", encoding="utf-8") as f:
                                        toml.dump(config, f)
                                    logger.info("✅ Configuración headless de Sentinel generada.")
                                except Exception as e:
                                    logger.error(f"❌ Error generando config headless Sentinel: {e}")

                        if is_adk_mode:
                            # ADK mode: iniciar Core primero, luego ADK
                            # Step 1a: Iniciar Core Engine
                            try:
                                import httpx
                                async with httpx.AsyncClient(timeout=10.0) as client:
                                    start_resp = await client.post(
                                        f"{settings.executor_url}/command",
                                        json={
                                            "action": "open",
                                            "service": core_service_name,
                                            "request_id": f"cerebro-custom-{core_service_name}"
                                        }
                                    )
                                    if start_resp.status_code == 200:
                                        logger.info(f"✅ Ejecutor inició {core_service_name} (Core para ADK)")
                                    else:
                                        logger.warning(f"⚠️ Ejecutor returned {start_resp.status_code} for {core_service_name}")
                            except Exception as e:
                                logger.warning(f"⚠️ No se pudo iniciar {core_service_name} via Ejecutor: {e}")

                            await asyncio.sleep(3)  # Esperar a que Core inicie

                            # Step 1b: Iniciar ADK
                            try:
                                async with httpx.AsyncClient(timeout=10.0) as client:
                                    start_resp = await client.post(
                                        f"{settings.executor_url}/command",
                                        json={
                                            "action": "open",
                                            "service": adk_service_name,
                                            "request_id": f"cerebro-custom-{adk_service_name}"
                                        }
                                    )
                                    if start_resp.status_code == 200:
                                        logger.info(f"✅ Ejecutor inició {adk_service_name} (ADK)")
                                    else:
                                        logger.warning(f"⚠️ Ejecutor returned {start_resp.status_code} for {adk_service_name}")
                            except Exception as e:
                                logger.warning(f"⚠️ No se pudo iniciar {adk_service_name} via Ejecutor: {e}")

                            await asyncio.sleep(2)  # Esperar a que ADK inicie

                            # Step 2: Enviar comando "open" al ADK
                            from app.dispatcher import send_command
                            from app.models import OrchestratorCommand

                            ack = await send_command(
                                adk_service_name,
                                OrchestratorCommand(action="open", service=adk_service_name)
                            )

                            if ack.get("status") == "ok":
                                logger.info(f"✅ {adk_service_name} respondió correctamente")
                            else:
                                logger.warning(f"⚠️ {adk_service_name} no respondió: {ack.get('error')}")

                            # Step 3: Para Sentinel ADK, activar monitoreo en el Core
                            # El ADK no hace file watching, solo el Core lo hace
                            if agent_name == "sentinel":
                                await asyncio.sleep(1)
                                logger.info(f"🛡️ Activando monitoreo de archivos en Sentinel Core...")
                                try:
                                    # Determinar modo auto: si NO requiere aprobación crítica, entonces auto=true
                                    is_auto = not (cerebro_config.require_approval_critical if cerebro_config else True)
                                    # Usar sentinel_core explícitamente para comando monitor
                                    monitor_ack = await send_command(
                                        "sentinel_core",
                                        OrchestratorCommand(
                                            action="monitor",
                                            target=project_path,
                                            options={"auto": is_auto}
                                        )
                                    )
                                    if monitor_ack.get("status") == "ok":
                                        logger.info(f"✅ Monitoreo de archivos activado en Sentinel Core (auto={is_auto})")
                                    else:
                                        logger.warning(f"⚠️ No se pudo activar monitoreo: {monitor_ack.get('error')}")
                                except Exception as e:
                                    logger.error(f"❌ Error activando monitoreo: {e}")

                        else:
                            # Core mode only
                            try:
                                import httpx
                                async with httpx.AsyncClient(timeout=10.0) as client:
                                    start_resp = await client.post(
                                        f"{settings.executor_url}/command",
                                        json={
                                            "action": "open",
                                            "service": core_service_name,
                                            "request_id": f"cerebro-custom-{core_service_name}"
                                        }
                                    )
                                    if start_resp.status_code == 200:
                                        logger.info(f"✅ Ejecutor inició {core_service_name}")
                                    else:
                                        logger.warning(f"⚠️ Ejecutor returned {start_resp.status_code} for {core_service_name}")
                            except Exception as e:
                                logger.warning(f"⚠️ No se pudo iniciar {core_service_name} via Ejecutor: {e}")

                            await asyncio.sleep(2)

                            # Enviar comando al Core
                            from app.dispatcher import send_command
                            from app.models import OrchestratorCommand

                            ack = await send_command(
                                core_service_name,
                                OrchestratorCommand(action="open", service=core_service_name)
                            )

                            if ack.get("status") == "ok":
                                logger.info(f"✅ {core_service_name} respondió correctamente")
                            else:
                                logger.warning(f"⚠️ {core_service_name} no respondió: {ack.get('error')}")

                        # Pequeña pausa entre agentes para no saturar
                        await asyncio.sleep(1)

                    except Exception as agent_error:
                        logger.error(f"❌ Error iniciando {agent_name}: {agent_error}")
                        continue  # Continuar con el siguiente agente

                # Finalmente activar el proyecto
                result = await orchestrator._projects.set_active(project_name)
                logger.info(f"Background init completado para {project_name}: {result}")

            except Exception as e:
                logger.exception(f"Error en background init: {e}")

        asyncio.create_task(background_init())

        return ApiResponse(ok=True, message="Proyecto seleccionado - Iniciando agentes en segundo plano", data={
            "project_name": project_name,
            "project_path": project_path.replace("\\", "/"),
            "workspace_root": parent_dir.replace("\\", "/")
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error seleccionando proyecto personalizado")
        return ApiResponse(ok=False, message=f"Error: {str(e)}", data=None)
