"""Event Router - Routes events to appropriate handlers."""

import logging
from typing import Dict, List, Any, Optional

from app.decision_engine import DecisionEngine, DecisionAction
from app.models import AgentEvent
from app.dispatcher import notify
from app.sockets import emit_agent_event
from app.context_db import ContextDB

logger = logging.getLogger("cerebro.events")


class EventRouter:
    """
    Routes agent events to appropriate handlers based on DecisionEngine evaluation.

    Responsibilities:
    - Evaluate events with DecisionEngine
    - Route to notification handlers
    - Chain events to other agents
    - Block critical actions
    """

    def __init__(self, decision_engine: DecisionEngine, context_db: ContextDB):
        self.decision_engine = decision_engine
        self.context_db = context_db
        self._handlers: Dict[str, Any] = {}

    async def route(self, event: AgentEvent) -> Dict[str, Any]:
        """
        Route an event to appropriate handlers.

        Returns:
            Dict with actions taken
        """
        # Emit to dashboard first
        await emit_agent_event(event.model_dump(mode="json"))

        logger.info(
            f"📥 [{event.source.upper()}] type={event.type} "
            f"severity={event.severity} id={event.id}"
        )

        result = {"event_id": event.id, "actions": []}
        
        # ── EVENTOS DE RESULTADO AUTÓMATA (AUTOFIX CALLBACK) Y MANUAL ──
        if event.type in (
            "autofix_completed", "autofix_failed",
            "feature_completed", "feature_failed",
            "bugfix_completed", "bugfix_failed"
        ):
            from app.autofix_client import get_autofix_client
            client = get_autofix_client()
            status, branch, target = await client.process_autofix_result(event.model_dump(mode="json"))
            
            if status == "success" and branch:
                # Merge automático
                try:
                    import subprocess
                    import os
                    from app.config import get_settings
                    project_root = getattr(self, '_active_project', None)
                    workspace = get_settings().workspace_root
                    
                    target_abs = target if target and os.path.isabs(target) else os.path.join(workspace, target) if target else workspace
                    
                    if project_root and project_root != "default":
                        repo_dir = os.path.join(workspace, project_root)
                    elif target and os.path.isdir(target_abs):
                        repo_dir = target_abs
                    elif target:
                        repo_dir = os.path.dirname(target_abs)
                    else:
                        repo_dir = workspace

                    if not os.path.isdir(repo_dir):
                        repo_dir = workspace
                        
                    logger.info(f"🌿 Iniciando MERGE AUTOMÁTICO de {branch} en {repo_dir}")
                    
                    # Intentar volver a main o master (ya que checkout - puede fallar si Cerebro no hizo checkout antes)
                    checkout_res = subprocess.run(
                        ["git", "checkout", "main"],
                        cwd=repo_dir, capture_output=True, text=True
                    )
                    if checkout_res.returncode != 0:
                        subprocess.run(
                            ["git", "checkout", "master"],
                            cwd=repo_dir, capture_output=True, text=True
                        )
                    
                    # Merge from new branch
                    res = subprocess.run(
                        ["git", "merge", branch, "-m", "Merge automático de Autofix proactivo"],
                        cwd=repo_dir, capture_output=True, text=True
                    )
                    
                    if res.returncode == 0:
                        logger.info(f"✅ Merge automático exitoso de la rama {branch}")
                        await notify(f"✨ **Integración de {event.type.split('_')[0]} aplicada y mergeada:** `{target}`", level="info")
                        result["actions"].append({"action": "auto_merge", "status": "success", "branch": branch})
                        
                        # Si es interactivo (feature/bugfix), enviar al Tribunal de Agentes en paralelo
                        if not event.type.startswith("autofix"):
                            import asyncio
                            asyncio.create_task(self._trigger_tribunal(repo_dir, target, target_abs))

                    else:
                        logger.warning(f"⚠️ Fallo al hacer merge automático: {res.stderr}")
                        await notify(f"⚠️ **Código validado pero el merge automático falló** para `{target}`. Requiere revisión manual de rama `{branch}`", level="warning")
                        result["actions"].append({"action": "auto_merge", "status": "failed", "error": res.stderr})
                        
                except Exception as merge_err:
                    logger.error(f"❌ Error durante el merge automático: {merge_err}")
            elif status == "pending_review":
                # Notificación para revisión
                await notify(f"⏳ **La ejecución requiere revisión humana** para `{target}`. Hay sugerencias pendientes.", level="warning")
            elif status == "failed":
                await notify(f"❌ **La ejecución falló** para `{target}`.", level="error")
                
            return result

    async def _trigger_tribunal(self, repo_dir: str, target: str, target_abs: str):
        """Dispara de manera encadenada a Sentinel, Architect y Warden tras la implementación de un requerimiento manual."""
        from app.dispatcher import send_raw_command
        from app.sockets import emit_agent_event
        import uuid
        import asyncio

        await emit_agent_event({
            "source": "cerebro", "type": "tribunal_started", "severity": "info",
            "payload": {"message": f"Iniciando TRIBUNAL (validación multi-agente) para código generado en {target}"}
        })
        logger.info(f"⚖️ Iniciando TRIBUNAL multi-agente para {target}")

        try: 
            # 1. Sentinel
            logger.info("⚖️ -> Ejecutando Sentinel...")
            await send_raw_command("sentinel", {
                "action": "pro", "subcommand": "check",
                "target": target_abs or repo_dir,
                "request_id": f"sentinel-{uuid.uuid4().hex[:8]}"
            })
            await asyncio.sleep(6)  # Darle margen a Sentinel

            # 2. Architect
            logger.info("⚖️ -> Ejecutando Architect...")
            await send_raw_command("architect", {
                "action": "pro", "subcommand": "review",
                "target": target_abs or repo_dir,
                "request_id": f"architect-{uuid.uuid4().hex[:8]}"
            })
            await asyncio.sleep(6)

            # 3. Warden
            logger.info("⚖️ -> Ejecutando Warden...")
            await send_raw_command("warden", {
                "action": "pro", "subcommand": "scan",
                "target": target_abs or repo_dir,
                "request_id": f"warden-{uuid.uuid4().hex[:8]}"
            })
            
            await emit_agent_event({
                "source": "cerebro", "type": "tribunal_completed", "severity": "success",
                "payload": {"message": f"Tribunal desplegado correctamente sobre {target}"}
            })
            logger.info(f"✅ TRIBUNAL lanzado exitosamente para {target}")

        except Exception as e:
            logger.error(f"❌ Error lanzando el tribunal: {e}")

    # ── RUTEO HABITUAL ── 
    async def _evaluate_standard(self, event: AgentEvent) -> Dict[str, Any]:
        """Aisla la lógica de enrutamiento que dependía de 'event' directamente en 'route()' para cumplir con el esquema."""
        result = {"event_id": event.id, "actions": []}
        context = {"project": getattr(self, '_active_project', None)}
        decision = await self.decision_engine.evaluate(
            event.model_dump(mode="json"),
            context
        )

        logger.info(
            f"🧠 Decision: actions={[a.value for a in decision.actions]}, "
            f"reason={decision.reason}"
        )

        # Record in ContextDB
        await self._record_event(event, decision)

        # Execute actions
        for action in decision.actions:
            handler = self._get_handler(action)
            if handler:
                try:
                    action_result = await handler(event, decision)
                    result["actions"].append(action_result)
                except Exception as e:
                    logger.error(f"Handler error for {action}: {e}")

        # Handle interactions regardless of decision
        if event.type == "interaction_required":
            interaction_result = await self._handle_interaction(event)
            result["actions"].append(interaction_result)

        return result

    def _get_handler(self, action: DecisionAction):
        """Get handler for a decision action."""
        handlers = {
            DecisionAction.NOTIFY: self._handle_notify,
            DecisionAction.CHAIN: self._handle_chain,
            DecisionAction.BLOCK: self._handle_block,
            DecisionAction.IGNORE: self._handle_ignore,
            DecisionAction.ESCALATE: self._handle_escalate,
            DecisionAction.AUTOFIX: self._handle_autofix,   # NUEVO
        }
        return handlers.get(action)

    async def _record_event(self, event: AgentEvent, decision):
        """Record event in ContextDB."""
        if not self.context_db:
            return

        file_path = event.payload.get("file") if event.payload else None
        if file_path:
            self.context_db.record_event(
                file_path=file_path,
                event_type=event.type,
                source=event.source,
                severity=event.severity.value,
                payload=event.payload,
                decision_actions=[a.value for a in decision.actions],
            )

    async def _handle_notify(self, event: AgentEvent, decision) -> Dict:
        """Send notification to user."""
        level = decision.notification_level or "info"
        message = self._build_message(event)

        sent = await notify(message, level=level, source=event.source)

        return {"action": "notify", "level": level, "delivered": sent}

    async def _handle_chain(self, event: AgentEvent, decision) -> Dict:
        """Chain event to other agents or start analysis pipeline."""
        from app.dispatcher import send_command
        from app.models import OrchestratorCommand

        targets = decision.target_agents or []
        if not targets:
            return {"action": "chain", "status": "skipped", "reason": "No targets"}

        # Si el evento es de Sentinel con un archivo, iniciar pipeline de análisis
        file_path = event.payload.get("file") if event.payload else None
        if event.source == "sentinel" and file_path:
            logger.info(f"🔄 Iniciando pipeline de análisis para: {file_path}")
            try:
                # Importar aquí para evitar circular imports
                from app.orchestrator import orchestrator
                # Asegurar que el pipeline tenga el proyecto activo
                if orchestrator.active_project:
                    orchestrator._pipeline.set_active_project(orchestrator.active_project)
                pipeline_result = await orchestrator.start_pipeline_analysis(
                    file_path=file_path,
                    agents=targets
                )
                return {
                    "action": "pipeline_started",
                    "file": file_path,
                    "agents": targets,
                    "pipeline_result": pipeline_result
                }
            except Exception as e:
                logger.error(f"Error iniciando pipeline: {e}")
                return {"action": "pipeline_error", "error": str(e)}

        # Fallback: chain tradicional con comandos individuales
        results = []
        for agent in targets:
            if agent == event.source:
                continue

            logger.info(f"🔗 Chaining to {agent}: {event.type}")

            command = OrchestratorCommand(
                action="analyze",
                target=file_path,
                options={
                    "original_event": event.model_dump(mode="json"),
                    "triggered_by": event.source,
                }
            )

            try:
                ack = await send_command(agent, command)
                results.append({"agent": agent, "status": "sent"})

                await emit_agent_event({
                    "source": "cerebro",
                    "type": "event_chained",
                    "severity": "info",
                    "payload": {
                        "from": event.source,
                        "to": agent,
                        "original_type": event.type,
                    }
                })
            except Exception as e:
                logger.error(f"Chain error to {agent}: {e}")
                results.append({"agent": agent, "error": str(e)})

        return {"action": "chain", "targets": targets, "results": results}

    async def _handle_block(self, event: AgentEvent, decision) -> Dict:
        """Block critical action and request approval."""
        logger.warning(f"🚫 Blocked by {event.source}: {event.type}")

        # TODO: Implement ChangeManager integration for approval workflow
        # For now, just notify
        message = f"🚫 **Blocked:** {self._build_message(event)}"
        await notify(message, level="critical", source=event.source)

        await emit_agent_event({
            "source": "cerebro",
            "type": "action_blocked",
            "severity": "critical",
            "payload": {
                "blocked_by": event.source,
                "reason": event.payload.get("message", "Critical action blocked"),
                "requires_approval": True,
            }
        })

        return {"action": "block", "status": "pending_approval"}

    async def _handle_ignore(self, event: AgentEvent, decision=None) -> Dict:
        """Ignore event (logged only)."""
        return {"action": "ignore", "status": "logged"}

    async def _handle_escalate(self, event: AgentEvent, decision=None) -> Dict:
        """Escalate critical event."""
        logger.critical(f"🔺 Escalating: {event.type} from {event.source}")

        message = self._build_message(event)
        await notify(message, level="critical", source=event.source)

        # Record pattern
        if self.context_db:
            file_path = event.payload.get("file") if event.payload else None
            if file_path:
                self.context_db.record_pattern(
                    pattern_type="escalated_event",
                    description=f"Escalated: {event.type}",
                    severity="critical",
                    file_path=file_path,
                    metadata={"source": event.source, "event_id": event.id},
                )

        return {"action": "escalate", "status": "escalated"}

    async def _handle_autofix(self, event: AgentEvent, decision) -> Dict:
        """
        Dispara un autofix automático vía AutofixClient.
        El AutofixClient se comunica con Executor → Aider → Validación.
        Solo Cerebro habla con Executor (principio arquitectónico).
        """
        from app.autofix_client import get_autofix_client
        from app.proactive_scheduler import get_proactive_scheduler

        # Verificar que autofix esté habilitado en la config proactiva
        scheduler = get_proactive_scheduler()
        project = getattr(self, '_active_project', None) or ""
        config = scheduler.get_config(project) if project else {}
        autofix_cfg = config.get("autofix", {})

        if not autofix_cfg.get("enabled", True):
            logger.info("🚫 Autofix deshabilitado en configuración — ignorando")
            return {"action": "autofix", "status": "disabled"}

        # Verificar confianza vs. threshold
        night_mode = scheduler.is_night_mode_active(config)
        if not self.decision_engine.should_autofix(event.model_dump(mode="json"), night_mode):
            logger.info("🚫 should_autofix=False — no se dispara autofix")
            return {"action": "autofix", "status": "skipped", "reason": "below_threshold"}

        batch_id = event.payload.get("batch_id") if event.payload else None
        logger.info(f"🔧 Disparando autofix para evento {event.type} (batch={batch_id})")

        try:
            client = get_autofix_client()
            result = await client.trigger_autofix(
                event=event.model_dump(mode="json"),
                batch_id=batch_id,
            )
            logger.info(f"✅ Autofix completado: {result.get('status')} | branch={result.get('branch')}")
            return {"action": "autofix", **result}
        except Exception as exc:
            logger.error(f"❌ Error en autofix handler: {exc}")
            return {"action": "autofix", "status": "error", "error": str(exc)}

    async def _handle_interaction(self, event: AgentEvent) -> Dict:
        """Handle interaction required event."""
        from app.config import get_settings
        import httpx

        settings = get_settings()

        prompt_id = event.payload.get("prompt_id")
        message = event.payload.get("message", "Confirmation required")

        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{settings.notifier_url}/ask-interaction",
                    json={
                        "message": message,
                        "prompt_id": prompt_id,
                        "source": event.source,
                    },
                    timeout=30.0
                )
            return {"action": "interaction", "status": "sent", "prompt_id": prompt_id}
        except Exception as e:
            logger.error(f"Interaction error: {e}")
            return {"action": "interaction", "status": "error", "error": str(e)}

    def _build_message(self, event: AgentEvent) -> str:
        """Build human-readable message from event."""
        lines = [f"*[{event.source.upper()}]* — `{event.type}`"]

        payload = event.payload or {}
        if "file" in payload:
            lines.append(f"Archivo: `{payload['file']}`")
        if "message" in payload:
            lines.append(f"Detalle: {payload['message']}")
        if "suggestion" in payload:
            lines.append(f"Sugerencia: _{payload['suggestion']}_")
        if "finding" in payload:
            lines.append(f"Hallazgo: {payload['finding']}")

        return "\n".join(lines)
