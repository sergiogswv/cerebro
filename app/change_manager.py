"""
Change Manager — Cerebro
Gestiona el ciclo de cambios críticos:
1. Acumula cambios que requieren aprobación humana
2. Notifica al usuario (Telegram/Dashboard)
3. Espera aprobación/rechazo
4. Pausa Sentinel, aplica lote, reactiva Sentinel
"""

import logging
import asyncio
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
from enum import Enum
from dataclasses import dataclass, field
import uuid

logger = logging.getLogger("cerebro.change_manager")

# Importar para enviar comandos al Executor y emitir eventos
from app.dispatcher import send_command
from app.models import OrchestratorCommand
from app.sockets import emit_agent_event


class ChangeStatus(Enum):
    """Estados de un cambio pendiente"""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    FAILED = "failed"


@dataclass
class PendingChange:
    """Representa un cambio pendiente de aprobación"""
    id: str
    event_id: str
    file_path: str
    description: str
    severity: str
    recommendation: Optional[str] = None
    status: ChangeStatus = ChangeStatus.PENDING
    created_at: datetime = field(default_factory=datetime.utcnow)
    approved_at: Optional[datetime] = None
    applied_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "event_id": self.event_id,
            "file_path": self.file_path,
            "description": self.description,
            "severity": self.severity,
            "recommendation": self.recommendation,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "applied_at": self.applied_at.isoformat() if self.applied_at else None,
        }


