import logging
import os
import json
from typing import List, Any, Dict, Optional
from app.models import AgentEvent, Severity, OrchestratorCommand
from app.dispatcher import send_command, notify
from app.decision_engine import DecisionEngine, DecisionAction
from app.context_db import ContextDB, get_context_db
from app.change_manager import ChangeManager, get_change_manager

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

    Usa DecisionEngine para evaluación centralizada y ContextDB para contexto.
    """

    def __init__(self):
        self.active_project = None
        self.monitored_project = None # Rastreo de éxito de monitorización
        self.workspace_root = settings.workspace_root
        self.decision_engine = DecisionEngine(architect_url=settings.architect_url)
        self.context_db = get_context_db()
        self.decision_engine.set_context_db(self.context_db)
        self.change_manager = get_change_manager()
        self.change_manager.set_orchestrator(self)

    async def handle_event(self, event: AgentEvent) -> dict:
        from app.sockets import emit_agent_event
        # Emitir a través de Socket.IO para el Dashboard
        await emit_agent_event(event.model_dump(mode="json"))

        logger.info(
            f"📥 [{event.source.upper()}] type={event.type} "
            f"severity={event.severity} id={event.id}"
        )

        result = {"event_id": event.id, "actions": []}

        # ── Lógica de decisión centralizada con DecisionEngine ─────────────────

        # 1. Evaluar evento con DecisionEngine
        event_dict = event.model_dump(mode="json")
        context = {"project": self.active_project}
        decision = await self.decision_engine.evaluate(event_dict, context)

        logger.info(f"🧠 Decisión: actions={[a.value for a in decision.actions]}, "
                    f"targets={decision.target_agents}, reason={decision.reason}")

        # 2. Registrar evento en ContextDB (si hay file_path)
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

        # 3. Ejecutar acciones decididas
        from app.routes.config import load_config
        config = load_config()
        
        # Si hay un bloqueo crítico y el auto-fix está encendido, 
        # interceptamos la decisión estática de encadenar para que actúe de inmediato en lugar de derivar.
        skip_chain = False
        if DecisionAction.BLOCK in decision.actions and config.get("auto_fix_enabled"):
            skip_chain = True

        for action in decision.actions:
            if action == DecisionAction.NOTIFY:
                result["actions"].append(await self._handle_notification(event, decision.notification_level))
            elif action == DecisionAction.CHAIN:
                if skip_chain:
                    logger.info("⚙️ Auto-Fix interceptó el evento crítico: Cancelando Chain a otros agentes")
                    continue
                result["actions"].append(await self._handle_chain(event, decision.target_agents))
            elif action == DecisionAction.BLOCK:
                result["actions"].append(await self._handle_block(event, decision))
            elif action == DecisionAction.IGNORE:
                result["actions"].append(await self._handle_info(event))
            elif action == DecisionAction.ESCALATE:
                result["actions"].append(await self._handle_escalate(event))

        # 4. Manejar interacción si es requerida (independiente de decisión)
        if event.type == "interaction_required":
            result["actions"].append(await self._handle_interaction(event))

        return result

    # ── Handlers por severidad ────────────────────────────────────────────────
    # ── (Mantenidos para compatibilidad, pero ahora se usan los nuevos handlers)

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
        """
        WARNING: notificar al usuario.
        Si es análisis de Sentinel, procesar violaciones como cambios pendientes.
        """
        logger.warning(f"⚠️ WARNING desde {event.source} - type={event.type}")

        # Si es análisis de Sentinel con problemas, procesarlos
        if event.source == "sentinel" and event.type == "analysis_completed":
            logger.info("🔍 Procesando análisis de Sentinel (warning)...")
            result = await self._process_sentinel_analysis(event)
            logger.info(f"✅ Análisis procesado: {result}")
            return result

        actions = []

        # NOTA: Ya NO encadenamos Architect automáticamente para file_change
        # Sentinel hace el análisis IA directamente en modo monitor/serve
        # El encadenamiento sería redundante y genera eventos duplicados

        message = self._build_message(event)
        sent = await notify(message, level="warning", source=event.source)
        actions.append({"action": "notify_warning", "delivered": sent})

        return {"action": "handle_warning", "steps": actions}

    async def _handle_info(self, event: AgentEvent) -> dict:
        """
        INFO: normalmente solo loggear, PERO si es analysis_completed con violaciones,
        procesarlas como cambios pendientes.
        """
        logger.info(f"ℹ️ INFO desde {event.source} — {event.type}")

        # Si es análisis de Sentinel con resultados, extraer violaciones
        if event.source == "sentinel" and event.type == "analysis_completed":
            return await self._process_sentinel_analysis(event)

        return {"action": "logged_only"}

    # ── Nuevos handlers basados en DecisionEngine ─────────────────────────────

    async def _handle_notification(self, event: AgentEvent, level: str) -> dict:
        """Envía notificación al usuario con el nivel especificado."""
        level = level or "info"
        logger.log(
            logging.WARNING if level == "warning" else logging.ERROR if level in ["error", "critical"] else logging.INFO,
            f"📬 Notificación {level} desde {event.source}"
        )

        message = self._build_message(event)
        sent = await notify(message, level=level, source=event.source)

        return {"action": f"notify_{level}", "delivered": sent}

    async def _handle_chain(self, event: AgentEvent, target_agents: List[str]) -> dict:
        """
        Encadena eventos a otros agentes para análisis adicional.
        Cada agente recibe el evento original y decide cómo actuar.
        """
        if not target_agents:
            return {"action": "chain", "status": "skipped", "reason": "No targets"}

        results = []
        for agent in target_agents:
            if agent == event.source:
                continue  # No enviar al mismo agente que originó

            logger.info(f"🔗 Encadenando a {agent}: {event.type}")

            # Construir comando para el agente
            command = OrchestratorCommand(
                action="analyze",
                target=event.payload.get("file") if event.payload else None,
                options={
                    "original_event": event.model_dump(mode="json"),
                    "triggered_by": event.source,
                }
            )

            try:
                ack = await send_command(agent, command)
                results.append({"agent": agent, "ack": ack})

                # Emitir evento de chain para el dashboard
                from app.sockets import emit_agent_event
                await emit_agent_event({
                    "source": "cerebro",
                    "type": "event_chained",
                    "severity": "info",
                    "payload": {
                        "from_agent": event.source,
                        "to_agent": agent,
                        "original_type": event.type,
                    }
                })
            except Exception as e:
                logger.error(f"❌ Error encadenando a {agent}: {e}")
                results.append({"agent": agent, "error": str(e)})

        return {"action": "chain", "targets": target_agents, "results": results}

    async def _handle_block(self, event: AgentEvent, decision=None) -> dict:
        """
        Bloquea una acción potencialmente peligrosa y la agrega a ChangeManager.
        Se usa para eventos críticos que requieren intervención manual.
        """
        logger.warning(f"🚫 BLOQUEO activado por {event.source}: {event.type}")

        # Agregar a ChangeManager para aprobación humana
        file_path = (event.payload or {}).get("file") or (event.payload or {}).get("target") or "."
        description = (event.payload or {}).get("message") or (event.payload or {}).get("finding") or f"{event.type} detectado"
        recommendation = (event.payload or {}).get("recommendation") or (event.payload or {}).get("suggestion")

        change = await self.change_manager.add_change(
            event_id=event.id,
            file_path=file_path,
            description=description,
            severity=event.severity.value,
            recommendation=recommendation,
            metadata=event.payload,
        )

        # Emitir evento de bloqueo con info del cambio pendiente
        from app.sockets import emit_agent_event
        await emit_agent_event({
            "source": "cerebro",
            "type": "action_blocked",
            "severity": "critical",
            "payload": {
                "blocked_by": event.source,
                "reason": description,
                "file": file_path,
                "change_id": change.id,
                "requires_approval": True,
            }
        })

        # --- AUTO-FIX ENGINE INJECTION ---
        import asyncio
        from app.routes.config import load_config
        config = load_config()

        if config.get("auto_fix_enabled"):
            # LEARNING: Si ya falló antes,DecisionEngine marcas suppress_autofix en metadata
            suppress = False
            if decision and getattr(decision, 'metadata', {}).get("suppress_autofix"):
                logger.warning(f"🛑 (Learning) Cancelando disparo de Auto-Fix automatizado en '{file_path}' porque ya falló antes.")
                suppress = True

            if not suppress:
                logger.info("⚙️ Auto-Fix habilitado. Evaluando delegación a Executor...")
                
                # ── AUTO-FIX RETRY ENGINE ────────────────────────────────────────────
                async def trigger_autofix():
                    # Tiempo de espera (Human-In-The-Loop)
                    wait_time = 5 # Default para pruebas
                    if decision and getattr(decision, 'metadata', {}).get("previously_fixed_successfully"):
                        logger.info("⚡ (Learning) Archivo conocido como confiable para Auto-Fix. Reduciendo aprobación a 1s.")
                        wait_time = 1

                    if config.get("require_approval_critical"):
                        timeout_mins = config.get("notifier_timeout_mins", 30)
                        logger.info(f"⏳ Esperando {timeout_mins} mins para aprobación humana antes de autofix...")
                        await asyncio.sleep(wait_time)  # Simbólico para pruebas, en prod: * 60
                
                from app.dispatcher import send_command
                from app.models import OrchestratorCommand
                from app.sockets import emit_agent_event

                max_retries = config.get("auto_fix_max_retries", 3)
                branch_prefix = config.get("isolation_branch_prefix", "skrymir-fix/")
                base_instruction = f"""You are the Skrymir Executor Auto-Fix Agent.
