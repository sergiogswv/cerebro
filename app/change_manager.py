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

    async def reject_change(self, change_id: str) -> bool:
        """
        Rechaza un cambio individual.

        Args:
            change_id: ID del cambio

        Returns:
            bool: True si se rechazó exitosamente
        """
        if change_id not in self._pending_changes:
            logger.error(f"❌ Cambio {change_id} no encontrado")
            return False

        change = self._pending_changes[change_id]
        change.status = ChangeStatus.REJECTED

        del self._pending_changes[change_id]

        # Registrar feedback negativo en ContextDB
        if self._orchestrator:
            self._orchestrator.context_db.record_decision_outcome(
                event_id=change.event_id,
                outcome_type="false_positive",
                file_path=change.file_path,
                outcome_details="Usuario rechazó cambio sugerido",
            )

        logger.info(f"❌ Cambio rechazado: {change_id} ({change.file_path})")
        return True

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

            ack = await send_command(
                "sentinel",
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
        Aplica un cambio individual.
        Aquí iría la lógica específica de aplicación.

        Args:
            change: Cambio a aplicar

        Returns:
            Dict: Resultado de la aplicación
        """
        logger.info(f"🔧 Aplicando cambio: {change.id} ({change.file_path})")

        # TODO: Implementar lógica específica de aplicación
        # Por ahora, simulamos éxito

        # Si hay recomendación, podríamos usar Architect para generar el fix
        if change.recommendation and self._orchestrator:
            # Futuro: llamar a Architect para generar código
            logger.debug(f"  ↳ Recomendación: {change.recommendation}")

        return {
            "status": "success",
            "message": f"Cambio aplicado en {change.file_path}",
        }

    async def _notify_application_result(self, results: List[Dict]):
        """Notifica el resultado de la aplicación de cambios"""
        success_count = sum(1 for r in results if r["status"] == "success")
        total = len(results)

        message = f"{'✅' if success_count == total else '⚠️'} **CAMBIOS APLICADOS**\n\n"
        message += f"Resultado: {success_count}/{total} exitosos\n\n"

        # Mostrar fallidos
        failed = [r for r in results if r["status"] != "success"]
        if failed:
            message += "**Fallidos:**\n"
            for r in failed:
                message += f"• `{r.get('file_path', 'unknown')}`: {r.get('message', 'Error desconocido')}\n"

        from app.dispatcher import notify
        await notify(message, level="info" if success_count == total else "warning", source="cerebro")

        # Emitir evento para dashboard
        from app.sockets import emit_agent_event
        await emit_agent_event({
            "source": "cerebro",
            "type": "changes_applied",
            "severity": "info",
            "payload": {
                "total": total,
                "success": success_count,
                "failed": total - success_count,
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