class ChangeManager:
    """
    Gestiona cambios que requieren aprobación humana.
    Flujo:
    1. Sentinel detecta cambio crítico → Cerebro lo acumula
    2. Cerebro notifica usuario con opciones [APROBAR/RECHAZAR]
    3. Usuario aprueba → Cerebro pausa Sentinel, aplica cambios, reactiva
    4. Usuario rechaza → Cerebro descarta cambios y continúa
    """

    def __init__(self, orchestrator=None):
        self._pending_changes: Dict[str, PendingChange] = {}
        self._approved_batch: List[PendingChange] = []
        self._orchestrator = orchestrator
        self._sentinel_paused = False
        self._apply_lock = asyncio.Lock()

        # Configuración
        self.auto_notify = True  # Notificar automáticamente al acumular
        self.batch_timeout_minutes = 5  # Tiempo máximo antes de notificar lote
        self._last_notification: Optional[datetime] = None

    def set_orchestrator(self, orchestrator):
        """Inyecta referencia al orchestrator para ejecutar comandos"""
        self._orchestrator = orchestrator

    async def add_change(
        self,
        event_id: str,
        file_path: str,
        description: str,
        severity: str,
        recommendation: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> PendingChange:
        """
        Agrega un cambio pendiente de aprobación.

        Args:
            event_id: ID del evento original
            file_path: Archivo afectado
            description: Descripción del cambio
            severity: Severidad (error, critical)
            recommendation: Recomendación de Sentinel/Architect
            metadata: Datos adicionales

        Returns:
            PendingChange: Cambio creado
        """
        change_id = str(uuid.uuid4())[:8]
        change = PendingChange(
            id=change_id,
            event_id=event_id,
            file_path=file_path,
            description=description,
            severity=severity,
            recommendation=recommendation,
            metadata=metadata or {},
        )

        self._pending_changes[change_id] = change
        logger.info(f"📝 Cambio pendiente agregado: {change_id} ({file_path})")

        # Notificar si está habilitado
        if self.auto_notify:
            await self._notify_pending_changes()

        return change

    async def _notify_pending_changes(self):
        """Notifica al usuario sobre cambios pendientes"""
        now = datetime.utcnow()

        # Evitar notificaciones muy frecuentes
        if self._last_notification and (now - self._last_notification).total_seconds() < 60:
            return

        pending = list(self._pending_changes.values())
        if not pending:
            return

        # Construir mensaje
        critical_count = sum(1 for c in pending if c.severity == "critical")
        error_count = sum(1 for c in pending if c.severity == "error")

        message = f"🔒 **CAMBIOS CRÍTICOS PENDIENTES**\n\n"
        message += f"Se detectaron {len(pending)} cambios que requieren aprobación:\n"
        message += f"- 🔴 Críticos: {critical_count}\n"
        message += f"- 🟠 Errores: {error_count}\n\n"

        for change in pending[:5]:  # Mostrar primeros 5
            message += f"• `{change.file_path}`: {change.description}\n"

        if len(pending) > 5:
            message += f"\n_...y {len(pending) - 5} más_"

        message += "\n\n**Acciones:**\n"
        message += "• [APROBAR TODOS] - Aplica el lote y pausa Sentinel temporalmente\n"
        message += "• [RECHAZAR TODOS] - Descarta los cambios\n"
        message += "• [VER DETALLES] - Revisa cada cambio individualmente"

        # Enviar notificación
        from app.dispatcher import notify
        await notify(message, level="critical", source="cerebro")

        # Emitir evento para dashboard
        from app.sockets import emit_agent_event
        await emit_agent_event({
            "source": "cerebro",
            "type": "changes_pending_approval",
            "severity": "critical",
            "payload": {
                "count": len(pending),
                "critical_count": critical_count,
                "error_count": error_count,
                "changes": [c.to_dict() for c in pending[:10]],
            }
        })

        self._last_notification = now
        logger.info(f"🔔 Notificación enviada: {len(pending)} cambios pendientes")

    async def approve_change(self, change_id: str) -> bool:
        """
        Aprueba un cambio individual.

        Args:
            change_id: ID del cambio

        Returns:
            bool: True si se aprobó exitosamente
        """
        if change_id not in self._pending_changes:
            logger.error(f"❌ Cambio {change_id} no encontrado")
            return False

        change = self._pending_changes[change_id]
        change.status = ChangeStatus.APPROVED
        change.approved_at = datetime.utcnow()

        # Mover a batch aprobado
        self._approved_batch.append(change)
        del self._pending_changes[change_id]

        logger.info(f"✅ Cambio aprobado: {change_id} ({change.file_path})")
        return True

    async def reject_change(self, change_id: str, continue_pipeline: bool = False) -> bool:
        """
        Rechaza un cambio individual.

        Args:
            change_id: ID del cambio
            continue_pipeline: Si True, Cerebro evalúa si continuar con Architect/Warden

        Returns:
            bool: True si se rechazó exitosamente
        """
        if change_id not in self._pending_changes:
            logger.error(f"❌ Cambio {change_id} no encontrado")
            return False

        change = self._pending_changes[change_id]
        change.status = ChangeStatus.REJECTED

        # Registrar feedback negativo en ContextDB
        if self._orchestrator:
            self._orchestrator.context_db.record_decision_outcome(
                event_id=change.event_id,
                outcome_type="false_positive",
                file_path=change.file_path,
                outcome_details="Usuario rechazó cambio sugerido",
            )

            # Si continue_pipeline=True, evaluar si se debe continuar con Architect/Warden
            if continue_pipeline:
                await self._evaluate_rejection_pipeline(change)

        del self._pending_changes[change_id]

        logger.info(f"❌ Cambio rechazado: {change_id} ({change.file_path})")
        return True

    async def _evaluate_rejection_pipeline(self, change: PendingChange):
        """
        Evalúa si se debe continuar el pipeline tras un rechazo.
        Usa el Decision Engine para determinar si Architect/Warden deben analizar el archivo.
        """
        try:
            from app.decision_engine import DecisionEngine
            from app.sockets import emit_agent_event

            # Construir evento sintético para evaluación
            rejection_event = {
                "source": "human",
                "type": "change_rejected",
                "severity": change.severity,
                "payload": {
                    "file": change.file_path,
                    "change_id": change.id,
                    "original_event_id": change.event_id,
                    "rejection_reason": "Usuario no está de acuerdo con la sugerencia",
                }
            }

            # Obtener Decision Engine y evaluar
            if hasattr(self._orchestrator, 'decision_engine'):
                decision = await self._orchestrator.decision_engine.evaluate(
                    rejection_event,
                    context={"file_path": change.file_path}
                )

                # Si la decisión incluye CHAIN, disparar a Architect/Warden
                from app.decision_engine import DecisionAction
                if DecisionAction.CHAIN in decision.actions and decision.target_agents:
                    logger.info(f"🔗 Rechazo en {change.file_path}: continuando pipeline a {decision.target_agents}")

                    for agent in decision.target_agents:
                        await emit_agent_event({
                            "source": "cerebro",
                            "type": f"pipeline_chain_to_{agent}",
                            "severity": "info",
                            "payload": {
                                "file": change.file_path,
                                "reason": "Evaluación post-rechazo",
                                "original_change_id": change.id,
                            }
                        })
        except Exception as exc:
            logger.warning(f"⚠️ Error evaluando pipeline post-rechazo: {exc}")

    async def approve_all_pending(self) -> int:
        """
        Aprueba todos los cambios pendientes.

        Returns:
            int: Cantidad de cambios aprobados
        """
        approved_count = 0
        for change_id in list(self._pending_changes.keys()):
            if await self.approve_change(change_id):
                approved_count += 1

        logger.info(f"✅ {approved_count} cambios aprobados en lote")
        return approved_count

    async def reject_all_pending(self) -> int:
        """
        Rechaza todos los cambios pendientes.

        Returns:
            int: Cantidad de cambios rechazados
        """
        rejected_count = 0
        for change_id in list(self._pending_changes.keys()):
            if await self.reject_change(change_id):
                rejected_count += 1

        logger.info(f"❌ {rejected_count} cambios rechazados en lote")
        return rejected_count

    async def apply_approved_changes(self) -> Dict[str, Any]:
        """
        Aplica el batch de cambios aprobados.
        Flujo:
        1. Pausa Sentinel
        2. Aplica cambios uno por uno
        3. Reactiva Sentinel
        4. Notifica resultado

        Returns:
            Dict: Resultado de la aplicación
        """
        if not self._approved_batch:
            return {"status": "skipped", "reason": "No hay cambios aprobados"}

        async with self._apply_lock:
            results = []
            sentinel_was_paused = False

            try:
                # 1. Pausar Sentinel
                logger.info(f"⏸️ Pausando Sentinel para aplicar {len(self._approved_batch)} cambios")
                sentinel_was_paused = await self._pause_sentinel()

                # 2. Aplicar cambios
                for change in self._approved_batch:
                    result = await self._apply_single_change(change)
                    results.append({
                        "change_id": change.id,
                        "file_path": change.file_path,
                        "status": result["status"],
                        "message": result.get("message", ""),
                        # Campos adicionales de trazabilidad
                        "branch": result.get("branch"),
                        "files_modified": result.get("files_modified", 0),
                        "modified_files": result.get("modified_files", []),
                        "suggested_count": result.get("suggested_count", 0),
                        "suggested_files": result.get("suggested_files", []),
                    })

                    # Actualizar estado
                    if result["status"] == "success":
                        change.status = ChangeStatus.APPLIED
                        change.applied_at = datetime.utcnow()
                    else:
                        change.status = ChangeStatus.FAILED

                # 3. Reactivar Sentinel
                if sentinel_was_paused:
                    await self._resume_sentinel()

                # 4. Notificar resultado
                await self._notify_application_result(results)

                # 5. Limpiar batch
                self._approved_batch.clear()

                success_count = sum(1 for r in results if r["status"] == "success")
                logger.info(f"✅ {success_count}/{len(results)} cambios aplicados")

                return {
                    "status": "completed",
                    "total": len(results),
                    "success": success_count,
                    "failed": len(results) - success_count,
                    "results": results,
                }

            except Exception as e:
                logger.exception(f"❌ Error aplicando cambios: {e}")

                # Reintentar reactivar Sentinel si falló
                if sentinel_was_paused:
                    await self._resume_sentinel()

                return {
                    "status": "failed",
                    "error": str(e),
                }

    async def _pause_sentinel(self) -> bool:
        """Pausa el monitoreo de Sentinel"""
        if not self._orchestrator:
            logger.warning("⚠️ No hay orchestrator para pausar Sentinel")
            return False

        try:
            from app.dispatcher import send_command
            from app.models import OrchestratorCommand

            ack = await send_command(
                "sentinel",
                OrchestratorCommand(
                    action="monitor/pause",
                    options={"reason": "applying_approved_changes"}
                )
            )

            self._sentinel_paused = True
            logger.info(f"⏸️ Sentinel pausado: {ack}")
            return True

        except Exception as e:
            logger.error(f"❌ Error pausando Sentinel: {e}")
            return False

    async def _resume_sentinel(self) -> bool:
        """Reactiva el monitoreo de Sentinel"""
        if not self._orchestrator:
            return False

        try:
            from app.dispatcher import send_command
            from app.models import OrchestratorCommand

            # Reiniciar monitoreo con el proyecto activo
            project_path = self._orchestrator.workspace_root
            if self._orchestrator.active_project:
                import os
                project_path = os.path.join(
                    self._orchestrator.workspace_root,
                    self._orchestrator.active_project
                ).replace("\\", "/")

            # Enviar comando monitor SIEMPRE al Core, no al ADK
            ack = await send_command(
                "sentinel_core",
                OrchestratorCommand(action="monitor", target=project_path)
            )

            self._sentinel_paused = False
            logger.info(f"▶️ Sentinel reactivado: {ack}")
            return True

        except Exception as e:
            logger.error(f"❌ Error reactivando Sentinel: {e}")
            return False

    async def _apply_single_change(self, change: PendingChange) -> Dict[str, Any]:
        """
        Aplica un cambio individual enviándolo al Executor para ejecución vía Aider.

        Args:
            change: Cambio a aplicar

        Returns:
            Dict: Resultado de la aplicación con info de archivos modificados, build, etc.
        """
        logger.info(f"🔧 Aplicando cambio: {change.id} ({change.file_path})")

        try:
            # Construir el comando para el Executor
            instruction = f"""You are the Skrymir Executor Auto-Fix Agent.
An upstream analysis agent has detected an issue in the codebase.

=== ISSUE DESCRIPTION ===
{change.description}

=== RECOMMENDATION ===
{change.recommendation or 'Fix the identified issue'}

YOUR TASK:
Implement the exact code changes to resolve the identified issue.
Focus strictly on the mentioned problem. Do not refactor unrelated code.
"""

            fix_cmd = OrchestratorCommand(
                action="autofix",
                target=change.file_path,
                options={
                    "instruction": instruction,
                    "branch_prefix": "skrymir-fix/",
                    "provider": "ollama",
                    "model": "qwen3:8b",
                }
            )

            # Emitir evento: enviando a Executor
            await emit_agent_event({
                "source": "cerebro",
                "type": "executor_command_sent",
                "severity": "info",
                "payload": {
                    "change_id": change.id,
                    "file_path": change.file_path,
                    "target": "ejecutor",
                    "action": "autofix",
                }
            })

            logger.info(f"  ↳ Enviando a Executor: autofix para {change.file_path}")

            # Enviar comando al Executor
            ack = await send_command("ejecutor", fix_cmd)

            # Procesar resultado
            fix_result = {}
            if ack and isinstance(ack, dict):
                fix_result = ack.get("data", {}).get("result", {}) or {}

            fix_validated = fix_result.get("fix_validated")
            branch = fix_result.get("branch", "unknown")
            modified_files = fix_result.get("modified_files", [])
            suggested_files = fix_result.get("suggested_files", [])
            build_exit_code = fix_result.get("build_exit_code")
            build_tool = fix_result.get("build_tool")

            # Emitir evento de resultado
            await emit_agent_event({
                "source": "cerebro",
                "type": "change_applied",
                "severity": "info" if fix_validated is True else "warning",
                "payload": {
                    "change_id": change.id,
                    "file_path": change.file_path,
                    "fix_validated": fix_validated,
                    "branch": branch,
                    "build_exit_code": build_exit_code,
                    "build_tool": build_tool,
                    "files_modified": len(modified_files),
                    "modified_files": modified_files,
                    "suggested_count": len(suggested_files),
                    "suggested_files": suggested_files,
                }
            })

            if fix_validated is True:
                logger.info(f"  ✅ Cambio aplicado exitosamente en rama {branch}")
                return {
                    "status": "success",
                    "message": f"Cambio aplicado exitosamente en {change.file_path}",
                    "branch": branch,
                    "files_modified": len(modified_files),
                    "modified_files": modified_files,
                }
            elif fix_validated is False:
                logger.warning(f"  ⚠️ Build falló para {change.file_path}")
                return {
                    "status": "failed",
                    "message": f"Build falló al aplicar cambio en {change.file_path}. Archivos .suggested creados.",
                    "branch": branch,
                    "build_exit_code": build_exit_code,
                    "suggested_count": len(suggested_files),
                    "suggested_files": suggested_files,
                }
            else:
                logger.info(f"  ℹ️ Cambio aplicado sin validación de build")
                return {
                    "status": "partial",
                    "message": f"Cambio aplicado en {change.file_path} (sin build tool detectado)",
                    "branch": branch,
                    "files_modified": len(modified_files),
                }

        except Exception as e:
            logger.error(f"  ❌ Error aplicando cambio: {e}")
            return {
                "status": "error",
                "message": f"Error aplicando cambio: {str(e)}",
            }

    async def _notify_application_result(self, results: List[Dict]):
        """Notifica el resultado de la aplicación de cambios con detalle de archivos"""
        success_count = sum(1 for r in results if r["status"] == "success")
        failed_count = sum(1 for r in results if r["status"] == "failed")
        error_count = sum(1 for r in results if r["status"] == "error")
        total = len(results)

        # Contar archivos modificados y suggested
        total_files_modified = sum(r.get("files_modified", 0) for r in results)
        total_suggested = sum(r.get("suggested_count", 0) for r in results)

        message = f"{'✅' if success_count == total else '⚠️'} **CAMBIOS APLICADOS**\n\n"
        message += f"Resultado: {success_count}/{total} exitosos\n"
        message += f"Archivos modificados: {total_files_modified}\n"
        if total_suggested > 0:
            message += f"⚠️ Archivos .suggested creados: {total_suggested}\n"
        message += "\n"

        # Mostrar exitosos con detalle
        succeeded = [r for r in results if r["status"] == "success"]
        if succeeded:
            message += "**Exitosos:**\n"
            for r in succeeded:
                branch = r.get('branch', 'unknown')
                files = r.get('files_modified', 0)
                message += f"• `{r.get('file_path', 'unknown')}`"
                if branch != 'unknown':
                    message += f" → rama `{branch}`"
                if files > 0:
                    message += f" ({files} archivos)"
                message += "\n"
            message += "\n"

        # Mostrar fallidos
        failed = [r for r in results if r["status"] != "success"]
        if failed:
            message += "**Fallidos:**\n"
            for r in failed:
                message += f"• `{r.get('file_path', 'unknown')}`: {r.get('message', 'Error desconocido')}\n"
                if r.get('suggested_count', 0) > 0:
                    suggested = r.get('suggested_files', [])
                    message += f"  📝 Archivos .suggested creados: {', '.join(suggested[:3])}\n"

        from app.dispatcher import notify
        await notify(message, level="info" if success_count == total else "warning", source="cerebro")

        # Emitir evento para dashboard con info completa
        from app.sockets import emit_agent_event
        await emit_agent_event({
            "source": "cerebro",
            "type": "changes_applied",
            "severity": "info" if success_count == total else "warning",
            "payload": {
                "total": total,
                "success": success_count,
                "failed": failed_count,
                "error": error_count,
                "files_modified": total_files_modified,
                "suggested_count": total_suggested,
                "results": results,
            }
        })

    # ─────────────────────────────────────────────────────────────────────────
    # MÉTODOS DE CONSULTA
    # ─────────────────────────────────────────────────────────────────────────

    def get_pending_changes(self) -> List[Dict]:
        """Retorna lista de cambios pendientes"""
        return [c.to_dict() for c in self._pending_changes.values()]

    def get_approved_batch(self) -> List[Dict]:
        """Retorna lista de cambios aprobados pendientes de aplicar"""
        return [c.to_dict() for c in self._approved_batch]

    def is_sentinel_paused(self) -> bool:
        """Retorna estado de Sentinel"""
        return self._sentinel_paused

    def get_stats(self) -> Dict[str, Any]:
        """Retorna estadísticas del ChangeManager"""
        return {
            "pending_count": len(self._pending_changes),
            "approved_batch_count": len(self._approved_batch),
            "sentinel_paused": self._sentinel_paused,
            "last_notification": self._last_notification.isoformat() if self._last_notification else None,
        }


# Instancia global
_change_manager: Optional[ChangeManager] = None


def get_change_manager() -> ChangeManager:
    """Obtiene la instancia global de ChangeManager"""
    global _change_manager
    if _change_manager is None:
        _change_manager = ChangeManager()
    return _change_manager