An upstream analysis agent ({event.source.upper()}) has audited the codebase and detected issues.

=== AGENT ANALYSIS REPORT ===
{description}

=== EXPLICIT ACTION/RECOMMENDATION ===
{recommendation}

YOUR TASK:
Implement the exact code changes to resolve the identified issues.
Focus strictly on the mentioned problems. Do not refactor unrelated code.
If the code is "Safe" or no critical action is required, do nothing and reply with "No changes needed".
"""
                accumulated_errors = []  # Errores de build acumulados entre intentos
                last_result = {}
                fix_validated = False

                await notify(
                    f"🤖 **Auto-Fix Iniciado** (máx. {max_retries} intentos)\n"
                    f"Archivo: `{file_path}` — Validation Gate activado.",
                    level="info", source="cerebro"
                )

                for attempt in range(1, max_retries + 1):
                    logger.warning(f"🔁 Auto-Fix intento {attempt}/{max_retries} para '{file_path}'")

                    # En reintentos, enriquecer el prompt con errores acumulados
                    if accumulated_errors:
                        retry_context = "\n".join([
                            f"--- Attempt {i+1} build error ---\n{err}"
                            for i, err in enumerate(accumulated_errors)
                        ])
                        instruction = f"""{base_instruction}

=== PREVIOUS FIX ATTEMPTS FAILED ===
You already tried to fix this {len(accumulated_errors)} time(s) but the build broke each time.
Here are the accumulated build errors from previous attempts. Learn from them and try a different approach:

