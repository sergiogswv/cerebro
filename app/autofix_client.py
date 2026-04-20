"""
autofix_client.py — Cliente de Cerebro para disparar autofixes en el Executor.

Solo Cerebro habla con Executor. Este módulo encapsula toda la lógica de:
  1. Preparar el payload para Ejecutor (instruction, branch_prefix, provider)
  2. Llamar POST {executor_url}/command con action="autofix"
  3. Procesar el resultado y emitir eventos al Dashboard vía WebSocket
  4. Actualizar proactive_analysis_state en la DB de Cerebro
  5. Decidir si notificar o auto-merge según el resultado de validación

Flujo:
  ProactiveScheduler / DecisionEngine
    └─► AutofixClient.trigger_autofix(event)
          └─► Executor POST /command {action: "autofix"}
                └─► [Aider] → [build/test validation]
          └─► emit autofix_started / autofix_completed / autofix_failed
          └─► update proactive_analysis_state
          └─► notify si hay pending_review
"""

import asyncio
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List

import httpx

logger = logging.getLogger("cerebro.autofix_client")


class AutofixClient:
    """
    Gestiona el ciclo de vida de un autofix:
    Cerebro → Executor → Aider → Validación → Resultado al Dashboard.
    """

    def __init__(self, executor_url: str, cerebro_db_path: str):
        self.executor_url = executor_url.rstrip("/")
        self.db_path = cerebro_db_path

    # ─────────────────────────────────────────────────────────────────────────
    # Punto de entrada principal
    # ─────────────────────────────────────────────────────────────────────────

    async def trigger_autofix(self, event: Dict[str, Any], batch_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Dispara un autofix para el archivo/evento dado.

        Args:
            event: Evento estandarizado del agente (con payload.file, payload.recommendation, etc.)
                   Puede venir con un solo finding seleccionado por DecisionEngine
            batch_id: Identificador del batch nocturno

        Returns:
            Diccionario con resultado del autofix: branch, validated, files_modified, etc.
        """
        payload = event.get("payload", {})
        target_file = payload.get("file") or payload.get("target") or event.get("target", "")

        # ── INSTRUCCIÓN PARA AIDER: UNA SOLA TAREA ──────────────────────────
        # Estrategia de extracción en cascada (de más específico a más genérico)
        instruction = None
        source_field = None

        # 1. Recommendation directa (prioridad máxima)
        if payload.get("recommendation") and isinstance(payload["recommendation"], str):
            rec = payload["recommendation"].strip()
            if len(rec) > 10:  # Removido el filtro de "Revisar hallazgos..."
                instruction = rec
                source_field = "recommendation"
                logger.debug(f"✓ Instrucción de recommendation ({len(instruction)} chars)")

        # 2. Finding único estructurado
        if not instruction:
            findings = payload.get("findings", [])
            if isinstance(findings, list) and len(findings) > 0 and isinstance(findings[0], dict):
                finding = findings[0]
                instruction = finding.get("suggestion") or finding.get("description")
                if finding.get("file"):
                    target_file = finding["file"]
                if finding.get("line"):
                    instruction = f"Línea {finding['line']}: {instruction}"
                if instruction:
                    source_field = "findings[0]"
                    logger.debug(f"✓ Instrucción de findings[0] ({len(instruction)} chars)")

        # 3. Análisis del LLM (texto completo)
        if not instruction:
            analysis = payload.get("analysis")
            if isinstance(analysis, str) and len(analysis.strip()) > 20:
                # Extraer primera oración o párrafo relevante
                instruction = analysis.strip().split("\n\n")[0][:500]
                source_field = "analysis"
                logger.debug(f"✓ Instrucción de analysis ({len(instruction)} chars)")

        # 4. Summary
        if not instruction:
            summary = payload.get("summary")
            if isinstance(summary, str) and len(summary.strip()) > 10:
                instruction = summary.strip()[:500]
                source_field = "summary"
                logger.debug(f"✓ Instrucción de summary ({len(instruction)} chars)")

        # 5. Finding como string directo
        if not instruction:
            finding_str = payload.get("finding")
            if isinstance(finding_str, str) and len(finding_str.strip()) > 10:
                instruction = finding_str.strip()
                source_field = "finding"
                logger.debug(f"✓ Instrucción de finding ({len(instruction)} chars)")

        # Fallback último
        if not instruction:
            instruction = "Aplicar mejora de código sugerida en el archivo"
            source_field = "fallback"
            logger.warning(f"⚠️ Usando instrucción genérica por falta de contenido")

        project = payload.get("target") or ""

        # Log detallado para debug
        selected_info = ""
        if payload.get("selected_from_batch"):
            selected_info = f" [1/{payload.get('original_findings_count', '?')} seleccionado]"

        # Log de qué campos hay disponibles en el payload
        payload_keys = list(payload.keys())
        logger.info(f"🔍 Payload keys: {payload_keys}")
        logger.info(f"📝 Instrucción para Aider{selected_info} (source={source_field}): {instruction[:150]}...")

        # DEBUG: Emitir evento al Dashboard con el detalle de la instrucción
        await self._emit("autofix_instruction_debug", {
            "autofix_id": autofix_id,
            "target_file": target_file,
            "instruction_source": source_field,
            "instruction": instruction,
            "payload_keys": payload_keys,
            "selected_from_batch": payload.get("selected_from_batch", False),
            "original_findings_count": payload.get("original_findings_count", None),
            # Campos del payload que se usaron
            "recommendation_preview": payload.get("recommendation", "")[:200] if payload.get("recommendation") else None,
            "analysis_preview": payload.get("analysis", "")[:200] if payload.get("analysis") else None,
            "finding_preview": payload.get("finding", "")[:200] if payload.get("finding") else None,
            "summary_preview": payload.get("summary", "")[:200] if payload.get("summary") else None,
        })

        # DEBUG: Log completo del payload para la próxima ejecución (solo si instruction es fallback)
        if source_field == "fallback":
            logger.warning(f"⚠️ PAYLOAD COMPLETO para debug: {payload}")
        # ─────────────────────────────────────────────────────────────────────

        # Inferir provider desde config de Cerebro (usa Ollama por defecto)
        from app.config import get_settings
        settings = get_settings()

        autofix_id = str(uuid.uuid4())[:8]
        branch_prefix = "skrymir-fix/"
        
        logger.info(f"🔧 [AutofixClient] Iniciando autofix #{autofix_id} → {target_file}")

        # ── 1. Obtener configuración actual de Cerebro ───────────────────
        from app.config_manager import UnifiedConfigManager
        manager = UnifiedConfigManager.get_instance()
        cerebro_cfg = manager.get_config().cerebro
        
        # ── 2. Emitir evento de inicio ──────────────────────────────────────
        await self._emit("autofix_started", {
            "autofix_id": autofix_id,
            "target": target_file,
            "instruction": instruction[:200],
            "batch_id": batch_id,
            "branch_prefix": branch_prefix,
        })

        # ── 3. Llamar al Executor ───────────────────────────────────────────
        try:
            # Obtener configuración del scheduler para flags de validación
            from app.proactive_scheduler import get_proactive_scheduler
            scheduler = get_proactive_scheduler()
            pro_config = scheduler.get_config(project)
            val_cfg = pro_config.get("autofix", {}).get("validation", {})

            # Resolver modelo: Priorizar config de Cerebro
            model = cerebro_cfg.auto_fix_model or "deepseek-coder-v2:16b-lite-instruct-q4_K_M"
            provider = cerebro_cfg.auto_fix_provider or "ollama"
            base_url = cerebro_cfg.auto_fix_base_url
            api_key = cerebro_cfg.auto_fix_api_key

            executor_result = await self._call_executor(
                target_file=target_file,
                instruction=instruction,
                branch_prefix=branch_prefix,
                workspace_root=settings.workspace_root,
                model=model,
                provider=provider,
                base_url=base_url,
                api_key=api_key,
                run_tests=val_cfg.get("run_tests", True),
                require_build=val_cfg.get("require_build", True),
            )
            
            # ── 4. Retornar inmediatamente (se procesa en Async Callback) ───────
            logger.info("✅ Autofix aceptado por Executor y ejecutándose en background")
            return {"ok": True, "status": "started", "autofix_id": autofix_id}
            
        except httpx.ConnectError:
            logger.error("❌ Executor no disponible — autofix cancelado")
            await self._emit("autofix_failed", {
                "autofix_id": autofix_id,
                "target": target_file,
                "error": "Executor no disponible (ConnectError)",
                "batch_id": batch_id,
            })
            return {"ok": False, "error": "executor_unavailable"}
        except Exception as exc:
            logger.error(f"❌ Error llamando a Executor: {exc}")
            await self._emit("autofix_failed", {
                "autofix_id": autofix_id,
                "target": target_file,
                "error": str(exc),
                "batch_id": batch_id,
            })
            return {"ok": False, "error": str(exc)}

    async def process_autofix_result(self, event: Dict[str, Any]):
        """Procesa el resultado cuando Executor termina y reporta por /api/events"""
        payload = event.get("payload", {})
        autofix_id = payload.get("autofix_id")
        target_file = payload.get("target")
        batch_id = payload.get("batch_id")
        branch = payload.get("branch", "")
        validated = payload.get("fix_validated", False)
        suggested = payload.get("suggested_files", 0)
        files_count = payload.get("files_count", 0)

        # Determinar estado final
        if validated:
            status = "success"
            result_label = "success"
            logger.info(f"✅ Autofix validado (callback): branch={branch}, {files_count} archivos modificados")
        elif validated is False and suggested > 0:
            status = "pending_review"
            result_label = "pending_review"
            logger.info(f"⏳ Autofix pendiente de revisión (callback): {suggested} archivos .suggested creados")
        elif validated is None:
            # Sin build tool detectado — resultado indeterminado
            status = "no_validation"
            result_label = "pending_review"
            logger.info(f"⚠️  Autofix sin validación (no hay build): branch={branch}")
        else:
            status = "failed"
            result_label = "failed"
            logger.warning(f"❌ Autofix falló (callback): branch={branch}")

        # Emitir evento al Dashboard
        event_type = "autofix_completed" if status in ("success", "no_validation", "pending_review") else "autofix_failed"
        payload["status"] = status
        await self._emit(event_type, payload)

        # Actualizar base de datos de Cerebro
        if target_file:
            await self._update_db(
                file_path=target_file,
                result=result_label,
                batch_id=batch_id,
                branch=branch,
            )

        return status, branch, target_file

    # ─────────────────────────────────────────────────────────────────────────
    # Llamada al Executor
    # ─────────────────────────────────────────────────────────────────────────

    async def _call_executor(
        self,
        target_file: str,
        instruction: str,
        branch_prefix: str,
        workspace_root: str,
        model: str,
        provider: str,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        run_tests: bool = True,
        require_build: bool = True,
    ) -> Dict[str, Any]:
        """POST `{executor_url}/command` con action='autofix'."""
        request_id = f"autofix-{uuid.uuid4().hex[:8]}"
        body = {
            "action": "autofix",
            "target": target_file,
            "request_id": request_id,
            "options": {
                "instruction": instruction,
                "branch_prefix": branch_prefix,
                "workspace_root": workspace_root,
                "provider": provider,
                "model": model,
                "base_url": base_url,
                "api_key": api_key,
                "cerebro_url": "http://localhost:4000",
                "run_tests": run_tests,
                "require_build": require_build,
            },
        }
        logger.debug(f"→ Executor POST /command: {body}")
        async with httpx.AsyncClient(timeout=10.0) as client:  # Rápido, porque ahora encola y retorna
            resp = await client.post(f"{self.executor_url}/command", json=body)
            resp.raise_for_status()
            return resp.json()

    # ─────────────────────────────────────────────────────────────────────────
    # Interactivo (Feature / Bugfix)
    # ─────────────────────────────────────────────────────────────────────────

    async def trigger_interactive_job(
        self,
        action: str,
        instruction: str,
        target_file: str = "",
        active_project: str = "",
        context_files: List[str] = None
    ) -> Dict[str, Any]:
        """Dispara un request explícito desde el humano hacia el Executor."""
        import os
        from app.config import get_settings
        settings = get_settings()
        workspace_root = settings.workspace_root

        # Si se especificó un proyecto activo, resolver la ruta completa
        if active_project and active_project not in ("", "Ninguno"):
            candidate = active_project if os.path.isabs(active_project) else os.path.join(workspace_root, active_project)
            if os.path.isdir(candidate):
                workspace_root = candidate
                logger.info(f"📂 [Interactive] workspace_root resuelto a proyecto: {workspace_root}")
            else:
                logger.warning(f"⚠️ [Interactive] Proyecto '{active_project}' no encontrado en {workspace_root}, usando root por defecto")
        else:
            logger.warning(f"⚠️ [Interactive] No se especificó active_project, Aider correrá en workspace_root: {workspace_root}")
        
        # Resolver modelo: Priorizar config de Cerebro
        from app.config_manager import UnifiedConfigManager
        cerebro_cfg = UnifiedConfigManager.get_instance().get_config().cerebro
        model = cerebro_cfg.auto_fix_model or "deepseek-coder-v2:16b-lite-instruct-q4_K_M"
        provider = cerebro_cfg.auto_fix_provider or "ollama"
        base_url = cerebro_cfg.auto_fix_base_url
        api_key = cerebro_cfg.auto_fix_api_key

        # Obtener datos de contexto desde .sentinelrc.toml para enriquecer el prompt
        project_context = ""
        try:
            sentinel_rc_path = os.path.join(workspace_root, ".sentinelrc.toml")
            if os.path.exists(sentinel_rc_path):
                import toml
                with open(sentinel_rc_path, "r", encoding="utf-8") as f:
                    config = toml.load(f)
                    fw = config.get("project", {}).get("framework", "unknown")
                    lang = config.get("project", {}).get("code_language", "unknown")
                    rules = config.get("sentinel", {}).get("rules", [])
                    
                    if fw != "unknown" or lang != "unknown":
                        project_context = f"\n\n--- INSTRUCCIONES DE CONTEXTO TÉCNICO ---\n"
                        project_context += f"Framework del Proyecto: {fw}\n"
                        project_context += f"Lenguaje de Programación: {lang}\n"
                        if rules:
                            project_context += f"Reglas Arquitecturales a seguir: {', '.join(rules)}\n"
                        project_context += "Por favor adapta tu código exclusivamente a estas tecnologías y reglas."
        except Exception as e:
            logger.warning(f"No se pudo extraer contexto técnico de .sentinelrc.toml: {e}")

        request_id = f"{action}-{uuid.uuid4().hex[:8]}"
        body = {
            "action": action,
            "target": target_file,
            "request_id": request_id,
            "options": {
                "instruction": f"{instruction}{project_context}",
                "branch_prefix": f"skrymir-{action}/",
                "workspace_root": workspace_root,
                "provider": provider,
                "model": model,
                "base_url": base_url,
                "api_key": api_key,
                "cerebro_url": "http://localhost:4000",
                "context_files": context_files or [],
                "run_tests": True, # Interactivo suele querer validación por defecto
                "require_build": True,
            },
        }

        # Emitimos un evento inicial confirmando el inicio de la petición interactiva
        await self._emit(f"{action}_started", {
            "target": target_file,
            "instruction": "..." if len(instruction)>50 else instruction,
            "status": "started",
            "request_id": request_id
        })

        try:
            logger.info(f"🚀 Enviando Interactive {action} a Executor ({request_id})...")
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(f"{self.executor_url}/command", json=body)
                resp.raise_for_status()
                return {"ok": True, "status": "started", "request_id": request_id}
        except Exception as exc:
            logger.error(f"❌ Error al disparar {action} en Executor: {exc}")
            await self._emit(f"{action}_failed", {
                "target": target_file,
                "status": "failed",
                "error": str(exc),
                "request_id": request_id
            })
            return {"ok": False, "error": str(exc)}


    # ─────────────────────────────────────────────────────────────────────────
    # WebSocket broadcast vía Cerebro
    # ─────────────────────────────────────────────────────────────────────────

    async def _emit(self, event_type: str, payload: Dict[str, Any]):
        """Emite un evento al Dashboard vía el bus de eventos de Cerebro."""
        try:
            from app.sockets import emit_agent_event
            await emit_agent_event({
                "source": "executor",
                "type": event_type,
                "severity": "info" if "completed" in event_type or "started" in event_type else "error",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payload": payload,
            })
        except Exception as exc:
            logger.warning(f"No se pudo emitir {event_type}: {exc}")

    # ─────────────────────────────────────────────────────────────────────────
    # Persistencia
    # ─────────────────────────────────────────────────────────────────────────

    async def _update_db(
        self,
        file_path: str,
        result: str,
        batch_id: Optional[str],
        branch: str,
    ):
        """Actualiza el estado del archivo en proactive_analysis_state."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            def _write():
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute(
                        """UPDATE proactive_analysis_state
                           SET status = 'autofixed',
                               last_autofix_result = ?,
                               times_autofixed = times_autofixed + 1,
                               last_analyzed_at = ?
                           WHERE file_path = ?
                             AND (batch_id = ? OR batch_id IS NULL)
                        """,
                        (result, now, file_path, batch_id)
                    )
                    # Si no había registro, insertar uno nuevo
                    if conn.execute("SELECT changes()").fetchone()[0] == 0:
                        conn.execute(
                            """INSERT INTO proactive_analysis_state
                               (id, project, mode, file_path, last_analyzed_at,
                                status, last_autofix_result, times_autofixed, batch_id)
                               VALUES (?, 'default', 'autofix', ?, ?, 'autofixed', ?, 1, ?)
                            """,
                            (str(uuid.uuid4()), file_path, now, result, batch_id)
                        )
            await asyncio.to_thread(_write)
            logger.debug(f"💾 DB actualizada: {file_path} → {result}")
        except Exception as exc:
            logger.warning(f"⚠️  No se pudo actualizar DB para {file_path}: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Singleton global
# ─────────────────────────────────────────────────────────────────────────────

_autofix_client: Optional[AutofixClient] = None


def get_autofix_client() -> AutofixClient:
    global _autofix_client
    if _autofix_client is None:
        from app.config import get_settings
        s = get_settings()
        from app.proactive_scheduler import get_proactive_scheduler
        db = get_proactive_scheduler().db_path
        _autofix_client = AutofixClient(
            executor_url=s.executor_url,
            cerebro_db_path=db,
        )
    return _autofix_client
