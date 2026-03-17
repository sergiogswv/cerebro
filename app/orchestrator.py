import logging
import os
from app.models import AgentEvent, Severity, OrchestratorCommand
from app.dispatcher import send_command, notify

logger = logging.getLogger("cerebro.orchestrator")


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
        self.workspace_root = "C:/Users/Sergio/Documents/dev"

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
                    timeout=5.0
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
            
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{settings.notifier_url}/ask-project",
                    json={"projects": projects},
                    timeout=5.0
                )
            
            return {"status": "ok", "scanned": len(projects)}
        except Exception as e:
            logger.error(f"Error en bootstrap: {e}")
            return {"status": "error", "message": str(e)}

    async def set_active_project(self, project_name: str):
        """Configura el proyecto en el que se trabajara"""
        self.active_project = project_name
        logger.info(f"📁 Proyecto activo establecido: {project_name}")
        
        from app.sockets import emit_system_status
        await emit_system_status({"type": "project_selected", "project": project_name})
        
        # Arrancar Sentinel automaticamente para ese proyecto
        project_path = os.path.join(self.workspace_root, project_name).replace("\\", "/")
        
        await send_command(
            "sentinel",
            OrchestratorCommand(action="monitor", target=project_path)
        )
        
        await notify(
            f"Listo! He configurado el entorno para `{project_name}`.\nSentinel y Architect estan preparados.",
            level="info",
            source="cerebro"
        )
        
        return {"status": "ok", "project": project_name}

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
                    {"id": "hexagonal", "label": "Hexagonal", "description": "domain/ application/ infrastructure/"},
                    {"id": "clean", "label": "Clean Architecture", "description": "entities/ use-cases/ adapters/"},
                    {"id": "layered", "label": "Layered", "description": "controllers/ services/ repos/"}
                ]
            return [
                {"id": "mvc", "label": "MVC", "description": "Classic Model-View-Controller"},
                {"id": "hexagonal", "label": "Hexagonal", "description": "Domain-driven design"}
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
        """Guarda .architect.ai.json en el proyecto activo"""
        if not self.active_project:
            return {"error": "No hay proyecto activo"}
        path = os.path.join(self.workspace_root, self.active_project, ".architect.ai.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                import json
                json.dump(config, f, indent=2)
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


# Instancia global del orquestador
orchestrator = Orchestrator()
