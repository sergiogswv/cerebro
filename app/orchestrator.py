import logging
import os
import json
from app.models import AgentEvent, Severity, OrchestratorCommand
from app.dispatcher import send_command, notify

logger = logging.getLogger("cerebro.orchestrator")

from app.config import get_settings
settings = get_settings()

class Orchestrator:
    """
    Cerebro del sistema.
    Recibe eventos de los agentes, evalúa su severidad y decide qué hacer:
    - Notificar al usuario
    - Encadenar otros agentes
    - Ignorar (solo loggear)
    """

    def __init__(self):
        self.active_project = None
        self.monitored_project = None # Rastreo de éxito de monitorización
        self.workspace_root = settings.workspace_root

    async def handle_event(self, event: AgentEvent) -> dict:
        from app.sockets import emit_agent_event
        # Emitir a través de Socket.IO para el Dashboard
        await emit_agent_event(event.model_dump(mode="json"))

        logger.info(
            f"📥 [{event.source.upper()}] type={event.type} "
            f"severity={event.severity} id={event.id}"
        )

        result = {"event_id": event.id, "actions": []}

        # ── Lógica de decisión por severidad ──────────────────────────────────

        if event.severity == Severity.critical:
            result["actions"].append(await self._handle_critical(event))

        elif event.severity == Severity.error:
            result["actions"].append(await self._handle_error(event))

        elif event.severity == Severity.warning:
            result["actions"].append(await self._handle_warning(event))

        elif event.type == "interaction_required":
            result["actions"].append(await self._handle_interaction(event))

        else:  # info
            result["actions"].append(await self._handle_info(event))

        return result

    # ── Handlers por severidad ────────────────────────────────────────────────

    async def _handle_critical(self, event: AgentEvent) -> dict:
        """CRITICAL: notificar de inmediato, detener lo que haga falta."""
        logger.warning(f"🚨 CRITICAL desde {event.source}")

        message = self._build_message(event)
        sent = await notify(message, level="critical", source=event.source)

        # Si es de warden (secreto expuesto), pausar sentinel como medida de seguridad
        if event.source == "warden":
            await send_command(
                "sentinel",
                OrchestratorCommand(action="stop", options={"reason": "security_alert"})
            )

        return {"action": "notify_critical", "delivered": sent}

    async def _handle_error(self, event: AgentEvent) -> dict:
        """ERROR: notificar al usuario."""
        logger.error(f"❌ ERROR desde {event.source}")

        message = self._build_message(event)
        sent = await notify(message, level="error", source=event.source)

        return {"action": "notify_error", "delivered": sent}

    async def _handle_warning(self, event: AgentEvent) -> dict:
        """WARNING: notificar y, si viene de sentinel, encadenar architect."""
        logger.warning(f"⚠️ WARNING desde {event.source}")

        actions = []

        # Encadenamiento: cambio de archivo → análisis de lint
        if event.source == "sentinel" and "file" in event.payload:
            target_file = event.payload.get("file")
            logger.info(f"🔗 Encadenando Architect sobre {target_file}")
            ack = await send_command(
                "architect",
                OrchestratorCommand(action="lint", target=target_file)
            )
            actions.append({"action": "chain_architect", "ack": ack})

        message = self._build_message(event)
        sent = await notify(message, level="warning", source=event.source)
        actions.append({"action": "notify_warning", "delivered": sent})

        return {"action": "handle_warning", "steps": actions}

    async def _handle_info(self, event: AgentEvent) -> dict:
        """INFO: solo loggear, no notificar (evitar spam)."""
        logger.info(f"ℹ️ INFO desde {event.source} — {event.type}")
        return {"action": "logged_only"}

    async def _handle_interaction(self, event: AgentEvent) -> dict:
        """Pide una respuesta al usuario via Notificador"""
        logger.info(f"❓ INTERACTION requerida por {event.source}")
        
        prompt_id = event.payload.get("prompt_id")
        message = event.payload.get("message", "Confirmación requerida")
        
        from app.dispatcher import get_settings
        import httpx
        settings = get_settings()
        
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{settings.notifier_url}/ask-interaction",
                    json={
                        "message": message,
                        "prompt_id": prompt_id,
                        "source": event.source # Para saber a quien responder luego
                    },
                    timeout=30.0 # Aumentado de 5.0 a 30.0
                )
            return {"action": "ask_interaction", "status": "sent", "prompt_id": prompt_id}
        except Exception as e:
            logger.error(f"Error pidiendo interaccion: {e}")
            return {"action": "ask_interaction", "status": "error", "message": str(e)}

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_message(self, event: AgentEvent) -> str:
        """Construye el mensaje legible para el usuario."""
        lines = [
            f"*[{event.source.upper()}]* — `{event.type}`",
            f"Severidad: `{event.severity}`",
        ]

        payload = event.payload
        if "file" in payload:
            lines.append(f"Archivo: `{payload['file']}`")
        if "message" in payload:
            lines.append(f"Detalle: {payload['message']}")
        if "suggestion" in payload:
            lines.append(f"Sugerencia: _{payload['suggestion']}_")
        if "finding" in payload:
            lines.append(f"Hallazgo: {payload['finding']}")
        if "recommendation" in payload:
            lines.append(f"Acción: {payload['recommendation']}")

        return "\n".join(lines)

    async def bootstrap(self):
        """
        Escanea el workspace y pide al usuario seleccionar proyecto via Notificador.
        """
        logger.info("🚀 Iniciando Bootstrap del sistema")
        
        try:
            # Escanear directorios en Documents/dev
            projects = [d for d in os.listdir(self.workspace_root) 
                       if os.path.isdir(os.path.join(self.workspace_root, d)) 
                       and not d.startswith(".")]
            
            # Limitar a los mas relevantes para no saturar el bot
            projects = sorted(projects)[:10] 

            # Llamar al notificador
            from app.dispatcher import get_settings
            import httpx
            settings = get_settings()
            
            try:
                async with httpx.AsyncClient() as client:
                    await client.post(
                        f"{settings.notifier_url}/ask-project",
                        json={"projects": projects},
                        timeout=10.0 # Aumentado de 2.0 a 10.0
                    )
            except Exception as e:
                logger.warning(f"⚠️ Notificador no disponible: {e}. El sistema funcionará solo via Dashboard.")
            
            return {"status": "ok", "scanned": len(projects), "projects": projects}
        except Exception as e:
            logger.error(f"Error en bootstrap: {e}")
            return {"status": "error", "message": str(e)}

    async def set_active_project(self, project_name: str):
        """Configura el proyecto en el que se trabajara"""
        # Solo omitimos si el proyecto es el mismo Y ya logramos monitorearlo con éxito
        if self.active_project == project_name and self.monitored_project == project_name:
            logger.info(f"ℹ️ El proyecto {project_name} ya está activo y monitoreado. Omitiendo duplicidad.")
            return {"status": "ok", "project": project_name, "restarted": False}
        
        self.active_project = project_name
        logger.info(f"📁 Proyecto activo establecido: {project_name}")
        
        from app.sockets import emit_system_status
        await emit_system_status({"type": "project_selected", "project": project_name})
        
        # Arrancar Sentinel automaticamente para ese proyecto
        project_path = os.path.join(self.workspace_root, project_name).replace("\\", "/")
        
        ack = await send_command(
            "sentinel",
            OrchestratorCommand(action="monitor", target=project_path)
        )
        
        # Si el monitor fue aceptado, registramos el éxito
        if ack.get("status") != "rejected":
            self.monitored_project = project_name
            logger.info(f"✅ Sentinel monitoreando exitosamente: {project_name}")
        else:
            self.monitored_project = None
            logger.warning(f"⚠️ Sentinel no pudo iniciar monitoreo (posiblemente aún arrancando): {ack.get('error')}")

        await notify(
            f"Listo! He configurado el entorno para `{project_name}`.\nSentinel y Architect estan preparados.",
            level="info",
            source="cerebro"
        )
        
        return {"status": "ok", "project": project_name, "restarted": True}

    async def get_architect_config(self) -> dict:
        """Lee el archivo architect.json del proyecto activo"""
        if not self.active_project:
            return {"error": "No hay proyecto activo seleccionado"}
        
        config_path = os.path.join(self.workspace_root, self.active_project, "architect.json")
        if not os.path.exists(config_path):
            # Retornar una estructura básica si no existe
            return {
                "version": "1.0",
                "rules": [],
                "exclude": ["**/node_modules/**"]
            }
            
        import json
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error leyendo architect.json: {e}")
            return {"error": str(e)}

    async def save_architect_config(self, config: dict) -> dict:
        """Guarda el archivo architect.json en el proyecto activo"""
        if not self.active_project:
            return {"error": "No hay proyecto activo seleccionado"}
            
        config_path = os.path.join(self.workspace_root, self.active_project, "architect.json")
        import json
        try:
            # Validar que sea un JSON válido antes de guardar (ya es dict, pero por si acaso)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            
            logger.info(f"✅ architect.json actualizado para {self.active_project}")
            
            # Notificar al dashboard y potencialmente a Architect (si tuviera reload)
            from app.sockets import emit_agent_event
            await emit_agent_event({
                "source": "architect",
                "type": "config_updated",
                "severity": "info",
                "payload": {"message": "Configuración de arquitectura actualizada desde Skrymir"}
            })
            
            return {"status": "ok"}
        except Exception as e:
            logger.error(f"Error guardando architect.json: {e}")
            return {"status": "error", "message": str(e)}

    async def get_sentinel_config(self) -> dict:
        """Lee el archivo .sentinelrc.toml del proyecto activo"""
        if not self.active_project:
            return {"error": "No hay proyecto activo seleccionado"}
        
        config_path = os.path.join(self.workspace_root, self.active_project, ".sentinelrc.toml")
        if not os.path.exists(config_path):
            return {"error": "Archivo .sentinelrc.toml no encontrado"}
            
        import toml
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return toml.load(f)
        except Exception as e:
            logger.error(f"Error leyendo .sentinelrc.toml: {e}")
            return {"error": str(e)}

    async def save_sentinel_config(self, config: dict) -> dict:
        """Guarda el archivo .sentinelrc.toml en el proyecto activo"""
        if not self.active_project:
            return {"error": "No hay proyecto activo seleccionado"}
            
        config_path = os.path.join(self.workspace_root, self.active_project, ".sentinelrc.toml")
        import toml
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                toml.dump(config, f)
            return {"status": "ok"}
        except Exception as e:
            logger.error(f"Error guardando .sentinelrc.toml: {e}")
            return {"status": "error", "message": str(e)}

    async def sentinel_init(self) -> dict:
        """Lanza el comando de inicialización de Sentinel"""
        if not self.active_project:
            return {"error": "No hay proyecto activo seleccionado"}
            
        project_path = os.path.join(self.workspace_root, self.active_project).replace("\\", "/")
        
        # Enviar comando a Sentinel para que se inicialice (o re-inicialice)
        ack = await send_command(
            "sentinel",
            OrchestratorCommand(
                action="monitor", # Sentinel proyecta el setup si no hay config al monitorear
                target=project_path
            )
        )
        
        return {"status": "ok", "ack": ack}

    async def architect_init(self, pattern: str | None = None) -> dict:
        """Llama al ejecutor para correr el comando 'init' de Architect en el proyecto activo"""
        if not self.active_project:
            return {"error": "No hay proyecto activo seleccionado"}

        project_path = os.path.join(self.workspace_root, self.active_project).replace("\\", "/")

        # LOG de inicio
        from app.sockets import emit_agent_event
        await emit_agent_event({
            "source": "architect",
            "type": "init_started",
            "severity": "info",
            "payload": {"message": f"Iniciando Magia ({pattern or 'Default'}) en {self.active_project}... Esto tomará unos segundos."}
        })

        # Enviamos acción 'run' para ejecución one-shot y esperamos
        # El ejecutor ahora usará asyncio.to_thread para no bloquear
        options = {
            "init": True,
            "force": True
        }
        if pattern:
            options["pattern"] = pattern

        ack = await send_command(
            "ejecutor", # Enviamos al ejecutor directamente
            OrchestratorCommand(
                action="run",
                service="architect",
                target=project_path,
                options=options
            )
        )

        logger.info(f"🔵 architect_init ack: {ack}")

        # Guardamos la configuración con las reglas del patrón (independientemente del ack)
        # Definimos las reglas base para cada patrón
        pattern_rules = {
            "hexagonal": {
                "rules": [
                    {"name": "No Direct Infrastructure Imports", "pattern": "domain/**/*.* -> infrastructure/**/*.*", "severity": "error"},
                    {"name": "Domain Independence", "pattern": "domain/**/*.* -> application/**/*.*", "severity": "error"}
                ]
            },
            "clean": {
                "rules": [
                    {"name": "Entities Pure", "pattern": "entities/**/*.* -> use-cases/**/*.*", "severity": "error"},
                    {"name": "Use Cases Isolation", "pattern": "use-cases/**/*.* -> adapters/**/*.*", "severity": "error"}
                ]
            },
            "layered": {
                "rules": [
                    {"name": "No Upward Dependencies", "pattern": "controllers/**/*.* -> services/**/*.*", "severity": "warning"},
                    {"name": "Repository Abstraction", "pattern": "services/**/*.* -> repositories/**/*.*", "severity": "error"}
                ]
            },
            "ddd": {
                "rules": [
                    {"name": "Aggregate Root Access", "pattern": "**/*.* -> aggregates/**/root.*", "severity": "error"},
                    {"name": "Value Object Immutability", "pattern": "value-objects/**/*.*", "severity": "warning"}
                ]
            },
            "cqrs": {
                "rules": [
                    {"name": "Command Query Separation", "pattern": "commands/**/*.* -> queries/**/*.*", "severity": "error"},
                    {"name": "Event Handler Isolation", "pattern": "handlers/**/*.* -> events/**/*.*", "severity": "warning"}
                ]
            },
            "modular": {
                "rules": [
                    {"name": "Module Boundary", "pattern": "modules/*/internal/**/*.* -> modules/*/**/*.*", "severity": "error"},
                    {"name": "Public API Only", "pattern": "modules/**/*.* -> modules/*/index.*", "severity": "error"}
                ]
            },
            "mvc": {
                "rules": [
                    {"name": "No Business Logic in Controllers", "pattern": "controllers/**/*.* -> models/**/*.*", "severity": "error"},
                    {"name": "View Isolation", "pattern": "views/**/*.* -> controllers/**/*.*", "severity": "warning"}
                ]
            },
            "feature": {
                "rules": [
                    {"name": "Feature Encapsulation", "pattern": "features/*/internal/**/*.* -> features/*/**/*.*", "severity": "error"},
                    {"name": "Shared Module Boundary", "pattern": "features/**/*.* -> shared/**/*.*", "severity": "warning"}
                ]
            },
            "microkernel": {
                "rules": [
                    {"name": "Core Stability", "pattern": "plugins/**/*.* -> core/**/*.*", "severity": "error"},
                    {"name": "Plugin Interface", "pattern": "core/**/*.* -> plugins/*/interface.*", "severity": "error"}
                ]
            },
            "event": {
                "rules": [
                    {"name": "Event Handler Decoupling", "pattern": "handlers/**/*.* -> publishers/**/*.*", "severity": "error"},
                    {"name": "Message Contract", "pattern": "messages/**/*.*", "severity": "warning"}
                ]
            }
        }

        # Guardar configuración con las reglas del patrón
        config_path = os.path.join(project_path, "architect.json")
        rules_to_save = pattern_rules.get(pattern, pattern_rules.get("layered"))

        try:
            import json
            config_to_save = {
                "name": f"{pattern}-architecture",
                "rules": rules_to_save.get("rules", [])
            }
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config_to_save, f, indent=2)

            logger.info(f"✅ Configuración de arquitectura guardada en {config_path}")
            logger.info(f"📄 Contenido guardado: {json.dumps(config_to_save, indent=2)}")
        except Exception as e:
            logger.error(f"❌ Error guardando configuración en {config_path}: {e}")

        # Si el comando terminó OK, avisamos al dashboard para que recargue
        await emit_agent_event({
            "source": "architect",
            "type": "init_completed",
            "severity": "info",
            "payload": {"message": "¡Magia completada! Arquitectura base generada exitosamente."}
        })

        return {"status": "ok", "ack": ack}
    async def get_architect_patterns(self) -> list:
        """Retorna los patrones disponibles para el framework detectado en el proyecto activo"""
        if not self.active_project:
            return []
        project_path = os.path.join(self.workspace_root, self.active_project)

        # Usamos el detector de Architect para saber el framework
        from app.dispatcher import AGENT_URLS
        import httpx
        try:
            # Una forma rápida es preguntar al detector (o re-implementarlo aquí)
            # Por ahora, devolvemos una lista genérica premium si es NestJS
            if os.path.exists(os.path.join(project_path, "nest-cli.json")):
                return [
                    {"id": "hexagonal", "label": "Hexagonal", "description": "Ports & Adapters. Domain at the core, infrastructure on the outside."},
                    {"id": "clean", "label": "Clean Architecture", "description": "Uncle Bob's approach. Entities → Use Cases → Interface Adapters → Frameworks."},
                    {"id": "layered", "label": "Layered (N-Layer)", "description": "Traditional separation: Presentation, Business, Data Access layers."},
                    {"id": "ddd", "label": "Domain-Driven Design", "description": "Bounded contexts, aggregates, entities, and value objects."},
                    {"id": "cqrs", "label": "CQRS + Event Sourcing", "description": "Command Query Responsibility Segregation with event persistence."},
                    {"id": "modular", "label": "Modular Monolith", "description": "Single deployment unit with strict module boundaries and contracts."}
                ]
            return [
                {"id": "mvc", "label": "MVC", "description": "Classic Model-View-Controller separation."},
                {"id": "hexagonal", "label": "Hexagonal", "description": "Ports & Adapters pattern for testability."},
                {"id": "layered", "label": "Layered", "description": "N-Tier architecture with clear separation."},
                {"id": "feature", "label": "Feature-First", "description": "Organize by features/modules instead of technical layers."},
                {"id": "microkernel", "label": "Microkernel", "description": "Core system + plugins for extensibility."},
                {"id": "event", "label": "Event-Driven", "description": "Loose coupling through events and message handlers."}
            ]
        except:
            return []

    async def get_ai_config(self) -> dict:
        """Carga .architect.ai.json del proyecto activo"""
        if not self.active_project:
            return {}
        path = os.path.join(self.workspace_root, self.active_project, ".architect.ai.json")
        if not os.path.exists(path):
            return {"configs": [], "selected_name": ""}
        try:
            with open(path, "r", encoding="utf-8") as f:
                import json
                return json.load(f)
        except Exception as e:
            logger.error(f"Error cargando AI config: {e}")
            return {"error": str(e)}

    async def save_ai_config(self, config: dict) -> dict:
        """Guarda .architect.ai.json en el proyecto activo y propaga a Sentinel si aplica"""
        if not self.active_project:
            return {"status": "error", "message": "No hay proyecto activo"}
        
        project_dir = os.path.join(self.workspace_root, self.active_project)
        ai_path = os.path.join(project_dir, ".architect.ai.json")
        sentinel_path = os.path.join(project_dir, ".sentinelrc.toml")

        try:
            # 1. Guardar config global (JSON)
            import json
            with open(ai_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
            
            # 2. Propagar a Sentinel si hay una selección activa
            selected_name = config.get("selected_name")
            if selected_name and os.path.exists(sentinel_path):
                selected_config = next((c for c in config.get("configs", []) if c.get("name") == selected_name), None)
                if selected_config:
                    import toml
                    with open(sentinel_path, "r", encoding="utf-8") as f:
                        sentinel_contents = toml.load(f)
                    
                    # Actualizar sección primary_model
                    sentinel_contents["primary_model"] = {
                        "name": selected_config.get("model", ""),
                        "url": selected_config.get("api_url", ""),
                        "api_key": selected_config.get("api_key", ""),
                        "provider": selected_config.get("provider", "anthropic").lower()
                    }

                    with open(sentinel_path, "w", encoding="utf-8") as f:
                        toml.dump(sentinel_contents, f)
                    
                    logger.info(f"🚀 AI Config propagada a Sentinel en {self.active_project}")

            return {"status": "success"}
        except Exception as e:
            logger.error(f"Error guardando AI config: {e}")
            return {"status": "error", "message": str(e)}

    async def validate_ai_provider(self, url: str, key: str, provider: str) -> dict:
        """Intenta listar modelos del proveedor para validar URL y API Key"""
        import httpx

        # Normalizar URL (OpenAI/Claude suelen tener /v1/models o similar)
        endpoint = url.rstrip("/")
        if "ollama" in provider.lower():
            endpoint = f"{endpoint}/api/tags"
        elif "claude" in provider.lower() or "anthropic" in provider.lower():
            # Anthropic no tiene un endpoint de 'models' tan simple como OpenAI,
            # pero para validar solemos probar un request vacío o usar su metadata.
            # Por simplicidad en este prototipo, usamos el estándar OpenAI si la URL lo parece.
            if "/v1" not in endpoint: endpoint = f"{endpoint}/v1/models"
            else: endpoint = f"{endpoint}/models"
        else:
            if "/v1" not in endpoint: endpoint = f"{endpoint}/v1/models"
            else: endpoint = f"{endpoint}/models"

        headers = {}
        if "ollama" not in provider.lower():
            headers["Authorization"] = f"Bearer {key}"
            if "claude" in provider.lower():
                headers["x-api-key"] = key
                headers["anthropic-version"] = "2023-06-01"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(endpoint, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    models = []
                    # Extraer modelos según formato
                    if "models" in data: # Ollama
                        models = [m["name"] for m in data["models"]]
                    elif "data" in data: # OpenAI
                        models = [m["id"] for m in data["data"]]
                    return {"ok": True, "models": models}
                else:
                    return {"ok": False, "error": f"Error {resp.status_code}: {resp.text}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def generate_ai_rules_for_pattern(self, pattern: str, project_path: str = None) -> dict:
        """
        Genera reglas de arquitectura usando IA para un patrón dado.
        Similar al flujo del CLI: detecta framework, obtiene contexto, consulta IA.
        """
        from app.ai_utils import (
            AIConfig, get_project_context, sugerir_reglas_para_patron,
            sugerir_top_3_arquitecturas, sugerir_arquitectura_inicial
        )

        target_project = project_path or self.active_project
        if not target_project:
            return {"error": "No hay proyecto activo ni se especificó uno"}

        # Ruta completa del proyecto
        if os.path.isabs(target_project):
            full_path = target_project
        else:
            full_path = os.path.join(self.workspace_root, target_project)

        if not os.path.exists(full_path):
            return {"error": f"Proyecto no encontrado: {full_path}"}

        # Cargar configuración de IA del proyecto
        ai_config_path = os.path.join(full_path, ".architect.ai.json")
        ai_configs = []

        if os.path.exists(ai_config_path):
            try:
                with open(ai_config_path, "r", encoding="utf-8") as f:
                    ai_config_data = json.load(f)
                    for cfg in ai_config_data.get("configs", []):
                        ai_configs.append(AIConfig(
                            name=cfg.get("name", "Unknown"),
                            provider=cfg.get("provider", "Claude"),
                            api_url=cfg.get("api_url", ""),
                            api_key=cfg.get("api_key", ""),
                            model=cfg.get("model", "")
                        ))
            except Exception as e:
                logger.error(f"Error cargando AI config: {e}")
                return {"error": "No hay configuración de IA válida. Configura una en el Architect Control Center."}
        else:
            return {"error": "No hay configuración de IA (.architect.ai.json). Configura una en el Architect Control Center > AI Config."}

        if not ai_configs:
            return {"error": "No hay proveedores de IA configurados. Agrega al menos uno en AI Config."}

        # Obtener contexto del proyecto
        context = get_project_context(full_path)
        logger.info(f"🔍 Contexto del proyecto: framework={context['framework']}, deps={len(context['dependencies'])}, folders={len(context['folder_structure'])}")

        # Si no se especificó patrón, hacer análisis automático
        if not pattern:
            logger.info("🧠 Realizando análisis automático de arquitectura con IA...")
            result = await sugerir_arquitectura_inicial(context, ai_configs)
            if not result:
                return {"error": "No se pudo obtener una respuesta de la IA. Verifica la configuración de IA."}
        else:
            # Generar reglas para el patrón específico
            logger.info(f"🧠 Generando reglas para patrón '{pattern}' con IA...")
            result = await sugerir_reglas_para_patron(pattern, context, ai_configs)
            if not result:
                return {"error": "No se pudo obtener una respuesta de la IA. Verifica la configuración de IA."}

        logger.info(f"✅ IA generó {len(result.rules)} reglas para el patrón '{result.pattern}'")

        # Retornar reglas en formato serializable
        return {
            "ok": True,
            "pattern": result.pattern,
            "suggested_max_lines": result.suggested_max_lines,
            "rules": [r.to_dict() for r in result.rules]
        }

    async def get_ai_architecture_suggestions(self, project_path: str = None) -> dict:
        """
        Obtiene sugerencias de top 3 arquitecturas desde IA.
        """
        from app.ai_utils import AIConfig, get_project_context, sugerir_top_6_arquitecturas

        target_project = project_path or self.active_project
        if not target_project:
            return {"error": "No hay proyecto activo"}

        full_path = os.path.join(self.workspace_root, target_project) if not os.path.isabs(target_project) else target_project

        logger.info(f"🔍 get_ai_architecture_suggestions: target_project={target_project}, full_path={full_path}")

        if not os.path.exists(full_path):
            return {"error": f"Proyecto no encontrado: {full_path}"}

        # Cargar configuración de IA
        ai_config_path = os.path.join(full_path, ".architect.ai.json")
        ai_configs = []

        logger.info(f"🔍 Buscando AI config en: {ai_config_path}")
        logger.info(f"🔍 full_path: {full_path}")
        logger.info(f"🔍 workspace_root: {self.workspace_root}")
        logger.info(f"🔍 active_project: {self.active_project}")
        logger.info(f"🔍 ai_config_path exists: {os.path.exists(ai_config_path)}")

        if os.path.exists(ai_config_path):
            try:
                with open(ai_config_path, "r", encoding="utf-8") as f:
                    file_content = f.read()
                    logger.info(f"🔍 AI config file content (first 500 chars): {file_content[:500]}")
                    ai_config_data = json.loads(file_content)
                    logger.info(f"🔍 AI config data parsed: {ai_config_data}")
                    logger.info(f"🔍 AI config data type: {type(ai_config_data)}")

                    configs_list = ai_config_data.get("configs", [])
                    logger.info(f"🔍 configs_list: {configs_list} (type: {type(configs_list)})")

                    for i, cfg in enumerate(configs_list):
                        logger.info(f"🔍 Processing config {i}: {cfg}")
                        if cfg and isinstance(cfg, dict):
                            ai_configs.append(AIConfig(
                                name=cfg.get("name", "Unknown"),
                                provider=cfg.get("provider", "Claude"),
                                api_url=cfg.get("api_url", ""),
                                api_key=cfg.get("api_key", ""),
                                model=cfg.get("model", "")
                            ))
                    logger.info(f"🔍 Loaded {len(ai_configs)} AI configs")
            except Exception as e:
                logger.error(f"❌ Error cargando AI config: {e}")
                logger.exception(e)  # Log full traceback
        else:
            logger.warning(f"⚠️ Archivo no encontrado: {ai_config_path}")
            # Listar archivos en el directorio para debug
            try:
                files = os.listdir(full_path)
                json_files = [f for f in files if f.endswith('.json')]
                logger.info(f"🔍 Archivos JSON en {full_path}: {json_files}")
            except Exception as e:
                logger.error(f"❌ No se pudo listar directorio: {e}")

        if not ai_configs:
            # No hay IA configurada - devolver error para que el frontend pregunte
            logger.warning("⚠️ No hay AI configs después de procesar")
            return {"error": "No hay configuración de IA. Configura un proveedor en AI Config primero."}

        # Obtener contexto y sugerencias
        logger.info(f"🧠 Obteniendo contexto del proyecto: {full_path}")
        framework = get_project_context(full_path)["framework"]
        logger.info(f"🧠 Framework detectado: {framework}")

        logger.info(f"🧠 Llamando a sugerir_top_6_arquitecturas con framework={framework}, ai_configs={len(ai_configs)}")
        top_6 = await sugerir_top_6_arquitecturas(framework, ai_configs)
        logger.info(f"🧠 Resultado de sugerir_top_6_arquitecturas: {top_6}")

        if not top_6:
            # La IA está configurada pero falló la llamada - esto es un error real
            logger.error("❌ La IA está configurada pero falló al generar sugerencias. Posibles causas: timeout, error HTTP, JSON inválido, o API key inválida.")
            # Verificar si podemos obtener más detalles del error revisando logs
            config_names = [cfg.name for cfg in ai_configs]
            return {
                "error": f"La IA ({', '.join(config_names)}) falló al generar sugerencias. Revisa: 1) Conexión a internet, 2) API key válida, 3) Modelo disponible, 4) Créditos suficientes."
            }

        return {
            "ok": True,
            "framework": framework,
            "patterns": [
                {"id": opt.name.lower().replace(" ", "-"), "label": opt.name, "description": opt.description}
                for opt in top_6
            ]
        }

    def _get_default_patterns(self, project_path: str) -> list:
        """Retorna 3 patrones por defecto si no hay IA disponible"""
        if os.path.exists(os.path.join(project_path, "nest-cli.json")):
            return [
                {"id": "hexagonal", "label": "Hexagonal", "description": "Ports & Adapters. Domain at the core, infrastructure on the outside."},
                {"id": "clean", "label": "Clean Architecture", "description": "Uncle Bob's approach. Entities → Use Cases → Interface Adapters → Frameworks."},
                {"id": "layered", "label": "Layered (N-Layer)", "description": "Traditional separation: Presentation, Business, Data Access layers."}
            ]
        return [
            {"id": "mvc", "label": "MVC", "description": "Classic Model-View-Controller separation."},
            {"id": "hexagonal", "label": "Hexagonal", "description": "Ports & Adapters pattern for testability."},
            {"id": "layered", "label": "Layered", "description": "N-Tier architecture with clear separation."}
        ]

    async def warden_scan(self, project: str | None = None) -> dict:
        """Ejecuta un escaneo de Warden sobre el proyecto activo o uno específico"""
        target = project or self.active_project
        if not target:
            return {"error": "No hay proyecto activo ni se especificó uno"}

        # Enviar comando directamente a Warden (modo servidor)
        from app.dispatcher import send_raw_command
        import uuid
        import os

        project_path = os.path.join(self.workspace_root, target).replace("\\", "/") if target != "." else "."

        command = {
            "action": "scan",
            "target": project_path,
            "request_id": f"warden-{str(uuid.uuid4())[:8]}"
        }

        ack = await send_raw_command("warden", command)
        return ack

    async def warden_predict_critical(self, project: str | None = None) -> dict:
        """Predice archivos críticos para el proyecto activo"""
        target = project or self.active_project
        if not target:
            return {"error": "No hay proyecto activo ni se especificó uno"}

        # Enviar comando directamente a Warden (modo servidor)
        from app.dispatcher import send_raw_command
        import uuid
        import os

        project_path = os.path.join(self.workspace_root, target).replace("\\", "/") if target != "." else "."

        command = {
            "action": "predict-critical",
            "target": project_path,
            "request_id": f"warden-{str(uuid.uuid4())[:8]}"
        }

        ack = await send_raw_command("warden", command)
        return ack

    async def warden_risk_assess(self, project: str | None = None) -> dict:
        """Evalúa riesgos del proyecto activo"""
        target = project or self.active_project
        if not target:
            return {"error": "No hay proyecto activo ni se especificó uno"}

        # Enviar comando directamente a Warden (modo servidor)
        from app.dispatcher import send_raw_command
        import uuid
        import os

        project_path = os.path.join(self.workspace_root, target).replace("\\", "/") if target != "." else "."

        command = {
            "action": "risk-assess",
            "target": project_path,
            "request_id": f"warden-{str(uuid.uuid4())[:8]}"
        }

        ack = await send_raw_command("warden", command)
        return ack

    async def warden_churn_report(self, project: str | None = None) -> dict:
        """Genera reporte de churn del proyecto activo"""
        target = project or self.active_project
        if not target:
            return {"error": "No hay proyecto activo ni se especificó uno"}

        # Enviar comando directamente a Warden (modo servidor)
        from app.dispatcher import send_raw_command
        import uuid
        import os

        project_path = os.path.join(self.workspace_root, target).replace("\\", "/") if target != "." else "."

        command = {
            "action": "churn-report",
            "target": project_path,
            "request_id": f"warden-{str(uuid.uuid4())[:8]}"
        }

        ack = await send_raw_command("warden", command)
        return ack


# Instancia global del orquestador
orchestrator = Orchestrator()
