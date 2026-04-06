import logging
import os
import json
from typing import List, Any, Dict, Optional
from app.models import AgentEvent, Severity, OrchestratorCommand
from app.dispatcher import send_command, notify
from app.decision_engine import DecisionEngine, DecisionAction
from app.context_db import ContextDB, get_context_db
from app.change_manager import ChangeManager, get_change_manager
from app.config_manager import UnifiedConfigManager

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
    Mantiene un AnalysisPipeline para análisis secuencial de archivos.
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

        # Analysis Pipeline for sequential agent execution
        from app.pipeline.config_manager import PipelineConfigManager
        from app.pipeline.analysis_pipeline import AnalysisPipeline
        pipeline_config = PipelineConfigManager.get_instance().get_config()
        self.analysis_pipeline = AnalysisPipeline(pipeline_config)

        # Circuit breaker status check loop
        self._timeout_check_task = None

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
        Si es análisis de Sentinel o Architect, procesar violaciones como cambios pendientes.
        """
        logger.warning(f"⚠️ WARNING desde {event.source} - type={event.type}")

        # Si es análisis de Sentinel con problemas, procesarlos
        if event.source == "sentinel" and event.type == "analysis_completed":
            logger.info("🔍 Procesando análisis de Sentinel (warning)...")
            result = await self._process_sentinel_analysis(event)
            logger.info(f"✅ Análisis procesado: {result}")
            return result

        # Si es análisis de Architect con hallazgos, procesarlos
        if event.source == "architect" and event.type in ("architect_lint_completed", "architect_analyze_completed"):
            logger.info("🏛️ Procesando análisis de Architect (warning)...")
            result = await self._process_architect_analysis(event)
            logger.info(f"✅ Análisis de Architect procesado: {result}")
            return result

        # Si es análisis de Warden con hallazgos, procesarlos
        if event.source == "warden" and event.type.startswith("warden_"):
            logger.info("🔱 Procesando análisis de Warden (warning)...")
            result = await self._process_warden_analysis(event)
            logger.info(f"✅ Análisis de Warden procesado: {result}")
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

        # Si es análisis de Architect con hallazgos, procesarlos
        if event.source == "architect" and event.type in ("architect_lint_completed", "architect_analyze_completed"):
            logger.info("🏛️ Procesando análisis de Architect (info)...")
            return await self._process_architect_analysis(event)

        # Si es análisis de Warden con hallazgos, procesarlos
        if event.source == "warden" and event.type.startswith("warden_"):
            logger.info("🔱 Procesando análisis de Warden (info)...")
            return await self._process_warden_analysis(event)

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

                # Emitir evento de inicio de autofix para trazabilidad en timeline
                await emit_agent_event({
                    "source": "cerebro",
                    "type": "autofix_started",
                    "severity": "info",
                    "payload": {
                        "file": file_path,
                        "source_agent": event.source,
                        "max_retries": max_retries,
                        "description": description[:200] if description else None,
                        "recommendation": recommendation[:200] if recommendation else None,
                    }
                })

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
                        # Emitir evento: enviando comando al Executor
                        await emit_agent_event({
                            "source": "cerebro",
                            "type": "autofix_sending_to_executor",
                            "severity": "info",
                            "payload": {
                                "file": file_path,
                                "attempt": attempt,
                                "max_retries": max_retries,
                                "target": "ejecutor",
                                "action": "autofix",
                            }
                        })

                        ack = await send_command("ejecutor", fix_cmd)
                        fix_result = {}
                        if ack and isinstance(ack, dict):
                            fix_result = ack.get("data", {}).get("result", {}) or {}

                        last_result = fix_result
                        fix_validated = fix_result.get("fix_validated")
                        branch = fix_result.get("branch", f"{branch_prefix}?")

                        # Extraer info de archivos modificados para trazabilidad
                        modified_files = fix_result.get("modified_files", [])
                        suggested_files = fix_result.get("suggested_files", [])
                        files_count = fix_result.get("files_count", 0)
                        suggested_count = fix_result.get("suggested_count", 0)
                        build_exit_code = fix_result.get("build_exit_code")
                        build_tool = fix_result.get("build_tool", "desconocido")
                        build_output = (fix_result.get("build_output") or "")[:400]
                        git_diff_stat = fix_result.get("git_diff_stat", "")

                        # Emitir evento: resultado recibido del Executor
                        await emit_agent_event({
                            "source": "ejecutor",
                            "type": "autofix_executor_response",
                            "severity": "info" if fix_validated is True else ("error" if fix_validated is False else "warning"),
                            "payload": {
                                "file": file_path,
                                "attempt": attempt,
                                "branch": branch,
                                "fix_validated": fix_validated,
                                "build_exit_code": build_exit_code,
                                "build_tool": build_tool,
                                "files_modified": files_count,
                                "files_list": [f["path"] for f in modified_files[:5]] if modified_files else [],
                                "suggested_count": suggested_count,
                                "suggested_files": [s["original"] for s in suggested_files] if suggested_files else [],
                            }
                        })

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
                                    "suggested_count": 0,  # Éxito = no hay suggested
                                    "message": f"Fix validado y aplicado en {files_count} archivos",
                                }
                            })
                            return  # ← Éxito, salir

                        elif fix_validated is False:
                            # ❌ Build falló, acumular error para el próximo intento
                            logger.warning(f"⚠️ Intento {attempt} falló build. Acumulando error para reintento...")
                            accumulated_errors.append(build_output)

                            # Info de archivos modificados y suggested en este intento
                            attempt_files = fix_result.get("modified_files", [])
                            attempt_diff = fix_result.get("git_diff_stat", "")[:500]
                            suggested_files = fix_result.get("suggested_files", [])
                            suggested_count = fix_result.get("suggested_count", 0)

                            # Emitir evento de build fallido para trazabilidad
                            await emit_agent_event({
                                "source": "ejecutor",
                                "type": "autofix_build_failed",
                                "severity": "error",
                                "payload": {
                                    "file": file_path,
                                    "attempt": attempt,
                                    "branch": branch,
                                    "build_exit_code": fix_result.get("build_exit_code"),
                                    "build_tool": fix_result.get("build_tool"),
                                    "build_output_preview": build_output[:500] if build_output else None,
                                    "files_modified_count": len(attempt_files) if attempt_files else 0,
                                    "suggested_count": suggested_count,
                                    "suggested_files": [s["original"] for s in suggested_files] if suggested_files else [],
                                    "message": f"Build falló en intento {attempt}. {suggested_count} archivos .suggested creados.",
                                }
                            })

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

                    # Info de archivos suggested en el último intento
                    last_suggested_files = last_result.get("suggested_files", []) if last_result else []
                    last_suggested_count = last_result.get("suggested_count", 0) if last_result else 0

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
                            # Info de archivos .suggested creados
                            "suggested_count": last_suggested_count,
                            "suggested_files": [s["original"] for s in last_suggested_files] if last_suggested_files else [],
                            "suggested_full_paths": [s["suggested"] for s in last_suggested_files] if last_suggested_files else [],
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
        Procesa resultados de análisis de Sentinel (Bridge v2).
        Extrae hallazgos y los agrega como cambios pendientes.

        El payload viene del Bridge de Sentinel con formato:
        - { finding: "descripción", recommendation: "sugerencia", file: "ruta", severity: "...", findings: [...] }
        """
        logger.info(f"🔍 Procesando análisis de Sentinel: {event.type}")

        payload = event.payload or {}
        file_path = payload.get("file") or payload.get("target", "unknown")

        # Extraer finding principal (nuevo formato del bridge)
        finding = payload.get("finding", "")
        recommendation = payload.get("recommendation", "")
        severity = payload.get("severity", "warning")
        summary = payload.get("summary", "")

        # Extraer lista de findings
        findings_list = payload.get("findings", [])
        findings_count = payload.get("findings_count", len(findings_list))

        # Si hay una lista de findings estructurados, procesarlos
        changes_added = []
        if findings_list and isinstance(findings_list, list):
            logger.info(f"  ↳ {len(findings_list)} hallazgos de Sentinel detectados")

            for i, item in enumerate(findings_list[:10]):  # Límite de 10
                if isinstance(item, dict):
                    desc = item.get("message") or item.get("description", str(item))
                    affected_file = item.get("file") or item.get("path") or file_path
                    item_severity = item.get("severity", "warning")
                    item_recommendation = item.get("recommendation")
                else:
                    desc = str(item)
                    affected_file = file_path
                    item_severity = severity
                    item_recommendation = recommendation

                change = await self.change_manager.add_change(
                    event_id=event.id,
                    file_path=affected_file,
                    description=desc[:200],
                    severity=item_severity if item_severity in ("critical", "error", "warning") else "warning",
                    recommendation=item_recommendation or f"Revisar: {desc[:100]}",
                    metadata={
                        "source": "sentinel_analysis",
                        "event_type": event.type,
                        "finding_index": i,
                        "full_finding": str(item),
                    },
                )
                changes_added.append(change.id)

        # Si no hay lista de findings pero sí hay un finding principal, agregarlo
        elif finding:
            logger.info(f"  ↳ 1 hallazgo de Sentinel detectado")

            change = await self.change_manager.add_change(
                event_id=event.id,
                file_path=file_path,
                description=finding[:200],
                severity=severity if severity in ("critical", "error", "warning") else "warning",
                recommendation=recommendation or "Revisar hallazgo de Sentinel",
                metadata={
                    "source": "sentinel_analysis",
                    "event_type": event.type,
                    "summary": summary,
                },
            )
            changes_added.append(change.id)

        if not changes_added:
            logger.debug("  ↳ Sin hallazgos de Sentinel para procesar")
            return {"action": "logged_only"}

        logger.info(f"✅ {len(changes_added)} cambios de Sentinel agregados a ChangeManager")

        # Notificar al usuario
        from app.dispatcher import notify
        message = f"🛡️ **Sentinel detectó {len(changes_added)} problemas** en `{file_path}`\n\n"

        if finding:
            message += f"**Hallazgo:** {finding[:200]}\n\n"

        if summary:
            message += f"**Resumen:** {summary[:200]}\n\n"

        if findings_list and len(findings_list) > 0:
            for i, item in enumerate(findings_list[:5]):
                if isinstance(item, dict):
                    desc = item.get("message", str(item))[:150]
                else:
                    desc = str(item)[:150]
                message += f"{i+1}. {desc}...\n"

            if len(findings_list) > 5:
                message += f"\n_...y {len(findings_list) - 5} más_\n"

        message += "\n**Revisa el panel de cambios para aprobar/rechazar correcciones.**"

        await notify(message, level=severity if severity in ["critical", "error", "warning"] else "warning", source="cerebro")

        return {
            "action": "process_sentinel_analysis",
            "changes_added": len(changes_added),
            "change_ids": changes_added,
        }

    async def _process_architect_analysis(self, event: AgentEvent) -> dict:
        """
        Procesa resultados de análisis de Architect ADK.
        Extrae hallazgos de arquitectura y los agrega como cambios pendientes.

        El payload viene de Architect ADK con formato:
        - { finding: "descripción", recommendation: "sugerencia", file: "ruta" }
        - { result: { findings: [...], health_score: XX } }
        """
        logger.info(f"🏛️ Procesando análisis de Architect: {event.type}")

        payload = event.payload or {}

        # Extraer información del payload de Architect
        file_path = payload.get("file") or payload.get("target") or "unknown"
        finding = payload.get("finding", "")
        recommendation = payload.get("recommendation", "")
        summary = payload.get("summary", "")

        # Extraer findings del resultado raw si existe
        raw_result = payload.get("result", {}).get("raw", {})
        findings_list = []
        if isinstance(raw_result, dict):
            result_data = raw_result.get("result", {})
            if isinstance(result_data, dict):
                findings_list = result_data.get("findings", [])

        # Si no hay finding específico pero hay summary, usar el summary
        if not finding and summary:
            finding = summary

        # Si no hay recommendation pero hay summary, usar summary
        if not recommendation and summary:
            recommendation = summary

        # Determinar severidad
        severity = payload.get("severity", "warning")
        if event.severity:
            severity = event.severity.value

        # Si no hay hallazgos claros, no agregar cambios
        if not finding and not findings_list:
            logger.debug("  ↳ Sin hallazgos de arquitectura para procesar")
            return {"action": "logged_only"}

        changes_added = []

        # Si hay una lista de findings, procesar cada uno
        if findings_list and isinstance(findings_list, list):
            logger.info(f"  ↳ {len(findings_list)} hallazgos de arquitectura detectados")

            for i, item in enumerate(findings_list[:10]):  # Límite de 10
                if isinstance(item, dict):
                    desc = item.get("message") or item.get("description") or str(item)
                    affected_file = item.get("file") or item.get("path") or file_path
                    rule = item.get("rule", "")
                    sev = item.get("severity", "warning")
                else:
                    desc = str(item)
                    affected_file = file_path
                    rule = ""
                    sev = severity

                # Construir descripción completa
                full_desc = desc
                if rule:
                    full_desc = f"[{rule}] {desc}"

                change = await self.change_manager.add_change(
                    event_id=event.id,
                    file_path=affected_file,
                    description=full_desc[:200],
                    severity=sev if sev in ("critical", "error", "warning") else "warning",
                    recommendation=recommendation or f"Revisar violación de arquitectura: {desc[:100]}",
                    metadata={
                        "source": "architect_analysis",
                        "event_type": event.type,
                        "finding_index": i,
                        "health_score": payload.get("result", {}).get("health_score"),
                        "full_finding": str(item),
                    },
                )
                changes_added.append(change.id)

        # Si hay un finding único (formato simplificado)
        elif finding:
            logger.info(f"  ↳ 1 hallazgo de arquitectura detectado")

            change = await self.change_manager.add_change(
                event_id=event.id,
                file_path=file_path,
                description=finding[:200],
                severity=severity if severity in ("critical", "error", "warning") else "warning",
                recommendation=recommendation or "Revisar violación de arquitectura detectada",
                metadata={
                    "source": "architect_analysis",
                    "event_type": event.type,
                    "health_score": payload.get("result", {}).get("health_score"),
                    "analysis": summary,
                },
            )
            changes_added.append(change.id)

        if not changes_added:
            return {"action": "logged_only"}

        logger.info(f"✅ {len(changes_added)} cambios de Architect agregados a ChangeManager")

        # Notificar al usuario
        from app.dispatcher import notify
        message = f"🏛️ **Architect detectó {len(changes_added)} violaciones** en `{file_path}`\n\n"

        if finding:
            message += f"**Hallazgo:** {finding[:200]}\n\n"

        if recommendation:
            message += f"**Sugerencia:** {recommendation[:200]}\n\n"

        message += "**Revisa el panel de cambios para aprobar/rechazar correcciones.**"

        await notify(message, level=severity, source="cerebro")

        return {
            "action": "process_architect_analysis",
            "changes_added": len(changes_added),
            "change_ids": changes_added,
        }

    async def _process_warden_analysis(self, event: AgentEvent) -> dict:
        """
        Procesa resultados de análisis de Warden.
        Extrae hallazgos de seguridad y los agrega como cambios pendientes.

        El payload viene de Warden ADK con formato:
        - { finding: "descripción", recommendation: "sugerencia", file: "ruta" }
        - { result: { top_risks: [...], secrets_found: [...] } }
        """
        logger.info(f"🔱 Procesando análisis de Warden: {event.type}")

        payload = event.payload or {}

        # Extraer información del payload de Warden
        file_path = payload.get("file") or payload.get("target") or "unknown"
        finding = payload.get("finding", "")
        recommendation = payload.get("recommendation", "")
        summary = payload.get("summary", "")

        # Extraer info adicional
        risks_count = payload.get("risks_count", 0)
        secrets_count = payload.get("secrets_count", 0)

        # Determinar severidad
        severity = payload.get("severity", "warning")
        if event.severity:
            severity = event.severity.value

        # Si no hay hallazgos claros, no agregar cambios
        if not finding:
            logger.debug("  ↳ Sin hallazgos de seguridad para procesar")
            return {"action": "logged_only"}

        # Agregar el hallazgo como cambio pendiente
        change = await self.change_manager.add_change(
            event_id=event.id,
            file_path=file_path,
            description=finding[:200],
            severity=severity if severity in ("critical", "error", "warning") else "warning",
            recommendation=recommendation or "Revisar hallazgo de seguridad detectado",
            metadata={
                "source": "warden_analysis",
                "event_type": event.type,
                "risks_count": risks_count,
                "secrets_count": secrets_count,
                "analysis": summary,
            },
        )

        logger.info(f"✅ Cambio de Warden agregado a ChangeManager: {change.id}")

        # Notificar al usuario
        from app.dispatcher import notify
        message = f"🔱 **Warden detectó un problema de seguridad** en `{file_path}`\n\n"

        if finding:
            message += f"**Hallazgo:** {finding[:200]}\n\n"

        if recommendation:
            message += f"**Sugerencia:** {recommendation[:200]}\n\n"

        if risks_count > 0:
            message += f"*Riesgos identificados: {risks_count}*\n"
        if secrets_count > 0:
            message += f"*⚠️ Secretos expuestos: {secrets_count}*\n"

        message += "\n**Revisa el panel de cambios para aprobar/rechazar correcciones.**"

        await notify(message, level=severity, source="cerebro")

        return {
            "action": "process_warden_analysis",
            "changes_added": 1,
            "change_id": change.id,
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
        """Obtiene la configuración de Architect del ConfigManager"""
        if not self.active_project:
            return {"error": "No hay proyecto activo seleccionado"}

        config_manager = UnifiedConfigManager.get_instance()
        config = config_manager.get_project_config(self.active_project, 'architect')

        if not config:
            # Retornar una estructura básica si no existe
            return {
                "version": "1.0",
                "rules": [],
                "exclude": ["**/node_modules/**"]
            }

        return config

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
        """Obtiene la configuración de Sentinel del ConfigManager"""
        if not self.active_project:
            return {"error": "No hay proyecto activo seleccionado"}

        config_manager = UnifiedConfigManager.get_instance()
        config = config_manager.get_project_config(self.active_project, 'sentinel')

        if not config:
            return {"error": "Configuración de Sentinel no encontrada"}

        return config

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
        """
        Lanza el wizard interactivo de inicialización de Sentinel.
        Usa el sistema de eventos interaction_required para guiar al usuario paso a paso.
        """
        if not self.active_project:
            return {"error": "No hay proyecto activo seleccionado"}

        project_path = os.path.join(self.workspace_root, self.active_project).replace("\\", "/")

        # Iniciar el wizard emitiendo el primer paso: framework detection
        from app.sockets import emit_agent_event
        import uuid

        wizard_id = f"sentinel-wizard-{uuid.uuid4().hex[:8]}"

        # Guardar estado del wizard en el contexto
        self.context_db.record_pattern(
            pattern_type="sentinel_wizard_started",
            description="Wizard de inicialización de Sentinel iniciado",
            severity="info",
            file_path=project_path,
            metadata={"wizard_id": wizard_id, "step": "framework_detection", "project": self.active_project}
        )

        # Paso 1: Pedir al usuario que confirme o seleccione el framework
        # Obtener info del modelo configurado para mostrarlo
        from app.config_manager import UnifiedConfigManager
        import asyncio
        config_manager = UnifiedConfigManager.get_instance()
        llm_config = config_manager.get_agent_llm_config("sentinel")

        logger.info(f"🛡️ [sentinel_init] Emitiendo interaction_required para wizard {wizard_id}")

        await emit_agent_event({
            "source": "sentinel",
            "type": "interaction_required",
            "severity": "info",
            "payload": {
                "prompt_id": f"{wizard_id}-framework",
                "message": f"🛡️ **Sentinel Setup Wizard**\n\nPaso 1/3: Framework Detection\n\nEl asistente usará el modelo **{llm_config.model}** ({llm_config.provider}) configurado en Global Config para analizar tu proyecto.",
                "options": ["auto-detect", "manual-select"],
                "wizard_step": 1,
                "total_steps": 3,
                "wizard_id": wizard_id,
                "project": self.active_project,
                "llm_model": llm_config.model,
                "llm_provider": llm_config.provider
            }
        })

        logger.info(f"🛡️ [sentinel_init] Evento interaction_required emitido")

        # También emitir evento de inicio del wizard
        await emit_agent_event({
            "source": "sentinel",
            "type": "wizard_init_started",
            "severity": "info",
            "payload": {
                "wizard_id": wizard_id,
                "message": "Wizard de inicialización de Sentinel iniciado",
                "project": self.active_project,
                "step": 1,
                "total_steps": 3
            }
        })

        return {"status": "ok", "message": "Wizard de Sentinel iniciado. Revisa las notificaciones.", "wizard_id": wizard_id}

    async def handle_sentinel_wizard_response(self, wizard_id: str, step: str, answer: str) -> dict:
        """
        Maneja las respuestas del wizard de Sentinel paso a paso.
        """
        from app.sockets import emit_agent_event
        import uuid

        project_path = os.path.join(self.workspace_root, self.active_project).replace("\\", "/")
        logger.info(f"🛡️ [sentinel_wizard] handle_sentinel_wizardResponse called: wizard_id={wizard_id}, step='{step}', answer='{answer}'")

        if step == "framework_detection" or step == "1":
            # Framework detectado/seleccionado, pasar a paso 2: AI Provider
            if answer == "auto-detect":
                # Usar IA para detectar framework con el modelo configurado en Global Config
                from app.ai_utils import detectar_framework_con_ia, AIConfig
                from app.config_manager import UnifiedConfigManager

                config_manager = UnifiedConfigManager.get_instance()
                llm_config = config_manager.get_agent_llm_config("sentinel")

                # Detectar si la URL base tiene /v1 o necesita agregarlo
                base_url = llm_config.base_url.rstrip('/') if llm_config.base_url else 'http://localhost:11434'
                final_url = f"{base_url}/v1" if not base_url.endswith('/v1') else base_url

                logger.info(f"🔍 [sentinel_wizard] LLM Config: provider={llm_config.provider}, model={llm_config.model}")
                logger.info(f"🔍 [sentinel_wizard] URL base desde Global Config: {llm_config.base_url}")

                ai_config = AIConfig(
                    name="sentinel-wizard",
                    provider=llm_config.provider,
                    api_url=llm_config.base_url,
                    api_key=llm_config.api_key,
                    model=llm_config.model
                )

                logger.info(f"🔍 [sentinel_wizard] Iniciando detección de framework con IA: {ai_config.provider} / {ai_config.model}")

                # Notificar al dashboard que está consultando IA
                await emit_agent_event({
                    "source": "sentinel",
                    "type": "wizard_ai_query_start",
                    "severity": "info",
                    "payload": {
                        "wizard_id": wizard_id,
                        "step": 1,
                        "provider": ai_config.provider,
                        "model": ai_config.model,
                        "message": f"Consultando {ai_config.model} ({ai_config.provider}) para detectar framework..."
                    }
                })

                # Intentar detectar framework con IA
                try:
                    framework_detected = await detectar_framework_con_ia(project_path, [ai_config])
                    error_info = None
                except Exception as e:
                    import traceback
                    framework_detected = None
                    error_info = str(e)
                    logger.error(f"🔍 [sentinel_wizard] Error en detectar_framework_con_ia: {e}")
                    logger.error(f"🔍 [sentinel_wizard] Traceback: {traceback.format_exc()}")

                # Notificar resultado
                await emit_agent_event({
                    "source": "sentinel",
                    "type": "wizard_ai_query_complete",
                    "severity": "info" if framework_detected else "warning",
                    "payload": {
                        "wizard_id": wizard_id,
                        "step": 1,
                        "framework_detected": framework_detected,
                        "error_info": error_info,
                        "api_url": ai_config.api_url,
                        "provider": ai_config.provider,
                        "model": ai_config.model,
                        "message": f"Framework detectado: {framework_detected}" if framework_detected else f"IA no respondió: {error_info or 'Sin respuesta'}"
                    }
                })

                logger.info(f"🔍 [sentinel_wizard] Resultado de detección: {framework_detected}")

                if framework_detected:
                    framework = framework_detected
                    logger.info(f"✅ Framework detectado por IA: {framework}")
                else:
                    # Fallback a detección por archivos
                    framework = await self._detect_framework(project_path)
                    logger.info(f"⚠️ IA no detectó framework, usando detección por archivos: {framework}")
            else:
                framework = answer

            # Obtener el provider configurado en Global Config
            config_manager = UnifiedConfigManager.get_instance()
            llm_config = config_manager.get_agent_llm_config("sentinel")
            current_provider = llm_config.provider

            logger.info(f"🛡️ [sentinel_wizard] Emitiendo PASO 2: AI Provider. Provider actual: {current_provider}, Modelo: {llm_config.model}")

            await emit_agent_event({
                "source": "sentinel",
                "type": "interaction_required",
                "severity": "info",
                "payload": {
                    "prompt_id": f"{wizard_id}-ai-provider",
                    "message": f"🛡️ **Sentinel Setup Wizard**\n\nPaso 2/3: AI Provider Configuration\n\nFramework detectado: **{framework}**\n\nProvider configurado en Global Config: **{current_provider}** ({llm_config.model})\n\n¿Usar este provider o seleccionar otro?",
                    "options": [f"use-{current_provider}", "change-provider"],
                    "wizard_step": 2,
                    "total_steps": 3,
                    "wizard_id": wizard_id,
                    "framework": framework,
                    "current_provider": current_provider,
                    "current_model": llm_config.model
                }
            })

            logger.info(f"🛡️ [sentinel_wizard] PASO 2 emitido exitosamente")

            # Guardar progreso
            self.context_db.record_pattern(
                pattern_type="sentinel_wizard_progress",
                description=f"Framework seleccionado: {framework}",
                severity="info",
                file_path=project_path,
                metadata={"wizard_id": wizard_id, "step": "ai_provider", "framework": framework}
            )

            return {"status": "ok", "step": 2, "message": "Paso 2: Configuración de AI Provider"}

        elif step == "ai_provider" or step == "2":
            # AI Provider seleccionado, pasar a paso 3: Testing config
            if answer == "change-provider":
                # Si elige cambiar, mostrar opciones de providers
                await emit_agent_event({
                    "source": "sentinel",
                    "type": "interaction_required",
                    "severity": "info",
                    "payload": {
                        "prompt_id": f"{wizard_id}-ai-provider-select",
                        "message": f"🛡️ **Sentinel Setup Wizard**\n\nPaso 2/3: Seleccionar AI Provider\n\nSelecciona el proveedor de IA para análisis:",
                        "options": ["anthropic", "openai", "ollama", "google"],
                        "wizard_step": 2,
                        "total_steps": 3,
                        "wizard_id": wizard_id,
                        "substep": "select"
                    }
                })
                return {"status": "ok", "step": 2, "message": "Seleccionar AI Provider"}

            # Extraer provider de la respuesta (use-ollama -> ollama)
            provider = answer.lower().replace("use-", "") if answer.startswith("use-") else answer.lower()

            # Guardar provider seleccionado en el contexto para el paso 3
            self.context_db.record_pattern(
                pattern_type="sentinel_wizard_ai_config",
                description=f"AI Provider configurado: {provider}",
                severity="info",
                file_path=project_path,
                metadata={"wizard_id": wizard_id, "provider": provider, "use_global_config": answer.startswith("use-")}
            )

            # Sugerir config de testing basada en el framework
            test_suggestions = await self._get_test_suggestions(project_path)

            await emit_agent_event({
                "source": "sentinel",
                "type": "interaction_required",
                "severity": "info",
                "payload": {
                    "prompt_id": f"{wizard_id}-testing",
                    "message": f"🛡️ **Sentinel Setup Wizard**\n\nPaso 3/3: Testing Configuration\n\nProveedor de IA: **{provider}**\n\n{test_suggestions}\n\n¿Habilitar sugerencias de testing automáticas?",
                    "options": ["yes", "no"],
                    "wizard_step": 3,
                    "total_steps": 3,
                    "wizard_id": wizard_id,
                    "provider": provider
                }
            })

            # Guardar progreso
            self.context_db.record_pattern(
                pattern_type="sentinel_wizard_progress",
                description=f"AI Provider seleccionado: {provider}",
                severity="info",
                file_path=project_path,
                metadata={"wizard_id": wizard_id, "step": "testing_config", "provider": provider}
            )

            return {"status": "ok", "step": 3, "message": "Paso 3: Configuración de Testing"}

        elif step == "testing_config" or step == "3":
            try:
                logger.info(f"🛡️ [sentinel_wizard] ========== PROCESANDO PASO 3 ==========")
                logger.info(f"🛡️ [sentinel_wizard] Answer recibido: '{answer}' (step='{step}')")
                # Wizard completado - guardar configuración final
                enable_testing = answer.lower() in ["yes", "true", "1", "si"]
                logger.info(f"🛡️ [sentinel_wizard] Testing enabled: {enable_testing} (answer.lower()={answer.lower()})")

                # Crear configuración de Sentinel (pasar wizard_id para obtener config de LLM)
                logger.info(f"🛡️ [sentinel_wizard] Generando config para proyecto: {project_path}")
                logger.info(f"🛡️ [sentinel_wizard] wizard_id={wizard_id}, active_project={self.active_project}")
                config = await self._generate_sentinel_config(project_path, enable_testing, wizard_id)
                logger.info(f"🛡️ [sentinel_wizard] Config generada con keys: {list(config.keys())}")

                # Guardar el archivo de configuración
                config_path = os.path.join(project_path, ".sentinelrc.toml")
                config_path = config_path.replace("\\", "/")  # Normalizar path para Windows
                logger.info(f"🛡️ [sentinel_wizard] Guardando config en: {config_path}")
                logger.info(f"🛡️ [sentinel_wizard] Contenido de config: {config}")
                try:
                    try:
                        import toml
                    except ImportError:
                        logger.error(f"🛡️ [sentinel_wizard] ERROR: modulo 'toml' no instalado")
                        return {"status": "error", "message": "Modulo 'toml' no instalado"}
                    logger.info(f"🛡️ [sentinel_wizard] Escribiendo archivo...")
                    with open(config_path, "w", encoding="utf-8") as f:
                        toml.dump(config, f)
                    logger.info(f"🛡️ [sentinel_wizard] Config guardada exitosamente en {config_path}")
                    # Verificar que el archivo existe
                    if os.path.exists(config_path):
                        file_size = os.path.getsize(config_path)
                        logger.info(f"🛡️ [sentinel_wizard] Archivo verificado: existe, tamaño={file_size} bytes")
                    else:
                        logger.error(f"🛡️ [sentinel_wizard] Archivo NO existe después de guardar")

                    # Emitir evento de finalización
                    logger.info(f"🛡️ [sentinel_wizard] Emitiendo evento wizard_init_completed")
                    await emit_agent_event({
                        "source": "sentinel",
                        "type": "wizard_init_completed",
                        "severity": "info",
                        "payload": {
                            "wizard_id": wizard_id,
                            "message": "✅ Wizard de Sentinel completado exitosamente",
                            "project": self.active_project,
                            "config_path": str(config_path),
                            "testing_enabled": enable_testing
                        }
                    })

                    # Iniciar monitoreo automáticamente
                    await send_command(
                        "sentinel",
                        OrchestratorCommand(action="monitor", target=project_path)
                    )

                    return {
                        "status": "ok",
                        "message": "Wizard completado y configuración guardada",
                        "config_path": str(config_path),
                        "testing_enabled": enable_testing
                    }

                except Exception as e:
                    logger.error(f"🛡️ [sentinel_wizard] Error guardando configuración de Sentinel: {e}")
                    import traceback
                    logger.error(f"🛡️ [sentinel_wizard] Traceback: {traceback.format_exc()}")
                    return {"status": "error", "message": str(e)}
            except Exception as outer_e:
                logger.error(f"🛡️ [sentinel_wizard] ERROR CRÍTICO en paso 3: {outer_e}")
                import traceback
                logger.error(f"🛡️ [sentinel_wizard] Traceback: {traceback.format_exc()}")
                return {"status": "error", "message": f"Error crítico: {outer_e}"}

        logger.warning(f"🛡️ [sentinel_wizard] Paso no reconocido: '{step}'")
        return {"status": "error", "message": f"Paso desconocido: {step}"}

    async def _detect_framework(self, project_path: str) -> str:
        """Detecta el framework del proyecto basándose en archivos presentes."""
        import os

        indicators = {
            "nextjs": ["next.config.js", "next.config.mjs"],
            "nest": ["nest-cli.json"],
            "react": ["vite.config.js", "vite.config.ts", "src/App.jsx", "src/App.tsx"],
            "vue": ["vue.config.js", "vite.config.js"],
            "angular": ["angular.json"],
            "django": ["manage.py", "requirements.txt"],
            "flask": ["app.py", "requirements.txt"],
            "rust": ["Cargo.toml"],
            "go": ["go.mod"],
            "python": ["requirements.txt", "setup.py", "pyproject.toml"],
            "nodejs": ["package.json"]
        }

        for framework, files in indicators.items():
            for file in files:
                if os.path.exists(os.path.join(project_path, file)):
                    return framework

        return "unknown"

    async def _get_test_suggestions(self, project_path: str) -> str:
        """Genera sugerencias de testing basadas en el proyecto."""
        import os

        has_tests = False
        test_framework = None

        # Verificar presencia de tests existentes
        test_patterns = ["**/*.test.*", "**/*.spec.*", "**/test_*.py", "tests/", "__tests__/"]
        for pattern in test_patterns:
            import glob
            if glob.glob(os.path.join(project_path, pattern), recursive=True):
                has_tests = True
                break

        # Detectar framework de testing
        if os.path.exists(os.path.join(project_path, "package.json")):
            try:
                with open(os.path.join(project_path, "package.json"), "r") as f:
                    pkg = json.load(f)
                    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                    if "jest" in deps:
                        test_framework = "Jest"
                    elif "vitest" in deps:
                        test_framework = "Vitest"
                    elif "cypress" in deps:
                        test_framework = "Cypress"
                    elif "playwright" in deps:
                        test_framework = "Playwright"
            except:
                pass

        if has_tests:
            if test_framework:
                return f"✓ Tests existentes detectados ({test_framework})"
            return "✓ Tests existentes detectados"
        return "⚠ No se detectaron tests existentes"

    async def _generate_sentinel_config(self, project_path: str, enable_testing: bool, wizard_id: str = None) -> dict:
        """Genera la configuración inicial de Sentinel basada en el proyecto."""
        logger.info(f"🛡️ [_generate_sentinel_config] Iniciando: project_path={project_path}, enable_testing={enable_testing}, wizard_id={wizard_id}")
        framework = await self._detect_framework(project_path)
        logger.info(f"🛡️ [_generate_sentinel_config] Framework detectado: {framework}")

        # Obtener configuración de LLM desde Global Config
        from app.config_manager import UnifiedConfigManager
        config_manager = UnifiedConfigManager.get_instance()
        llm_config = config_manager.get_agent_llm_config("sentinel")
        logger.info(f"🛡️ [_generate_sentinel_config] LLM Config: provider={llm_config.provider}, model={llm_config.model}, base_url={llm_config.base_url}")

        # Determinar el modelo y provider a usar
        # Si hay un wizard_id, buscar si se guardó configuración específica
        model_name = llm_config.model
        provider = llm_config.provider
        api_url = llm_config.base_url
        api_key = llm_config.api_key

        if wizard_id:
            logger.info(f"🛡️ [_generate_sentinel_config] Buscando patterns para wizard_id={wizard_id}")
            # Buscar si se guardó configuración específica del wizard
            patterns = self.context_db.get_recent_patterns(
                source_filter="sentinel_wizard_ai_config",
                limit=10
            )
            logger.info(f"🛡️ [_generate_sentinel_config] Patterns encontrados: {len(patterns)}")
            for p in patterns:
                if p.get("metadata", {}).get("wizard_id") == wizard_id:
                    provider = p["metadata"].get("provider", provider)
                    logger.info(f"🛡️ [_generate_sentinel_config] Provider actualizado desde pattern: {provider}")
                    break

        config = {
            "sentinel": {
                "framework": framework,
                "code_language": self._get_language_for_framework(framework),
                "enable_monitor": True,
                "enable_git_hooks": True
            },
            "analysis": {
                "max_complexity": 10,
                "max_lines_per_function": 60,
                "forbidden_patterns": ["console.log", "debugger", "TODO: HACK"],
                "severity_threshold": "warning"
            },
            "testing": {
                "enabled": enable_testing,
                "auto_suggest": enable_testing,
                "min_coverage": 70 if enable_testing else 0
            },
            "primary_model": {
                "name": model_name,
                "url": api_url,
                "api_key": api_key,
                "provider": provider,
                "temperature": 0.1
            }
        }

        logger.info(f"🛡️ [sentinel_wizard] Config generada: framework={framework}, provider={provider}, model={model_name}, url={api_url}")

        return config

    def _get_language_for_framework(self, framework: str) -> str:
        """Retorna el lenguaje principal para un framework dado."""
        mapping = {
            "nextjs": "typescript",
            "nest": "typescript",
            "react": "typescript",
            "vue": "typescript",
            "angular": "typescript",
            "django": "python",
            "flask": "python",
            "rust": "rust",
            "go": "go",
            "python": "python",
            "nodejs": "javascript"
        }
        return mapping.get(framework, "unknown")

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

    # ── Analysis Pipeline Integration ──────────────────────────────────────────

    async def start_pipeline_analysis(self, file_path: str, agents: list[str] = None) -> dict:
        """
        Start a sequential analysis pipeline for a file.

        Args:
            file_path: Path to the file to analyze
            agents: Optional list of agents to run (defaults from pipeline config)

        Returns:
            Pipeline status dict
        """
        from app.pipeline.models import PipelineConfig

        if not self.active_project:
            return {"error": "No active project selected"}

        # Get agents from pipeline config if not specified
        if not agents:
            config = self.analysis_pipeline.config
            agents = [
                s.agent for s in config.auto_init.services
                if s.enabled
            ]
            agents.sort(key=lambda a: next(
                (s.priority for s in config.auto_init.services if s.agent == a), 999
            ))

        if not agents:
            return {"error": "No agents enabled in pipeline config"}

        try:
            status = self.analysis_pipeline.start(file_path, agents)

            # Emit event to dashboard
            from app.sockets import emit_pipeline_event
            await emit_pipeline_event("started", {
                "pipeline_id": status.id,
                "target_file": file_path,
                "agents_queue": agents,
            })

            return {"status": "ok", "pipeline_id": status.id, "state": status.state.value}
        except RuntimeError as e:
            logger.warning(f"Pipeline already running: {e}")
            return {"error": str(e), "current_state": self.analysis_pipeline.status.state.value if self.analysis_pipeline.status else None}

    async def on_agent_analysis_complete(self, agent: str, findings: list, duration: float = None):
        """Called when an agent completes its analysis."""
        from app.pipeline.models import AgentFindings, AgentFinding
        from datetime import datetime

        # Convert findings to pipeline model
        agent_findings = AgentFindings(
            agent=agent,
            findings=[
                AgentFinding(
                    id=f.get("id", str(uuid.uuid4())),
                    agent=agent,
                    file_path=f.get("file", f.get("file_path", "")),
                    severity=f.get("severity", "info"),
                    category=f.get("category", "code_quality"),
                    message=f.get("message", str(f)),
                )
                for f in findings
            ],
            completed_at=datetime.utcnow(),
            duration_seconds=duration,
        )

        status = self.analysis_pipeline.on_agent_completed(agent, agent_findings)

        # Emit event
        from app.sockets import emit_pipeline_event
        await emit_pipeline_event("agent_completed", {
            "pipeline_id": status.id,
            "agent": agent,
            "findings_count": len(findings),
            "state": status.state.value,
        })

        return status

    async def get_pipeline_status(self) -> dict:
        """Get current pipeline status for the dashboard."""
        status = self.analysis_pipeline.status
        if not status:
            return {"state": "idle"}

        return {
            "pipeline_id": status.id,
            "state": status.state.value,
            "target_file": status.target_file,
            "current_agent": status.current_agent,
            "completed_agents": status.completed_agents,
            "queued_agents": status.queued_agents,
            "round_count": status.round_count,
            "error": status.error,
            "unified_report": status.unified_report.model_dump() if status.unified_report else None,
        }

    async def pipeline_action(self, action: str, **kwargs) -> dict:
        """
        Perform an action on the current pipeline.

        Actions:
        - "retry": Retry current agent after timeout
        - "skip": Skip current agent and continue
        - "abort": Cancel pipeline
        - "approve": Approve fixes (requires finding_ids)
        """
        status = self.analysis_pipeline.status

        if not status:
            return {"error": "No pipeline running"}

        if action == "retry":
            new_status = self.analysis_pipeline.retry_current_agent()
            return {"status": "ok", "state": new_status.state.value if new_status else None}

        elif action == "skip":
            new_status = self.analysis_pipeline.skip_current_agent()
            return {"status": "ok", "state": new_status.state.value if new_status else None}

        elif action == "abort":
            new_status = self.analysis_pipeline.abort(kwargs.get("reason", "user_aborted"))
            return {"status": "ok", "state": new_status.state.value if new_status else None}

        elif action == "approve":
            finding_ids = kwargs.get("finding_ids", [])
            new_status = self.analysis_pipeline.approve_fixes(finding_ids)
            return {"status": "ok", "state": new_status.state.value if new_status else None}

        else:
            return {"error": f"Unknown action: {action}"}

    async def start_timeout_checker(self):
        """Start periodic timeout checking for the pipeline."""
        import asyncio

        async def check_loop():
            while True:
                try:
                    await asyncio.sleep(5)  # Check every 5 seconds
                    if self.analysis_pipeline.check_timeout():
                        logger.warning("Pipeline timeout detected, notifying dashboard")
                        from app.sockets import emit_pipeline_event
                        await emit_pipeline_event("paused", {
                            "reason": "timeout",
                            "agent": self.analysis_pipeline.status.current_agent if self.analysis_pipeline.status else None,
                        })
                except Exception as e:
                    logger.error(f"Error in timeout check loop: {e}")

        self._timeout_check_task = asyncio.create_task(check_loop())
        logger.info("Pipeline timeout checker started")

    async def stop_timeout_checker(self):
        """Stop the timeout checker."""
        if self._timeout_check_task:
            self._timeout_check_task.cancel()
            self._timeout_check_task = None
            logger.info("Pipeline timeout checker stopped")

    def get_circuit_status(self) -> dict:
        """Get circuit breaker status for monitoring."""
        return self.analysis_pipeline.get_circuit_status()


# Instancia global del orquestador
orchestrator = Orchestrator()
