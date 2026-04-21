"""Event Router - Routes events to appropriate handlers."""

import logging
import uuid
import re as _re
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional

from app.decision_engine import DecisionEngine, DecisionAction
from app.dispatcher import notify, send_command, send_raw_command
from app.models import AgentEvent, OrchestratorCommand
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
        # 🔥 Bloqueo para evitar bucles infinitos en modo autónomo
        # { "project_root": timestamp_of_last_dispatch }
        self._active_processing: Dict[str, float] = {}
        self._debounce_window = 180.0 # 3 minutos (suficiente para la mayoría de los saltos de Aider)
        # 🛡️ Lock de async para evitar race conditions en el de-bounce
        import asyncio
        self._route_lock = asyncio.Lock()
        # 🛡️ Cache para evitar duplicados de reportes de Sentinel (ADK/Core)
        # { "file_path": (timestamp, findings_hash) }
        self._sentinel_reports_cache: Dict[str, Any] = {}

    async def route(self, event: AgentEvent) -> Dict[str, Any]:
        async with self._route_lock:
            return await self._route_internal(event)

    async def start_stale_lock_cleanup(self) -> None:
        """
        Tarea de fondo (TASK-03): Detecta y libera locks huérfanos.
        Un lock es huérfano si lleva más de debounce_window * 1.5 segundos sin
        ser liberado (indica que el Executor crashó sin notificar a Cerebro).

        Se lanza desde el startup de Cerebro como una tarea asyncio de fondo.
        """
        import asyncio, time
        stale_threshold = self._debounce_window * 1.5  # 270 segundos por defecto
        logger.info(f"🧹 Stale lock cleanup iniciado (threshold={stale_threshold:.0f}s)")

        while True:
            await asyncio.sleep(60)  # Revisar cada minuto
            try:
                now = time.time()
                stale_keys = [
                    k for k, ts in list(self._active_processing.items())
                    if (now - ts) > stale_threshold
                ]
                for key in stale_keys:
                    elapsed = now - self._active_processing.get(key, now)
                    del self._active_processing[key]
                    logger.warning(
                        f"🧹 Lock huérfano liberado: {key} "
                        f"(llevó {elapsed:.0f}s, threshold={stale_threshold:.0f}s)"
                    )
                    await emit_agent_event({
                        "source": "cerebro",
                        "type": "stale_lock_released",
                        "severity": "warning",
                        "payload": {
                            "project_key": key,
                            "elapsed_seconds": round(elapsed),
                            "threshold_seconds": stale_threshold,
                            "message": f"🧹 Lock huérfano liberado para: {key[:60]}",
                        }
                    })
            except Exception as cleanup_err:
                logger.warning(f"⚠️ Error en stale lock cleanup: {cleanup_err}")

    def get_active_locks(self) -> List[Dict]:
        """Devuelve info de todos los locks activos (para el dashboard de salud)."""
        import time
        now = time.time()
        return [
            {
                "key": k,
                "elapsed_seconds": round(now - ts),
                "stale": (now - ts) > (self._debounce_window * 1.5),
            }
            for k, ts in self._active_processing.items()
        ]

    async def _route_internal(self, event: AgentEvent) -> Dict[str, Any]:
        """
        Route an event to appropriate handlers.

        Returns:
            Dict with actions taken
        """
        # ── EVENTOS DE EXECUTOR (Control de ciclo de vida de fixes) ──
        if event.source == "executor" and event.type in ("task_completed", "task_failed", "autofix_completed", "autofix_failed", "feature_completed", "feature_failed", "bugfix_completed", "bugfix_failed"):
            file_path = event.payload.get("target") or event.payload.get("file")
            logger.info(f"✅ [Cerebro] Executor terminó {event.type} para {file_path}. Reanudando Sentinel...")
            
            # 1. Quitar bloqueo local (buscando el proyecto base)
            if file_path:
                fp_norm = file_path.replace("\\", "/").lower().rstrip("/")
                # Intentar limpiar bloqueos que contengan esta ruta o viceversa
                to_remove = []
                for k in self._active_processing.keys():
                    k_norm = k.replace("\\", "/").lower().rstrip("/")
                    if k_norm in fp_norm or fp_norm in k_norm:
                        to_remove.append(k)
                
                for k in to_remove:
                    self._active_processing.pop(k, None)
                    logger.info(f"🔓 [Cerebro] Bloqueo liberado para {k}")
            
            # 2. Reanudar monitoreo en Sentinel Core
            await send_command("sentinel_core", OrchestratorCommand(action="monitor/resume"))
            
            # 3. Disparar una re-validación inmediata si fue un éxito
            if event.type.endswith("_completed") and file_path:
                logger.info(f"🔍 [Cerebro] Disparando re-análisis de verificación para {file_path}")
                await send_command("sentinel_core", OrchestratorCommand(action="analyze", target=file_path))
            
            # 4. Si era una inicialización de proyecto, marcar como terminada y activar agentes
            if event.type in ("feature_completed", "feature_failed", "autofix_completed"):
                try:
                    import asyncio
                    from app.orchestrator import orchestrator
                    
                    found_project = None
                    for p in list(orchestrator._initializing_projects):
                        p_path = orchestrator._projects.get_project_path(p)
                        # Verificar si el target del evento pertenece a la ruta de este proyecto
                        if p_path and file_path and (p_path.lower() in file_path.lower() or file_path.lower() in p_path.lower()):
                            found_project = p
                            break
                    
                    if found_project:
                        logger.info(f"✨ [Cerebro] Proyecto `{found_project}` construido y verificado. Actualizando estado en DB.")
                        # ✅ Usar DB en lugar del set en memoria (persistente ante reinicios)
                        new_state = 'active' if event.type.endswith('_completed') else 'error'
                        self.context_db.set_project_state(
                            found_project, new_state,
                            metadata={'finished_by': event.type, 'event_id': event.id}
                        )
                        
                        if event.type.endswith("_completed"):
                            await notify(f"🚀 **Proyecto `{found_project}` listo:** estructura construida y Verificación de Runtime EXITOSA.", level="success")
                            # Trigger Tribunal y Activación de Agentes
                            if orchestrator.active_project == found_project:
                                logger.info(f"🔄 Activando agentes para `{found_project}` tras construcción exitosa.")
                                asyncio.create_task(orchestrator.set_active_project(found_project))

                    # 💡 REGISTRAR APRENDIZAJE: Registrar el éxito/fallo como un outcome
                    if self.context_db:
                        outcome_type = "correct" if "_completed" in event.type else "false_negative"
                        self.context_db.record_outcome(
                            event_id=event.id,
                            file_path=file_path or found_project,
                            outcome_type=outcome_type,
                            outcome_details=f"Executor {event.type}: {event.payload.get('message', '')}",
                            auto_detected=True
                        )
                except Exception as ex:
                    logger.warning(f"⚠️ Error finalizando estado de inicialización o registrando outcome: {ex}")

            # TASK-05: Feedback automático basado en el resultado del build ────────
            # Si el Executor reporta éxito/fracaso de build, registramos feedback
            # automático para cerrar el loop de aprendizaje sin intervención humana.
            if self.context_db and event.type in (
                "autofix_completed", "autofix_failed",
                "feature_completed", "feature_failed",
                "bugfix_completed",  "bugfix_failed",
            ):
                try:
                    build_success = event.type.endswith("_completed")
                    build_exit   = event.payload.get("build_exit_code")
                    # Si build_exit_code está disponible, usarlo para mayor precisión
                    if build_exit is not None:
                        build_success = (build_exit == 0)

                    auto_feedback_type = "thumbs_up" if build_success else "thumbs_down"
                    auto_reason = (
                        f"Auto-feedback: {'Build exitoso ✅' if build_success else 'Build fallido ❌'}"
                        f" | {event.type}"
                        + (f" | exit_code={build_exit}" if build_exit is not None else "")
                    )

                    self.context_db.record_feedback(
                        event_id=event.id,
                        feedback_type=auto_feedback_type,
                        decision_actions=event.payload.get("actions"),
                        reason=auto_reason,
                        suggested_action=None,
                    )
                    logger.info(
                        f"🤖 [AutoFeedback] {auto_feedback_type} registrado automáticamente "
                        f"para {event.type} ({file_path or 'unknown'})"
                    )
                except Exception as fb_err:
                    logger.debug(f"No se pudo registrar auto-feedback: {fb_err}")

            # 5. Notificar al dashboard antes de terminar
            await emit_agent_event(event.model_dump(mode="json"))

                
            return {"action": "resume_cycle", "status": "completed"}


        # Emit to dashboard first (except for file_change which is decorated below
        # and analysis_completed which has its own de-duplication logic)
        if not (event.source == "sentinel" and event.type == "file_change") and event.type != "analysis_completed":
            await emit_agent_event(event.model_dump(mode="json"))

        logger.info(
            f"📥 [{event.source.upper()}] type={event.type} "
            f"severity={event.severity} id={event.id}"
        )

        result = {"event_id": event.id, "actions": []}

        # ── EVENTOS DE SENTINEL CORE (file_change) ──
        # Cuando Sentinel Core detecta cambio de archivo
        if event.type == "file_change" and event.source == "sentinel":
            from app.config import get_settings
            import uuid
            settings = get_settings()
            file_path = event.payload.get("file", "unknown")

            # 🛑 VALIDAR DEBOUNCE: Si ya estamos procesando este proyecto, ignoramos
            import time
            now = time.time()
            
            logger.info(f"👀 [Cerebro] Sentinel Core reportó cambio: {file_path}")

            # SIEMPRE emitir evento al timeline para que aparezca en dashboard
            # Hacemos esto ANTES del bloqueo para que el usuario vea que Sentinel detectó el cambio
            await emit_agent_event({
                "source": "sentinel",
                "type": "file_change",
                "severity": event.severity,
                "timestamp": event.timestamp or datetime.now(timezone.utc).isoformat(),
                "id": event.id,
                "payload": {
                    "original_event_id": event.id,
                    "file": file_path,
                    "agent_status": "eye_active",
                    "short_file": file_path.replace("\\", "/").split("/")[-1],
                    "message": event.payload.get("message", f"Sentinel detectó cambios en {file_path}. Iniciando inspección..."),
                }
            })

            # 🛑 VALIDAR DEBOUNCE: Si ya estamos procesando este proyecto, ignoramos el ANÁLISIS AUTOMÁTICO
            import time
            now = time.time()
            file_path_norm = file_path.replace("\\", "/").lower()
            
            is_locked = False
            for locked_root, lock_time in self._active_processing.items():
                locked_root_norm = locked_root.replace("\\", "/").lower()
                if file_path_norm.startswith(locked_root_norm) or locked_root_norm in file_path_norm:
                    elapsed = now - lock_time
                    if elapsed < self._debounce_window:
                        logger.info(f"⏳ [Cerebro] Ignorando ANÁLISIS de {file_path} (Proyecto {locked_root} bloqueado: {elapsed:.1f}s restan)")
                        await emit_agent_event({
                            "source": "cerebro",
                            "type": "decision",
                            "severity": "info",
                            "payload": {
                                "action": "skip_analysis",
                                "reason": "debounce",
                                "file": file_path,
                                "message": f"⏳ Análisis omitido por debounce ({elapsed:.1f}s desde el último cambio). El proyecto {locked_root} está bloqueado temporalmente.",
                            }
                        })
                        return {"action": "debounce_active", "status": "skipped"}
                    is_locked = True
                    break
            
            if is_locked:
                to_del = [k for k, v in self._active_processing.items() if (now - v) >= self._debounce_window]
                for k in to_del: del self._active_processing[k]

            from app.config import SentinelMode
            # Enrutar basado en el modo operativo
            if settings.sentinel_mode == SentinelMode.CORE_ONLY.value:
                # Ya tenemos el análisis rudimentario del core en event.payload, lo dejamos pasar.
                pass
            elif settings.sentinel_mode in (SentinelMode.ADK_ONLY.value, SentinelMode.HYBRID.value):
                # TASK-12: Cache de decisiones LLM para evitar gastar tokens en cambios triviales
                should_invoke = await self._should_invoke_adk(file_path)
                if not should_invoke:
                    return {"action": "forward_to_adk", "status": "skipped", "reason": "cache_hit"}

                logger.info(f"📡 [Cerebro] Reenviando a ADK para análisis LLM: {file_path}")

                # Emitir evento de transición
                await emit_agent_event({
                    "source": "cerebro",
                    "type": "sentinel_core_to_adk",
                    "severity": "info",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "id": f"transition-{event.id[:8]}", # ID derivado
                    "payload": {
                        "original_event_id": event.id,
                        "file": file_path,
                        "message": "Core detectó cambio, reenviando a ADK",
                    }
                })

                # Reenviar al ADK para análisis
                adk_result = await self._forward_to_sentinel_adk(event)
                result["actions"].append(adk_result)

        # ── EVENTOS DE SENTINEL (ADK o CORE) CON TAREA SELECCIONADA ──
        # Cuando Sentinel analiza y selecciona una tarea (ya sea vía ADK o via Core Rust)
        is_sentinel_completed = (event.type.startswith("sentinel_") and event.type.endswith("_completed")) or (event.type == "analysis_completed")
        
        if is_sentinel_completed:
            payload = event.payload or {}
            file_hint = payload.get("file")

            # 🛡️ DE-DUPLICACIÓN: Si el contenido es idéntico al último reporte (hace < 60s), ignoramos
            import hashlib
            import time
            import re
            
            # Normalizar findings: ignorar números de línea y espacios extras para el hash
            findings_text = str(payload.get("finding") or payload.get("findings") or "")
            normalized_findings = re.sub(r'L\d+(-L\d+)?', '', findings_text) # Quitar L123 o L123-L125
            normalized_findings = re.sub(r':\d+', ':', normalized_findings)    # Quitar :123
            normalized_findings = "".join(normalized_findings.split())       # Quitar todos los espacios
            
            findings_hash = hashlib.md5(normalized_findings.encode()).hexdigest()
            
            if file_hint:
                last_time, last_hash = self._sentinel_reports_cache.get(file_hint, (0, ""))
                if last_hash == findings_hash and (time.time() - last_time) < 120:
                    logger.info(f"♻️ [Cerebro] Ignorando reporte duplicado (hash estable) para {file_hint}")
                    return result
                
                # Actualizar cache
                self._sentinel_reports_cache[file_hint] = (time.time(), findings_hash)

            # 🛑 VALIDAR BLOQUEO: Si el proyecto está en active_processing, ignoramos reportes
            if file_hint:
                now = time.time()
                file_hint_norm = file_hint.replace("\\", "/").lower()
                for locked_root, lock_time in self._active_processing.items():
                    locked_root_norm = locked_root.replace("\\", "/").lower()
                    if file_hint_norm.startswith(locked_root_norm) or locked_root_norm in file_hint_norm:
                        if (now - lock_time) < self._debounce_window:
                            logger.info(f"⏳ [Cerebro] Ignorando reporte para {file_hint} (Proyecto {locked_root} en Fix)")
                            return result

            # Emitir al dashboard (ahora que sabemos que no es duplicado ni bloqueado)
            await emit_agent_event(event.model_dump(mode="json"))
            
            # Soporte tanto para 'finding' (ADK estructurado) como 'findings' (Core Rust / Legacy)
            task_raw = payload.get("finding") or payload.get("findings")
            
            # Limpiar tarea si es un bloque enorme (común en Core Rust)
            task_selected = task_raw
            if task_raw and len(task_raw) > 300:
                # Intentar extraer el primer punto de una lista o la primera oración significativa
                lines = [l.strip() for l in task_raw.split('\n') if l.strip()]
                for line in lines:
                    # Buscar líneas que parezcan tareas (empiecen con bullet, o tengan palabras clave)
                    if line.startswith(('-', '*', '1.', '###', '####')):
                        # Ignorar encabezados genéricos
                        if any(h in line.lower() for h in ["análisis", "analisis", "arquitectura", "calidad"]):
                            continue
                        task_selected = line.lstrip('-*#123456789. \t')
                        break
                    elif any(k in line.lower() for k in ["error", "vulnerabilidad", "bug", "refactor"]):
                        task_selected = line
                        break
                
                # Si falló la extracción, al menos truncar o tomar el primer párrafo
                if task_selected == task_raw:
                    task_selected = task_raw.split('\n\n')[0][:500]
                    if len(task_selected) < 20 and len(task_raw) > 20: # Probablemente un título
                        task_selected = task_raw[:500]

            task_type = payload.get("task_type")
            task_priority = payload.get("task_priority") or "medium"
            file_hint = payload.get("file")
            original_count = payload.get("original_findings_count", 1)

            # Fallback para tareas sin tipo (común en eventos directos del Core Rust)
            if task_selected and not task_type:
                logger.info(f"ℹ️ [Cerebro] Tarea sin tipo explícito (posible Core Rust), asignando 'refactor' por defecto")
                task_type = "refactor"
                
                # Inferencia de tipo y prioridad basada en contenido (para Core Rust)
                tl = str(task_selected).lower()
                if any(w in tl for w in ["seguridad", "security", "vulnerabilidad", "vulnerability", "crítico", "critico", "critical"]):
                    task_type = "security"
                    task_priority = "critical" if "critic" in tl else "high"
                elif any(w in tl for w in ["bug", "error", "fallo"]):
                    task_type = "bugfix"
                    task_priority = "high"
                elif any(w in tl for w in ["propuesta", "sugerencia", "mejora", "style"]):
                    task_type = "refactor"
                    task_priority = "low"

            # Solo procesar si hay una tarea válida
            if not task_selected or not task_type:
                logger.warning(f"⚠️ [Cerebro] Evento {event.type} ignorado para despacho: no contiene tarea válida. (task_selected={task_selected is not None}, task_type={task_type})")
                await emit_agent_event({
                    "source": "cerebro",
                    "type": "decision",
                    "severity": "warning",
                    "payload": {
                        "action": "ignore_event",
                        "reason": "missing_task",
                        "message": f"⚠️ Evento {event.type} de {event.source} ignorado: No se pudo extraer una tarea o tipo de acción válido.",
                    }
                })
                return result

            if self._is_auto_mode_enabled():
                logger.info(f"🎯 [Cerebro] Tarea detectada para ejecución: {task_type} ({task_priority})")

                await emit_agent_event({
                    "source": "executor", # Antes 'cerebro', para que aparezca en la columna Warden & Executor
                    "type": "sentinel_task_selected",
                    "severity": "info" if task_priority not in ["critical", "high"] else "warning",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "payload": {
                        "original_event_id": event.id,
                        "task_type": task_type,
                        "priority": task_priority,
                        "selected_task": task_selected,
                        "recommendation": task_selected, # 🔥 Duplicamos como 'recommendation' para activar botones en el dashboard
                        "file": file_hint,
                        "message": f"Tarea {task_type} seleccionada de Sentinel",
                    }
                })

                # 🔥 REGISTRAR BLOQUEO: Este proyecto entra en ventana de fix
                if file_hint:
                    # Intentar deducir la raíz del proyecto para bloquearlo entero
                    # Si no, bloquear el archivo y sus alrededores
                    project_root = file_hint
                    try:
                        import os
                        if os.path.isfile(file_hint):
                            # Buscar package.json hacia arriba (simplificado)
                            curr = os.path.dirname(file_hint)
                            for _ in range(5):
                                if any(os.path.exists(os.path.join(curr, f)) for f in ["package.json", "Cargo.toml", ".git"]):
                                    project_root = curr
                                    break
                                parent = os.path.dirname(curr)
                                if parent == curr: break
                                curr = parent
                    except: pass

                    project_root_norm = project_root.replace("\\", "/").lower()
                    self._active_processing[project_root_norm] = time.time()
                    logger.info(f"🔒 [Cerebro] Bloqueando re-análisis en PROYECTO {project_root_norm} por {self._debounce_window}s")

                # Enviar tarea a Executor
                # IMPORTANTE: Enviamos 'task_raw' para que Aider tenga el contexto completo del error,
                # mientras que 'task_selected' se usó solo para el resumen visual en el Dashboard.
                executor_result = await self._dispatch_to_executor(event, task_raw or task_selected, file_hint, task_type)
                result["actions"].append(executor_result)

                return result

        # ── EVENTOS DE RESULTADO AUTÓMATA (AUTOFIX CALLBACK) Y MANUAL ──
        if event.type in (
            "autofix_completed", "autofix_failed",
            "feature_completed", "feature_failed",
            "bugfix_completed", "bugfix_failed"
        ):
            # NOTA: No hacemos el release del lock aquí para no duplicar lógica.
            # El bloque de arriba en _route_internal ahora maneja el release para estos eventos.
            
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
                        
                        # Enviar al Tribunal de Agentes en paralelo para validación multi-agente
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

        # ── RUTEO ESTÁNDAR PARA EL RESTO DE EVENTOS ──
        return await self._evaluate_standard(event)

    async def _trigger_tribunal(self, repo_dir: str, target: str, target_abs: str):
        """Dispara de manera simultánea a Sentinel, Architect y Warden tras la implementación de un requerimiento manual."""
        from app.dispatcher import send_raw_command
        from app.sockets import emit_agent_event
        import uuid
        import asyncio

        await emit_agent_event({
            "source": "cerebro", "type": "tribunal_started", "severity": "info",
            "payload": {"message": f"Iniciando TRIBUNAL (validación multi-agente paralela) para {target}"}
        })
        logger.info(f"⚖️ Iniciando TRIBUNAL multi-agente paralelo para {target}")

        async def safe_agent_call(name, action, subcommand, timeout=30):
            try:
                command = {
                    "action": action,
                    "subcommand": subcommand,
                    "target": target_abs or repo_dir,
                    "request_id": f"{name}-{uuid.uuid4().hex[:8]}"
                }
                logger.debug(f"⚖️ -> Pidiendo a {name}...")
                result = await asyncio.wait_for(send_raw_command(name, command), timeout=timeout)
                return {"agent": name, "status": "ok", "result": result}
            except asyncio.TimeoutError:
                logger.warning(f"⚠️ Timeout esperando a {name} en tribunal")
                return {"agent": name, "status": "timeout"}
            except Exception as e:
                logger.error(f"❌ Error en {name} durante tribunal: {e}")
                return {"agent": name, "status": "error", "error": str(e)}

        try:
            # Ejecutar de manera concurrente
            results = await asyncio.gather(
                safe_agent_call("sentinel_core", "pro", "check"),
                safe_agent_call("architect", "pro", "review"),
                safe_agent_call("warden", "pro", "scan"),
                return_exceptions=True
            )

            # Procesar los resultados
            for r in results:
                if isinstance(r, dict) and r.get("status") == "ok":
                    agent = r["agent"]
                    ack = r["result"]
                    if not ack or not self.context_db:
                        continue
                        
                    result_data = ack.get("result", {})
                    
                    if agent == "sentinel_core":
                        analysis = result_data.get("analysis") or result_data.get("summary")
                        if analysis:
                            self.context_db.record_pattern(
                                pattern_type="sentinel_tribunal_analysis",
                                description=analysis[:500],
                                severity="info",
                                file_path=target,
                                metadata={"full_analysis": analysis, "source": "sentinel"}
                            )
                    elif agent == "architect":
                        analysis = result_data.get("analysis") or result_data.get("feedback")
                        if analysis:
                            self.context_db.record_pattern(
                                pattern_type="architect_tribunal_review",
                                description=analysis[:500],
                                severity="info",
                                file_path=target,
                                metadata={"full_analysis": analysis, "source": "architect"}
                            )
                    elif agent == "warden":
                        finding = result_data.get("finding") or result_data.get("summary")
                        if finding:
                            self.context_db.record_pattern(
                                pattern_type="warden_tribunal_scan",
                                description=finding[:500],
                                severity="warning",
                                file_path=target,
                                metadata={"full_finding": finding, "source": "warden"}
                            )

            await emit_agent_event({
                "source": "cerebro", "type": "tribunal_completed", "severity": "success",
                "payload": {"message": f"Tribunal desplegado correctamente sobre {target}"}
            })
            logger.info(f"✅ TRIBUNAL lanzado exitosamente para {target} (paralelo)")

        except Exception as e:
            logger.error(f"❌ Error lanzando el tribunal paralelo: {e}")


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

        # TASK-14: Trazar decisión en la DB de contexto
        if self.context_db:
            try:
                self.context_db.log_decision_trace(
                    event_id=event.id,
                    node="decision_engine",
                    decision=",".join([a.value for a in decision.actions]),
                    context={"reason": decision.reason, "auto_mode": self._is_auto_mode_enabled()}
                )
            except Exception as e:
                logger.debug(f"Error logging decision trace: {e}")

        # Emitir decisión al timeline
        import uuid as _uuid
        decision_id = str(_uuid.uuid4())
        await emit_agent_event({
            "source": "cerebro",
            "type": "decision",
            "id": decision_id,   # Este ID se usa para el feedback del usuario
            "severity": "info",
            "payload": {
                "decision_id": decision_id,
                "actions": [a.value for a in decision.actions],
                "reason": decision.reason,
                "confidence": round(decision.confidence, 2),
                "message": f"🧠 Cerebro decidió: {decision.reason} (Acciones: {', '.join([a.value for a in decision.actions]) or 'Ninguna'})",
                "event_type": event.type,
                "event_source": event.source,
                "original_event_id": event.id, # Link feedback to the original event
                "target_agents": decision.target_agents,
            }
        })

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

        file_path = (
            event.payload.get("file") or 
            event.payload.get("target") or 
            event.payload.get("path") or 
            event.payload.get("project")
        ) if event.payload else None

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

        IMPORTANTE: Si el evento tiene múltiples findings, selecciona SOLO UNO
        para evitar que Aider intente resolver todo a la vez y termine haciendo
        cambios inconsistentes o incompletos.
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

        # Convertir evento a dict para evaluación
        event_dict = event.model_dump(mode="json")

        # Verificar confianza vs. threshold
        night_mode = scheduler.is_night_mode_active(config)
        if not self.decision_engine.should_autofix(event_dict, night_mode):
            logger.info("🚫 should_autofix=False — no se dispara autofix")
            return {"action": "autofix", "status": "skipped", "reason": "below_threshold"}

        # ── SELECCIÓN DE TAREA ÚNICA ──────────────────────────────────────────
        # Si hay múltiples findings, seleccionar solo uno para esta iteración
        selected_event = self.decision_engine.select_single_task(event_dict)
        if selected_event is None:
            logger.warning("⚠️ No hay findings procesables para autofix")
            return {"action": "autofix", "status": "skipped", "reason": "no_findings"}

        # Si se seleccionó un finding de un batch, loguearlo
        if selected_event.get("payload", {}).get("selected_from_batch"):
            orig_count = selected_event.get("payload", {}).get("original_findings_count", 0)
            remaining = selected_event.get("payload", {}).get("remaining_count", 0)
            logger.info(f"   📋 Iteración actual: 1/{orig_count} findings ({remaining} pendientes)")
        # ──────────────────────────────────────────────────────────────────────

        batch_id = event.payload.get("batch_id") if event.payload else None
        logger.info(f"🔧 Disparando autofix para evento {event.type} (batch={batch_id})")

        try:
            client = get_autofix_client()
            result = await client.trigger_autofix(
                event=selected_event,  # Usar evento con finding único seleccionado
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

    async def _should_invoke_adk(self, file_path: str) -> bool:
        """Verifica si el archivo cambió lo suficiente o si usamos el cache. Evita malgastar tokens."""
        import os
        import hashlib
        import time
        if not os.path.exists(file_path):
            return True
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            # Clean comments and whitespace
            import re
            cleaned_content = re.sub(r'#.*', '', content)
            cleaned_content = re.sub(r'//.*', '', cleaned_content)
            cleaned_content = "".join(cleaned_content.split())
            change_hash = hashlib.md5(cleaned_content.encode()).hexdigest()

            if not hasattr(self, '_adk_cache'):
                self._adk_cache = {}

            last_time, last_hash = self._adk_cache.get(file_path, (0, ""))
            if last_hash == change_hash and (time.time() - last_time) < 600:
                logger.info(f"💾 [Cerebro] Usando análisis cacheado para {file_path} (mismo contenido sustantivo)")
                return False
            
            self._adk_cache[file_path] = (time.time(), change_hash)
            return True
        except Exception as e:
            logger.debug(f"Error checkeando cache ADK en {file_path}: {e}")
            return True

    def _is_auto_mode_enabled(self) -> bool:
        """Verifica si el modo autónomo está habilitado en la configuración."""
        try:
            from app.config_manager import UnifiedConfigManager
            manager = UnifiedConfigManager.get_instance()
            unified_config = manager.get_config()
            cerebro_config = unified_config.cerebro if hasattr(unified_config, 'cerebro') else None
            return cerebro_config.auto_fix_enabled if cerebro_config else False
        except Exception as e:
            logger.warning(f"⚠️ No se pudo verificar auto_mode: {e}")
            return False

    async def _forward_to_sentinel_adk(self, event: AgentEvent) -> Dict:
        """
        Reenvía un evento file_change del Core al ADK para análisis LLM.
        """
        from app.dispatcher import send_command
        from app.models import OrchestratorCommand

        file_path = event.payload.get("file") if event.payload else None
        if not file_path:
            logger.warning("⚠️ [Cerebro] Evento file_change sin archivo, no se puede reenviar a ADK")
            return {"action": "forward_to_adk", "status": "skipped", "reason": "no_file"}

        logger.info(f"🔄 [Cerebro] Reenviando archivo a Sentinel ADK: {file_path}")

        try:
            # Enviar comando al ADK para análisis del archivo
            ack = await send_command(
                "sentinel_adk",
                OrchestratorCommand(
                    action="check",
                    target=file_path,
                    options={"auto": True, "triggered_by": "file_change"}
                )
            )

            if ack.get("status") == "completed" or (ack.get("result") and ack.get("result").get("status") == "completed"):
                logger.info(f"✅ [Cerebro] ADK completó análisis de {file_path}")
                return {
                    "action": "forward_to_adk",
                    "status": "success",
                    "file": file_path,
                    "adk_result": ack.get("result")
                }
            else:
                logger.warning(f"⚠️ [Cerebro] ADK no completó análisis: {ack.get('error', 'Unknown error')} - Iniciando fallback a Core")
                
                # Fallback al Core Rust
                from app.sockets import emit_agent_event
                await emit_agent_event({
                    "source": "cerebro", "type": "degraded_mode_active", "severity": "warning",
                    "payload": {"reason": "adk_unavailable", "fallback": "core_static", "file": file_path}
                })

                import uuid
                from app.dispatcher import send_raw_command
                logger.info("🛡️ Sentinel en modo degradado: Usando Core Rust estático")
                fallback_ack = await send_raw_command("sentinel_core", {
                    "action": "pro", "subcommand": "check",
                    "target": file_path,
                    "request_id": f"sentinel-fb-{uuid.uuid4().hex[:8]}"
                })
                
                return {
                    "action": "forward_to_adk",
                    "status": "degraded_success" if fallback_ack and fallback_ack.get("status") == "completed" else "failed",
                    "file": file_path,
                    "fallback_used": True,
                    "adk_result": fallback_ack.get("result") if fallback_ack else None,
                    "error": ack.get("error")
                }

        except Exception as exc:
            logger.error(f"❌ [Cerebro] Error reenviando a ADK: {exc} - Iniciando fallback a Core")
            from app.sockets import emit_agent_event
            await emit_agent_event({
                "source": "cerebro", "type": "degraded_mode_active", "severity": "warning",
                "payload": {"reason": "adk_exception", "fallback": "core_static", "file": file_path}
            })
            
            try:
                import uuid
                from app.dispatcher import send_raw_command
                fallback_ack = await send_raw_command("sentinel_core", {
                    "action": "pro", "subcommand": "check",
                    "target": file_path,
                    "request_id": f"sentinel-fb-{uuid.uuid4().hex[:8]}"
                })
                return {
                    "action": "forward_to_adk",
                    "status": "degraded_success" if fallback_ack and fallback_ack.get("status") == "completed" else "failed",
                    "file": file_path,
                    "fallback_used": True,
                    "adk_result": fallback_ack.get("result") if fallback_ack else None,
                    "error": str(exc)
                }
            except Exception as fb_exc:
                logger.error(f"❌ [Cerebro] Ambos ADK y Core fallaron para {file_path}: {fb_exc}")
                return {
                    "action": "forward_to_adk",
                    "status": "error",
                    "file": file_path,
                    "error": str(exc),
                    "fallback_error": str(fb_exc)
                }

    async def _dispatch_to_executor(self, event: AgentEvent, task_description: str, file_hint: str | None, task_type: str) -> Dict:
        """
        Envía una tarea seleccionada por Sentinel ADK al Executor para ejecución automática.
        """
        import uuid
        from datetime import datetime, timezone

        logger.info(f"🚀 [Cerebro] Enviando tarea a Executor: {task_type}")

        # Determinar acción: 'autofix' usa Aider, 'run' para otras tareas
        # Incluimos permanentemente maintainability y refactor en autofix
        is_autofix = task_type in ["bugfix", "security", "autofix", "maintainability", "refactor"]
        action = "autofix" if is_autofix else "run"

        # Obtener prioridad desde el evento original si existe
        payload_orig = event.payload or {}
        task_priority = payload_orig.get("task_priority") or payload_orig.get("priority", "medium")

        # 1. Ya no pausamos Sentinel Core físicamente (Cerebro ya bloquea el re-análisis via _active_processing)
        # Esto evita que Sentinel se quede pausado permanentemente si falla la notificación de finalización.
        logger.info("🛡️ [Cerebro] Manteniendo Sentinel Core activo (Lock lógico aplicado en Cerebro)")

        # Construir el comando para Executor (formato compatible con handle_command en executor/app/routes.py)
        from app.config import get_settings
        from app.config_manager import UnifiedConfigManager
        
        settings = get_settings()
        manager = UnifiedConfigManager.get_instance()
        unified_config = manager.get_config()
        
        # 1. Prioridad: Valores configurados dinámicamente desde el Dashboard
        # 2. Fallback: Valores de variables de entorno (Settings)
        # 3. Fallback final: Hardcoded defaults
        
        # Obtener cerebro config si existe en el unified config
        cerebro_conf = getattr(unified_config, 'cerebro', None)
        global_llm = unified_config.global_config.get("llm", {}) if hasattr(unified_config, 'global_config') else {}
        
        # 1. Resolver Provider
        provider = (getattr(cerebro_conf, 'auto_fix_provider', None) or 
                    global_llm.get("provider") or
                    settings.autofix_llm_provider or 
                    "ollama")
        
        # 2. Resolver Model
        model = (getattr(cerebro_conf, 'auto_fix_model', None) or 
                 global_llm.get("model") or
                 settings.autofix_llm_model or 
                 "deepseek-coder-v2:16b-lite-instruct-q4_K_M")
        
        # 3. Resolver Base URL
        base_url = (getattr(cerebro_conf, 'auto_fix_base_url', None) or 
                    global_llm.get("base_url") or
                    settings.autofix_api_base or "")
        
        # 4. Resolver API Key
        api_key = (getattr(cerebro_conf, 'auto_fix_api_key', None) or 
                   global_llm.get("api_key") or
                   settings.autofix_api_key or "")

        executor_payload = {
            "action": action,
            "service": payload_orig.get("service") or payload_orig.get("origin") or "default",
            "target": file_hint or payload_orig.get("target"),
            "request_id": f"cerebro-sentinel-{event.id[:8]}",
            "options": {
                "instruction": task_description,
                "priority": task_priority,
                "auto_approve": True,
                "context_files": [file_hint] if file_hint else [],
                "max_build_retries": 5,
                "require_build": True,
                # 🔥 Configuración dinámica de LLM resuelta
                "provider": str(provider),
                "model": str(model),
                "api_key": str(api_key),
                "base_url": str(base_url)
            }
        }

        logger.info(f"📤 [Cerebro] Despachando a Executor ({action}): {model} via {provider}")
        logger.debug(f"DEBUG Payload: {executor_payload}")

        # Emitir evento de inicio de ejecución al timeline
        await emit_agent_event({
            "source": "executor", # Antes 'cerebro', cambiado para aparecer en la columna correcta
            "type": "executor_task_dispatched",
            "severity": "info",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": {
                "original_event_id": event.id,
                "task_type": task_type,
                "task_description": task_description[:200] if len(task_description) > 200 else task_description,
                "file": file_hint,
                "executor_request_id": executor_payload["request_id"],
                "message": f"Tarea enviada a Executor: {task_type} (acción: {action})",
            }
        })

        # Enviar comando a Executor
        try:
            result = await send_raw_command("ejecutor", executor_payload)

            # El Executor retorna un ApiResponse(ok, message, data: CommandAck)
            success = result.get("ok") is True
            data = result.get("data") or {}
            status = data.get("status", "unknown") if success else result.get("status", "error")

            # Emitir resultado al timeline
            await emit_agent_event({
                "source": "executor",
                "type": "executor_task_completed" if success else "executor_task_failed",
                "severity": "success" if success else "error",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payload": {
                    "original_event_id": event.id,
                    "executor_request_id": executor_payload["request_id"],
                    "task_type": task_type,
                    "status": status,
                    "result": data.get("result") if success else None,
                    "error": data.get("error") or result.get("message") if not success else None,
                    "message": f"Ejecución {'aceptada' if success else 'fallida'}: {task_type}",
                }
            })

            if success:
                logger.info(f"✅ [Cerebro] Tarea ejecutada exitosamente por Executor")
                await notify(
                    f"✅ **Tarea {task_type} ejecutada automáticamente**\n📁 `{file_hint or 'N/A'}`\n📝 {task_description[:100]}...",
                    level="info",
                    source="cerebro"
                )
            else:
                logger.warning(f"⚠️ [Cerebro] Executor reportó error: {result.get('error')}")
                await notify(
                    f"⚠️ **Tarea {task_type} requiere atención**\nExecutor reportó: {result.get('error', 'Error desconocido')}",
                    level="warning",
                    source="cerebro"
                )

            return {
                "action": "dispatch_to_executor",
                "status": status,
                "executor_request_id": executor_payload["request_id"],
                "task_type": task_type,
                "file": file_hint,
                "result": result,
            }

        except Exception as exc:
            logger.error(f"❌ [Cerebro] Error enviando tarea a Executor: {exc}")

            # Emitir error al timeline
            await emit_agent_event({
                "source": "cerebro",
                "type": "executor_task_failed",
                "severity": "error",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payload": {
                    "original_event_id": event.id,
                    "task_type": task_type,
                    "error": str(exc),
                    "message": f"Error enviando tarea a Executor: {exc}",
                }
            })

            await notify(
                f"❌ **Error enviando tarea a Executor:** `{exc}`",
                level="error",
                source="cerebro"
            )

            return {
                "action": "dispatch_to_executor",
                "status": "error",
                "error": str(exc),
                "task_type": task_type,
            }

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
