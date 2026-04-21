"""Refactored Orchestrator using specialized components."""

import logging
import os
from pathlib import Path
from typing import Dict, Any, Optional

from app.config import get_settings
from app.models import AgentEvent
from app.decision_engine import DecisionEngine
from app.context_db import get_context_db
from app.change_manager import get_change_manager
from app.dispatcher import notify

from app.core import (
    PipelineCoordinator,
    AgentManager,
    EventRouter,
    ProjectManager,
)

logger = logging.getLogger("cerebro.orchestrator")
settings = get_settings()


class Orchestrator:
    """
    Central orchestrator using specialized components.

    This is a facade that delegates to:
    - PipelineCoordinator: Sequential analysis pipeline
    - AgentManager: Agent configs, wizards, AI provider handling
    - EventRouter: Event routing and decision handling
    - ProjectManager: Project lifecycle
    """

    def __init__(self):
        # Core dependencies
        self.context_db = get_context_db()
        self.change_manager = get_change_manager()
        self.decision_engine = DecisionEngine(architect_url=settings.architect_url)
        self.decision_engine.set_context_db(self.context_db)
        self.change_manager.set_orchestrator(self)

        # Specialized components
        self._pipeline = PipelineCoordinator()
        self._agents = AgentManager(settings.workspace_root, self.context_db)
        self._events = EventRouter(self.decision_engine, self.context_db)
        self._projects = ProjectManager(settings.workspace_root)
        # _initializing_projects ya NO es un set en memoria.
        # Se persiste en context_db.project_states para sobrevivir reinicios.
        # Usar self._initializing_projects (property) o context_db.get_projects_by_state()

        # Wire up pipeline
        self._pipeline.set_active_project(self._projects.active_project)

        # Recuperar proyectos en estado 'initializing' que sobrevivieron un reinicio
        _recovered = self.context_db.get_projects_by_state('initializing')
        if _recovered:
            logger.warning(
                f"⚡ Cerebro reiniciado durante init de proyectos: {_recovered}. "
                f"Marcando como 'error' para no bloquear agentes."
            )
            for p in _recovered:
                self.context_db.set_project_state(p, 'error', metadata={'reason': 'cerebro_restart'})

    @property
    def workspace_root(self) -> str:
        """Get the workspace root directory."""
        return self._projects.workspace_root

    @property
    def _initializing_projects(self) -> set:
        """
        Proyectos en proceso de inicialización AI.
        Lee desde la DB para ser resistente a reinicios de Cerebro.
        """
        return set(self.context_db.get_projects_by_state('initializing'))

    @workspace_root.setter
    def workspace_root(self, value: str):
        """Set the workspace root directory."""
        self._projects.workspace_root = value

    @property
    def active_project(self) -> Optional[str]:
        """Get the active project name."""
        return self._projects.active_project

    # ═══════════════════════════════════════════════════════════════════════
    # EVENT HANDLING (delegated to EventRouter)
    # ═══════════════════════════════════════════════════════════════════════

    async def handle_event(self, event: AgentEvent) -> Dict[str, Any]:
        """Handle incoming agent event."""
        return await self._events.route(event)

    # ═══════════════════════════════════════════════════════════════════════
    # PIPELINE (delegated to PipelineCoordinator)
    # ═══════════════════════════════════════════════════════════════════════

    async def start_pipeline_analysis(self, file_path: str, agents: list = None) -> Dict:
        """Start sequential analysis pipeline."""
        return await self._pipeline.start_analysis(file_path, agents)

    async def on_agent_analysis_complete(self, agent: str, findings: list, duration: float = None):
        """Handle agent analysis completion."""
        return await self._pipeline.on_agent_complete(agent, findings, duration)

    async def get_pipeline_status(self) -> Dict:
        """Get current pipeline status."""
        return self._pipeline.get_status()

    async def pipeline_action(self, action: str, **kwargs) -> Dict:
        """Execute pipeline action (retry/skip/abort/approve)."""
        return await self._pipeline.execute_action(action, **kwargs)

    def get_circuit_status(self) -> Dict:
        """Get circuit breaker status."""
        return self._pipeline.get_circuit_status()

    async def start_timeout_checker(self):
        """Start pipeline timeout checker."""
        await self._pipeline._start_timeout_checker()

    async def stop_timeout_checker(self):
        """Stop pipeline timeout checker."""
        await self._pipeline._stop_timeout_checker()

    # ═══════════════════════════════════════════════════════════════════════
    # AGENT MANAGEMENT (delegated to AgentManager)
    # ═══════════════════════════════════════════════════════════════════════

    async def get_sentinel_config(self) -> Dict:
        """Get Sentinel configuration."""
        return await self._agents.get_sentinel_config(self._projects.active_project)

    async def save_sentinel_config(self, config: Dict) -> Dict:
        """Save Sentinel configuration."""
        return await self._agents.save_sentinel_config(self._projects.active_project, config)

    async def get_architect_config(self) -> Dict:
        """Get Architect configuration."""
        return await self._agents.get_architect_config(self._projects.active_project)

    async def save_architect_config(self, config: Dict) -> Dict:
        """Save Architect configuration."""
        return await self._agents.save_architect_config(self._projects.active_project, config)

    async def get_ai_config(self) -> Dict:
        """Get AI configuration."""
        return await self._agents.get_ai_config(self._projects.active_project)

    async def save_ai_config(self, config: Dict) -> Dict:
        """Save AI configuration."""
        return await self._agents.save_ai_config(self._projects.active_project, config)

    async def validate_ai_provider(self, url: str, key: str, provider: str) -> Dict:
        """Validate AI provider."""
        return await self._agents.validate_ai_provider(url, key, provider)

    async def sentinel_init(self) -> Dict:
        """Start Sentinel wizard."""
        return await self._agents.start_sentinel_wizard(self._projects.active_project)

    async def handle_sentinel_wizard_response(self, wizard_id: str, step: str, answer: str) -> Dict:
        """Handle Sentinel wizard response."""
        return await self._agents.handle_wizard_response(
            self._projects.active_project, wizard_id, step, answer
        )

    # ═══════════════════════════════════════════════════════════════════════
    # PROJECT MANAGEMENT (delegated to ProjectManager)
    # ═══════════════════════════════════════════════════════════════════════

    async def bootstrap(self) -> Dict:
        """Bootstrap system and scan projects. (Automatic restoration disabled per user request)"""
        logger.info("🚀 Starting bootstrap")
        
        # NOTE: Restoration of previous active state is disabled to ensure 
        # manual selection on every startup.
        # active_projects = self.context_db.get_projects_by_state('active')
        # if active_projects:
        #     last_active = active_projects[0]
        #     logger.info(f"♻️  Restoring last active project: {last_active}")
        #     await self.set_active_project(last_active)

        return await self._projects.bootstrap()

    async def set_active_project(self, project: str) -> Dict:
        """Set active project and start monitoring with prioritized agents."""
        is_initializing = project in self._initializing_projects
        logger.info(f"🔍 [set_active_project] Checking `{project}` | is_initializing={is_initializing} | current_set={self._initializing_projects}")

        async def activate(project_name: str):
            """Callback when project is activated."""
            if is_initializing:
                logger.info(f"⏭️ Skipping agent activation for `{project_name}` (AI Initialization in progress)")
                return
            from app.dispatcher import send_command, notify as dispatcher_notify
            from app.models import OrchestratorCommand
            import httpx
            import asyncio

            project_path = self._projects.get_project_path(project_name)

            # Get auto-start configuration with priority order
            from app.config_manager import UnifiedConfigManager
            manager = UnifiedConfigManager.get_instance()
            unified_config = manager.get_config()
            cerebro_config = unified_config.cerebro if hasattr(unified_config, 'cerebro') else None
            auto_start_agents = cerebro_config.auto_start_agents if cerebro_config else ["sentinel"]

            logger.info(f"Activating project {project_name} with agent priority: {auto_start_agents}")

            # Ejecutor URL for starting agents
            from app.config import get_settings
            settings = get_settings()
            executor_url = settings.executor_url

            # Pre-population of .sentinelrc.toml if it doesn't exist to ensure stable headless startup
            # and inherit global AI configurations automatically.
            sentinel_rc_path = Path(project_path) / ".sentinelrc.toml"
            if not sentinel_rc_path.exists():
                logger.info(f"🛡️ .sentinelrc.toml not found for {project_name}. Generating headless global AI config...")
                try:
                    import toml
                    config = await self._agents._generate_sentinel_config(
                        Path(project_path), False, "headless-init"
                    )
                    with open(sentinel_rc_path, "w", encoding="utf-8") as f:
                        toml.dump(config, f)
                    logger.info("✅ Headless Sentinel config generated successfully.")
                except Exception as e:
                    logger.error(f"❌ Failed to generate headless Sentinel config: {e}")

            # Start each agent in priority order
            for agent_name in auto_start_agents:
                try:
                    # Get agent mode from cerebro config (overrides agent config)
                    agent_mode = cerebro_config.agent_modes.get(agent_name, "core") if cerebro_config else "core"
                    core_service_name = agent_name  # e.g., "architect"
                    adk_service_name = f"{agent_name}_adk"  # e.g., "architect_adk"
                    is_adk_mode = agent_mode == "adk"

                    logger.info(f"Starting {agent_name} in {agent_mode} mode (priority: {auto_start_agents.index(agent_name) + 1})")

                    # If ADK mode: start Core Engine FIRST, then ADK
                    if is_adk_mode:
                        # Step 1a: Start Core Engine via Ejecutor
                        try:
                            async with httpx.AsyncClient(timeout=10.0) as client:
                                start_resp = await client.post(
                                    f"{executor_url}/command",
                                    json={
                                        "action": "open",
                                        "service": core_service_name,
                                        "request_id": f"cerebro-auto-{core_service_name}-{project_name}"
                                    }
                                )
                                if start_resp.status_code == 200:
                                    logger.info(f"✅ Ejecutor started {core_service_name} (Core for ADK)")
                                else:
                                    logger.warning(f"⚠️ Ejecutor returned {start_resp.status_code} for {core_service_name}")
                        except Exception as e:
                            logger.warning(f"⚠️ Could not start {core_service_name} via Ejecutor: {e}")

                        # Wait for Core to be ready
                        await asyncio.sleep(3)

                        # Step 1b: Start ADK via Ejecutor
                        try:
                            async with httpx.AsyncClient(timeout=10.0) as client:
                                start_resp = await client.post(
                                    f"{executor_url}/command",
                                    json={
                                        "action": "open",
                                        "service": adk_service_name,
                                        "request_id": f"cerebro-auto-{adk_service_name}-{project_name}"
                                    }
                                )
                                if start_resp.status_code == 200:
                                    logger.info(f"✅ Ejecutor started {adk_service_name} (ADK)")
                                else:
                                    logger.warning(f"⚠️ Ejecutor returned {start_resp.status_code} for {adk_service_name}")
                        except Exception as e:
                            logger.warning(f"⚠️ Could not start {adk_service_name} via Ejecutor: {e}")

                        # Wait for ADK to be ready
                        await asyncio.sleep(2)

                        # Step 2: Send command "open" to ADK
                        try:
                            ack = await asyncio.wait_for(
                                send_command(
                                    adk_service_name,
                                    OrchestratorCommand(action="open", service=adk_service_name)
                                ),
                                timeout=10.0
                            )

                            if ack.get("status") != "rejected":
                                logger.info(f"✅ {adk_service_name} responded to 'open' command")
                        except asyncio.TimeoutError:
                            logger.error(f"Timeout sending 'open' to {adk_service_name}")
                        except Exception as e:
                            logger.error(f"Error sending 'open' to {adk_service_name}: {e}")

                        # Step 3: For Sentinel ADK, activate file monitoring in Core
                        # The ADK doesn't do file watching, only the Core does
                        if agent_name == "sentinel":
                            await asyncio.sleep(1)
                            try:
                                # Determine if auto mode should be enabled
                                is_auto = cerebro_config.auto_fix_enabled if cerebro_config else False

                                monitor_ack = await asyncio.wait_for(
                                    send_command(
                                        core_service_name,
                                        OrchestratorCommand(
                                            action="monitor",
                                            target=project_path,
                                            options={"auto": is_auto}
                                        )
                                    ),
                                    timeout=10.0
                                )

                                if monitor_ack.get("status") != "rejected":
                                    self._projects.set_monitored(project_name)
                                    logger.info(f"✅ Sentinel Core file monitoring activated for: {project_name}")
                                else:
                                    logger.warning(f"⚠️ Sentinel Core monitoring rejected: {monitor_ack.get('error')}")

                            except asyncio.TimeoutError:
                                logger.error(f"Timeout activating Sentinel Core monitoring")
                            except Exception as e:
                                logger.error(f"Error activating Sentinel Core monitoring: {e}")

                        # Emit ADK ready event for all agents
                        try:
                            from app.sockets import emit_agent_event
                            await emit_agent_event({
                                "source": agent_name,
                                "type": f"{agent_name}_adk_ready",
                                "severity": "info",
                                "payload": {"ready": True, "priority": auto_start_agents.index(agent_name) + 1, "mode": "adk"}
                            })
                        except Exception as e:
                            logger.error(f"Error emitting ADK ready event: {e}")

                    else:
                        # Core mode only
                        service_name = core_service_name
                        logger.info(f"Starting {service_name} (Core mode only)")

                        # Step 1: Start agent via Ejecutor
                        try:
                            async with httpx.AsyncClient(timeout=10.0) as client:
                                start_resp = await client.post(
                                    f"{executor_url}/command",
                                    json={
                                        "action": "open",
                                        "service": service_name,
                                        "request_id": f"cerebro-auto-{service_name}-{project_name}"
                                    }
                                )
                                if start_resp.status_code == 200:
                                    logger.info(f"✅ Ejecutor started {service_name}")
                                else:
                                    logger.warning(f"⚠️ Ejecutor returned {start_resp.status_code} for {service_name}")
                        except Exception as e:
                            logger.warning(f"⚠️ Could not start {service_name} via Ejecutor: {e}")

                        # Step 2: Wait for agent to be ready
                        await asyncio.sleep(2)

                        # Step 3: Send command to agent
                        try:
                            # Determine if auto mode should be enabled
                            # auto_fix_enabled=true → modo autónomo (no pregunta)
                            # require_approval_critical=true → requiere aprobación para críticos
                            is_auto = cerebro_config.auto_fix_enabled if cerebro_config else False

                            # Para Sentinel, el comando monitor SIEMPRE va al Core (no al ADK)
                            # El ADK no soporta file watching, solo el Core
                            target_agent = "sentinel_core" if agent_name == "sentinel" else service_name
                            ack = await asyncio.wait_for(
                                send_command(
                                    target_agent,
                                    OrchestratorCommand(
                                        action="monitor" if agent_name == "sentinel" else "open",
                                        target=project_path,
                                        options={"auto": is_auto}
                                    )
                                ),
                                timeout=10.0
                            )

                            if ack.get("status") != "rejected":
                                if agent_name == "sentinel":
                                    self._projects.set_monitored(project_name)
                                    logger.info(f"Sentinel monitoring: {project_name}")

                                from app.sockets import emit_agent_event
                                await emit_agent_event({
                                    "source": agent_name,
                                    "type": f"{agent_name}_ready",
                                    "severity": "info",
                                    "payload": {"ready": True, "priority": auto_start_agents.index(agent_name) + 1, "mode": "core"}
                                })
                            else:
                                logger.warning(f"{service_name} command rejected: {ack.get('error')}")

                        except asyncio.TimeoutError:
                            logger.error(f"Timeout sending command to {service_name}")
                        except Exception as e:
                            logger.error(f"Error sending command to {service_name}: {e}")

                    # Pause between agents
                    await asyncio.sleep(1)

                except Exception as e:
                    logger.error(f"Error starting {agent_name}: {e}")
                    continue

            await notify(
                f"Environment configured for `{project_name}`. Agents: {', '.join(auto_start_agents)}.",
                level="info",
                source="cerebro"
            )

        result = await self._projects.set_active(project, on_activate=activate)
        # Update pipeline with active project
        self._pipeline.set_active_project(project)

        # Arrancar ProactiveScheduler para el nuevo proyecto activo
        if not is_initializing:
            try:
                from app.proactive_scheduler import get_proactive_scheduler
                scheduler = get_proactive_scheduler()
                if not scheduler._running:
                    import asyncio
                    asyncio.create_task(scheduler.start(project))
                    logger.info(f"📅 ProactiveScheduler arrancado para proyecto '{project}'")
                else:
                    # Si ya corría, re-configurar con el proyecto nuevo
                    scheduler._project = project
                    scheduler.config = scheduler.get_config(project)
                    logger.info(f"📅 ProactiveScheduler re-configurado para '{project}'")
            except Exception as exc:
                logger.warning(f"⚠️  No se pudo iniciar ProactiveScheduler: {exc}")
        else:
            logger.info(f"📅 ProactiveScheduler en espera (proyecto `{project}` en inicialización AI)")

        return result

    async def create_project(self, name: str, project_type: str = "generic", description: str = "", base_path: Optional[str] = None) -> Dict:
        """Create a new project and optionally initialize with AI."""
        result = await self._projects.create_project(name, project_type, description, base_path)
        
        if result.get("status") == "ok" and (description or project_type != "generic"):
            # Persistir estado en DB (sobrevive reinicios)
            p_name = result["project"]
            self.context_db.set_project_state(
                p_name, 'initializing',
                metadata={'project_type': project_type, 'description': description[:200]}
            )
            logger.info(f"➕ [create_project] Proyecto `{p_name}` marcado como 'initializing' en DB.")
            
            # Si hay descripción o tipo, pedirle al ejecutor que inicialice el proyecto usando Aider
            import asyncio
            asyncio.create_task(self._ai_initialize_project(p_name, result["path"], project_type, description))
            
        return result

    async def _ai_initialize_project(self, name: str, path: str, project_type: str, description: str):
        """Initialize project using AI (Aider)."""
        from app.dispatcher import send_command, notify
        from app.models import OrchestratorCommand
        from app.sockets import emit_agent_event
        
        logger.info(f"🚀 AI Initialization for project {name} ({project_type})...")
        
        # Emitir evento para el timeline del dashboard
        await emit_agent_event({
            "source": "cerebro",
            "type": "project_init_started",
            "severity": "info",
            "payload": {
                "message": f"Construyendo estructura para `{name}` ({project_type})",
                "description": description,
                "path": path
            }
        })
        
        await notify(f"Inicializando estructura de `{name}` vía Cerebro IA...", level="info", source="cerebro")
        
        instruction = (
            f"Initialize a new {project_type} project named '{name}'. {description}. "
            f"Create the basic structure and essential files. "
            f"IMPORTANT: Follow best practices, create a robust .gitignore matching the {project_type} stack, "
            f"a standard README.md, and ensure the initial code is functional and follows professional architecture."
        )

        
        # Intentamos obtener configuración de IA desde el UnifiedConfigManager (Dashboard)
        from app.config_manager import UnifiedConfigManager
        config_manager = UnifiedConfigManager.get_instance()
        unified_config = config_manager.get_config()
        cerebro_conf = unified_config.cerebro
        
        provider = cerebro_conf.auto_fix_provider or "ollama"
        model = cerebro_conf.auto_fix_model or "qwen3:8b"
        api_key = cerebro_conf.auto_fix_api_key or ""
        base_url = cerebro_conf.auto_fix_base_url or ""

        # Log de qué estamos usando
        logger.info(f"🤖 Utilizando configuración de IA: provider={provider}, model={model}")
        
        options = {
            "instruction": instruction,
            "workspace_root": path,
            "provider": provider,
            "model": model,
            "api_key": api_key,
            "base_url": base_url,
            "require_run": True,
            "max_build_retries": 5
        }
        
        try:
            ack = await send_command(
                "ejecutor",
                OrchestratorCommand(
                    action="feature", # 'feature' usa Aider
                    target="README.md",
                    options=options
                )
            )
            
            if ack.get("status") == "accepted":
                await emit_agent_event({
                    "source": "cerebro",
                    "type": "project_init_queued",
                    "severity": "info",
                    "payload": {
                        "message": f"Tarea de construcción encolada en Executor para `{name}`",
                        "request_id": ack.get("request_id")
                    }
                })
        except Exception as e:
            logger.error(f"❌ Failed to send AI init command: {e}")
            await emit_agent_event({
                "source": "cerebro",
                "type": "project_init_error",
                "severity": "error",
                "payload": {"message": f"Error al iniciar construcción: {str(e)}"}
            })
            # Solo remover si falló el envío
            self.context_db.set_project_state(name, 'error', metadata={'reason': str(e)[:200]})


    async def get_architect_patterns(self, project: str = None) -> list:
        """Get available architecture patterns."""
        target = project or self._projects.active_project
        result = await self._agents.get_architect_suggestions(target)
        if isinstance(result, dict):
            return result.get("patterns", [])
        return result if isinstance(result, list) else []

    async def architect_init(self, pattern: str = None) -> Dict:
        """Initialize Architect for active project."""
        return await self._agents.start_architect_init(
            self._projects.active_project, pattern
        )

    async def generate_ai_rules_for_pattern(self, pattern: str, project: str = None) -> Dict:
        """Generate AI rules for pattern."""
        target = project or self._projects.active_project
        return await self._agents.generate_ai_rules(
            target, pattern
        )

    async def get_ai_architecture_suggestions(self, project: str = None) -> Dict:
        """Get AI architecture suggestions."""
        target = project or self._projects.active_project
        return await self._agents.get_architect_suggestions(target)

    # ═══════════════════════════════════════════════════════════════════════
    # WARDEN COMMANDS (delegated to AgentManager)
    # ═════════════════════��═════════════════════════════════════════════════

    async def warden_scan(self, project: str = None) -> Dict:
        """Execute Warden scan."""
        target = project or self._projects.active_project
        return await self._agents.send_warden_command("scan", target)

    async def warden_predict_critical(self, project: str = None) -> Dict:
        """Predict critical files with Warden."""
        target = project or self._projects.active_project
        return await self._agents.send_warden_command("predict-critical", target)

    async def warden_risk_assess(self, project: str = None) -> Dict:
        """Assess project risks with Warden."""
        target = project or self._projects.active_project
        return await self._agents.send_warden_command("risk-assess", target)

    async def warden_churn_report(self, project: str = None) -> Dict:
        """Generate churn report with Warden."""
        target = project or self._projects.active_project
        return await self._agents.send_warden_command("churn-report", target)


# Global instance
orchestrator = Orchestrator()