{retry_context}

CRITICAL: The build MUST pass after your changes. Prioritize fixing the build errors above.
"""
                    else:
                        instruction = base_instruction

                    fix_cmd = OrchestratorCommand(
                        action="autofix",
                        target=file_path,
                        options={
                            "instruction": instruction,
                            "branch_prefix": branch_prefix,
                            "provider": config.get("auto_fix_provider", "ollama"),
                            "model": config.get("auto_fix_model", "qwen3:8b"),
                            "workspace_root": self.workspace_root
                        }
                    )

                    try:
                        ack = await send_command("ejecutor", fix_cmd)
                        fix_result = {}
                        if ack and isinstance(ack, dict):
                            fix_result = ack.get("data", {}).get("result", {}) or {}

                        last_result = fix_result
                        fix_validated = fix_result.get("fix_validated")
                        branch = fix_result.get("branch", f"{branch_prefix}?")
                        build_tool = fix_result.get("build_tool", "desconocido")
                        build_output = (fix_result.get("build_output") or "")[:400]

                        # Capturar info de archivos modificados (incluso si falla)
                        modified_files = fix_result.get("modified_files", [])
                        git_diff_stat = fix_result.get("git_diff_stat", "")
                        files_count = fix_result.get("files_count", 0)

                        if fix_validated is True:
                            # ✅ ¡ÉXITO! Salir del loop
                            logger.info(f"✅ Fix validado en intento {attempt}/{max_retries} | rama: {branch}")

                            self.context_db.record_pattern(
                                pattern_type="autofix_success",
                                description=f"Fix de {event.source} validado en intento {attempt}/{max_retries}",
                                severity="info",
                                file_path=file_path,
                                metadata={"source": event.source, "branch": branch,
                                          "build_tool": build_tool, "attempts": attempt}
                            )

                            # Notificar a Warden para enfriar risk_score
                            try:
                                import httpx
                                from app.config import get_settings
                                settings = get_settings()
                                warden_url = getattr(settings, "warden_url", "http://localhost:8001")
                                async with httpx.AsyncClient() as client:
                                    await client.post(
                                        f"{warden_url}/api/memory/fix-applied",
                                        json={"file_path": file_path, "branch": branch, "validated": True},
                                        timeout=5.0
                                    )
                            except Exception as e:
                                logger.warning(f"No se pudo notificar a Warden del fix: {e}")

                            # Construir resumen de archivos modificados
                            files_summary = ""
                            if modified_files:
                                files_summary = "\n".join([f"• `{f['path']}` ({f['status']})" for f in modified_files[:5]])
                                if len(modified_files) > 5:
                                    files_summary += f"\n• ... y {len(modified_files) - 5} más"

                            await notify(
                                f"✅ **Auto-Fix Validado** (intento {attempt}/{max_retries})\n"
                                f"Archivo: `{file_path}`\n"
                                f"Rama: `{branch}`\n"
                                f"Build: `{build_tool}` ✅\n"
                                f"Archivos modificados: {files_count}\n\n"
                                f"{files_summary}\n\n"
                                f"Listo para merge:\n`git merge {branch}`",
                                level="info", source="cerebro"
                            )

                            await emit_agent_event({
                                "source": "cerebro",
                                "type": "autofix_success",
                                "severity": "info",
                                "payload": {
                                    "file": file_path,
                                    "branch": branch,
                                    "attempts": attempt,
                                    "build_tool": build_tool,
                                    "modified_files": modified_files,
                                    "files_count": files_count,
                                    "git_diff_stat": git_diff_stat,
                                }
                            })
                            return  # ← Éxito, salir

                        elif fix_validated is False:
                            # ❌ Build falló, acumular error para el próximo intento
                            logger.warning(f"⚠️ Intento {attempt} falló build. Acumulando error para reintento...")
                            accumulated_errors.append(build_output)

                            # Info de archivos modificados en este intento
                            attempt_files = fix_result.get("modified_files", [])
                            attempt_diff = fix_result.get("git_diff_stat", "")[:500]

                            if attempt < max_retries:
                                files_msg = ""
                                if attempt_files:
                                    files_msg = f"\nArchivos modificados: {len(attempt_files)}\n"
                                    for f in attempt_files[:3]:
                                        files_msg += f"• `{f['path']}`\n"

                                await notify(
                                    f"🔄 **Auto-Fix Reintentando** ({attempt}/{max_retries})\n"
                                    f"Build quebrado en intento {attempt}. Aider aprenderá del error y reintentará.{files_msg}\n"
                                    f"Error: `{build_output[:200]}`",
                                    level="warning", source="cerebro"
                                )
                        else:
                            # Build tool no detectado, no hay forma de validar → éxito parcial
                            logger.info(f"⚠️ Intento {attempt}: sin build tool detectable, saliendo.")
                            break

                    except Exception as ex:
                        logger.error(f"Error en intento {attempt}: {ex}")
                        accumulated_errors.append(str(ex))

                # ── MAX RETRIES AGOTADOS ──────────────────────────────────────────
                if fix_validated is not True:
                    logger.error(f"🆘 Auto-Fix AGOTADO tras {max_retries} intentos en '{file_path}'")

                    # Construir info de archivos que se intentaron modificar
                    last_modified_files = last_result.get("modified_files", []) if last_result else []
                    last_diff_stat = last_result.get("git_diff_stat", "") if last_result else ""

                    self.context_db.record_pattern(
                        pattern_type="autofix_exhausted",
                        description=f"Auto-Fix agotó {max_retries} reintentos sin éxito",
                        severity="critical",
                        file_path=file_path,
                        metadata={
                            "source": event.source,
                            "attempts": max_retries,
                            "accumulated_errors": accumulated_errors,
                            "modified_files_attempted": last_modified_files,
                            "git_diff_stat": last_diff_stat,
                        }
                    )

                    # Emitir evento especial al Dashboard con info de archivos
                    await emit_agent_event({
                        "source": "cerebro",
                        "type": "autofix_exhausted",
                        "severity": "critical",
                        "payload": {
                            "file": file_path,
                            "attempts": max_retries,
                            "reason": "Build falló en todos los intentos. Intervención humana requerida.",
                            "last_build_error": accumulated_errors[-1][:300] if accumulated_errors else "N/A",
                            "requires_human": True,
                            # Info de archivos intentados
                            "modified_files": last_modified_files,
                            "files_count": len(last_modified_files),
                            "git_diff_stat": last_diff_stat,
                        }
                    })

                    # Escalación a Telegram con info de archivos
                    files_attempted_msg = ""
                    if last_modified_files:
                        files_attempted_msg = "\n**Archivos que se intentaron modificar:**\n"
                        for f in last_modified_files[:5]:
                            files_attempted_msg += f"• `{f['path']}`\n"
                        if len(last_modified_files) > 5:
                            files_attempted_msg += f"• ... y {len(last_modified_files) - 5} más\n"

                    await notify(
                        f"🆘 **Auto-Fix Agotado** — Intervención Humana Requerida\n\n"
                        f"El agente no pudo resolver el problema en `{file_path}` "
                        f"tras **{max_retries} intentos**.\n\n"
                        f"{files_attempted_msg}\n"
                        f"**Último error de build:**\n```\n{accumulated_errors[-1][:400] if accumulated_errors else 'N/A'}\n```\n\n"
                        f"**Diff de cambios intentados:**\n```\n{last_diff_stat[:500] if last_diff_stat else 'N/A'}\n```\n\n"
                        f"Acción: Revisar la rama `{branch}` y corregir manualmente.",
                        level="critical", source="cerebro"
                    )

            asyncio.create_task(trigger_autofix())



        return {"action": "block", "status": "pending_approval", "change_id": change.id, "autofix_triggered": config.get("auto_fix_enabled", False)}

    async def _handle_escalate(self, event: AgentEvent) -> dict:
        """
        Escala un evento crítico para atención inmediata.
        Combina notificación crítica + bloqueo + registro especial.
        """
        logger.critical(f"🔺 ESCALANDO evento crítico: {event.type} desde {event.source}")

        # Notificación crítica
        message = self._build_message(event)
        await notify(message, level="critical", source=event.source)

        # Registrar patrón en ContextDB
        file_path = event.payload.get("file") if event.payload else None
        if file_path:
            self.context_db.record_pattern(
                pattern_type="escalated_event",
                description=f"Evento {event.type} escalado por criticidad",
                severity="critical",
                file_path=file_path,
                metadata={"source": event.source, "event_id": event.id},
            )

        return {"action": "escalate", "status": "escalated", "event_id": event.id}

    async def _handle_interaction(self, event: AgentEvent) -> dict:
        """Pide una respuesta al usuario via Notificador"""
        logger.info(f"❓ INTERACTION requerida por {event.source}")

        prompt_id = event.payload.get("prompt_id")
        message = event.payload.get("message", "Confirmación requerida")

        from app.config import get_settings
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

    async def _process_sentinel_analysis(self, event: AgentEvent) -> dict:
        """
        Procesa resultados de análisis de Sentinel.
        Extrae violaciones/problemas individuales y los agrega como cambios pendientes.

        El payload puede venir en diferentes formatos:
        - { findings: "texto con problemas..." }
        - { result: { findings: [...] } }
        - { violations: [...] }
        """
        logger.info(f"🔍 Procesando análisis de Sentinel: {event.type}")

        payload = event.payload or {}
        file_path = payload.get("file", "unknown")

        # Extraer findings (puede ser string o array)
        findings = payload.get("findings") or payload.get("result", {}).get("findings")

        if not findings:
            logger.debug("  ↳ Sin findings para procesar")
            return {"action": "logged_only"}

        # Si findings es un string, dividirlo por líneas/numeros
        if isinstance(findings, str):
            # Buscar patrones como "1. **Violación...**" o líneas separadas
            import re
            # Dividir por patrones de lista numerada o viñetas
            problemas = re.split(r'\n(?=\d+\.|\n\*|\n-)', findings)
            problemas = [p.strip() for p in problemas if p.strip() and len(p.strip()) > 20]
        elif isinstance(findings, list):
            problemas = findings
        else:
            logger.warning(f"  ↳ Formato de findings desconocido: {type(findings)}")
            return {"action": "logged_only"}

        if not problemas:
            logger.debug("  ↳ No se extrajeron problemas del análisis")
            return {"action": "logged_only"}

        logger.info(f"  ↳ {len(problemas)} problemas detectados")

        # Agregar cada problema como cambio pendiente
        changes_added = []
        for i, problema in enumerate(problemas[:10]):  # Límite de 10
            # Extraer descripción (quitar números y markdown)
            descripcion = str(problema).replace(f"{i+1}.", "").replace("**", "").strip()

            change = await self.change_manager.add_change(
                event_id=event.id,
                file_path=file_path,
                description=descripcion[:200],  # Máximo 200 chars
                severity="warning",  # Sentinel ya filtró, son warning
                recommendation=f"Revisar y corregir: {descripcion[:100]}",
                metadata={
                    "source": "sentinel_analysis",
                    "full_finding": str(problema),
                    "index": i,
                },
            )
            changes_added.append(change.id)

        logger.info(f"✅ {len(changes_added)} cambios agregados a ChangeManager")

        # Notificar al usuario
        from app.dispatcher import notify
        message = f"🔍 **Sentinel detectó {len(changes_added)} problemas** en `{file_path}`\n\n"

        for i, problema in enumerate(problemas[:5]):
            desc = str(problema).replace(f"{i+1}.", "").replace("**", "")[:150]
            message += f"{i+1}. {desc}...\n" if len(str(problema)) > 150 else f"{i+1}. {desc}\n"

        if len(problemas) > 5:
            message += f"\n_...y {len(problemas) - 5} más_\n"

        message += "\n**Revisa el panel de cambios para aprobar/rechazar correcciones.**"

        await notify(message, level="warning", source="cerebro")

        return {
            "action": "process_analysis",
            "changes_added": len(changes_added),
            "change_ids": changes_added,
        }

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
            from app.config import get_settings
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

        # Si el monitor fue aceptado, registramos el éxito y emitimos sentinel_ready
        if ack.get("status") != "rejected":
            self.monitored_project = project_name
            logger.info(f"✅ Sentinel monitoreando exitosamente: {project_name}")

            # Emitir sentinel_ready la primera vez que responde exitosamente
            from app.sockets import emit_agent_event
            await emit_agent_event({
                "source": "sentinel",
                "type": "sentinel_ready",
                "severity": "info",
                "payload": {"ready": True, "message": "Sentinel está listo para monitoreo"}
            })
            logger.info("✅ Sentinel ready emitido desde orchestrator")
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

        logger.info(f"🔍 Validando proveedor: provider='{provider}', url='{url}', key_length={len(key) if key else 0}")

        # Normalizar URL (OpenAI/Claude suelen tener /v1/models o similar)
        endpoint = url.rstrip("/")
        if "ollama" in provider.lower():
            endpoint = f"{endpoint}/api/tags"
            logger.info(f"🦙 Detectado Ollama, endpoint: {endpoint}")
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
                logger.info(f"📡 Haciendo GET a: {endpoint}")
                resp = await client.get(endpoint, headers=headers)
                logger.info(f"📡 Respuesta status: {resp.status_code}")
                if resp.status_code == 200:
                    data = resp.json()
                    logger.info(f"📡 Respuesta data keys: {list(data.keys()) if isinstance(data, dict) else 'not a dict'}")
                    models = []
                    # Extraer modelos según formato
                    if "models" in data: # Ollama
                        models = [m["name"] for m in data["models"]]
                        logger.info(f"🦙 Modelos Ollama encontrados: {models}")
                    elif "data" in data: # OpenAI
                        models = [m["id"] for m in data["data"]]
                        logger.info(f"🤖 Modelos OpenAI encontrados: {len(models)}")
                    return {"ok": True, "models": models}
                else:
                    logger.error(f"❌ Error {resp.status_code}: {resp.text[:200]}")
                    return {"ok": False, "error": f"Error {resp.status_code}: {resp.text}"}
        except Exception as e:
            logger.exception(f"❌ Error validando proveedor: {e}")
            return {"ok": False, "error": str(e)}

    async def generate_ai_rules_for_pattern(self, pattern: str, project_path: str = None) -> dict:
        """
        Genera reglas de arquitectura usando IA.
        Delega a Architect (Rust) que es quien tiene la lógica de IA implementada.
        """
        import httpx

        target_project = project_path or self.active_project
        logger.info(f"🔍 generate_ai_rules_for_pattern: pattern={pattern}, project_path={project_path}, active_project={self.active_project}")

        if not target_project:
            logger.error("❌ No hay proyecto activo ni se especificó uno")
            return {"error": "No hay proyecto activo ni se especificó uno"}

        # Ruta completa del proyecto
        if os.path.isabs(target_project):
            full_path = target_project
        else:
            full_path = os.path.join(self.workspace_root, target_project)

        logger.info(f"🔍 Ruta completa del proyecto: {full_path}")

        if not os.path.exists(full_path):
            logger.error(f"❌ Proyecto no encontrado: {full_path}")
            return {"error": f"Proyecto no encontrado: {full_path}"}

        # Cargar configuración de IA del proyecto para verificar que existe
        ai_config_path = os.path.join(full_path, ".architect.ai.json")
        if not os.path.exists(ai_config_path):
            logger.error(f"❌ AI config no existe: {ai_config_path}")
            return {"error": "No hay configuración de IA (.architect.ai.json). Configura una en Architect Control Center."}

        # Llamar a Architect vía HTTP
        architect_url = settings.architect_url.rstrip('/')
        params = {"project": full_path}
        if pattern:
            params["pattern"] = pattern

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                logger.info(f"🔍 Llamando a Architect: {architect_url}/ai/rules?{params}")
                resp = await client.get(f"{architect_url}/ai/rules", params=params)
                resp.raise_for_status()
                result = resp.json()

                if result.get("ok"):
                    logger.info(f"✅ Architect generó {len(result.get('rules', []))} reglas")
                    return result
                else:
                    error_msg = result.get("error", "Error desconocido de Architect")
                    logger.error(f"❌ Architect retornó error: {error_msg}")
                    return {"error": error_msg}
        except httpx.TimeoutException:
            logger.error("⏰ Timeout consultando a Architect")
            return {"error": "Timeout: Architect tardó más de 120s en responder"}
        except Exception as e:
            logger.exception(f"❌ Error llamando a Architect: {e}")
            return {"error": f"Error comunicando con Architect: {str(e)}"}

    async def get_ai_architecture_suggestions(self, project_path: str = None) -> dict:
        """
        Obtiene sugerencias de arquitecturas desde IA.
        Delega a Architect (Rust) que es quien tiene la lógica de IA implementada.
        """
        import httpx

        target_project = project_path or self.active_project
        if not target_project:
            return {"error": "No hay proyecto activo"}

        full_path = os.path.join(self.workspace_root, target_project) if not os.path.isabs(target_project) else target_project

        logger.info(f"🔍 get_ai_architecture_suggestions: target_project={target_project}, full_path={full_path}")

        if not os.path.exists(full_path):
            return {"error": f"Proyecto no encontrado: {full_path}"}

        # Verificar que existe AI config
        ai_config_path = os.path.join(full_path, ".architect.ai.json")
        if not os.path.exists(ai_config_path):
            return {"error": "No hay configuración de IA. Configura un proveedor en AI Config primero."}

        # Llamar a Architect vía HTTP
        architect_url = settings.architect_url.rstrip('/')
        params = {"project": full_path}

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                logger.info(f"🔍 Llamando a Architect: {architect_url}/ai/suggestions?{params}")
                resp = await client.get(f"{architect_url}/ai/suggestions", params=params)
                resp.raise_for_status()
                result = resp.json()

                if result.get("ok"):
                    logger.info(f"✅ Architect retornó {len(result.get('patterns', []))} sugerencias")
                    return result
                else:
                    error_msg = result.get("error", "Error desconocido de Architect")
                    logger.error(f"❌ Architect retornó error: {error_msg}")
                    return {"error": error_msg}
        except httpx.TimeoutException:
            logger.error("⏰ Timeout consultando a Architect")
            return {"error": "Timeout: Architect tardó más de 120s en responder"}
        except Exception as e:
            logger.exception(f"❌ Error llamando a Architect: {e}")
            return {"error": f"Error comunicando con Architect: {str(e)}"}

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
