from enum import Enum
from typing import Any
from datetime import datetime, timezone
from pydantic import BaseModel, Field
import uuid


# ─── Enums ────────────────────────────────────────────────────────────────────

class AgentSource(str, Enum):
    sentinel = "sentinel"
    architect = "architect"
    warden = "warden"
    ejecutor = "ejecutor"
    executor = "executor"  # alias para compatibilidad con el Executor


class Severity(str, Enum):
    info = "info"
    warning = "warning"
    error = "error"
    critical = "critical"


class NotifyLevel(str, Enum):
    info = "info"
    warning = "warning"
    error = "error"
    critical = "critical"


# ─── Eventos entrantes (Agente → Orquestador) ─────────────────────────────────

class AgentEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: AgentSource
    type: str
    severity: Severity
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = Field(default_factory=dict)


# ─── Comandos salientes (Orquestador → Agente) ────────────────────────────────

class OrchestratorCommand(BaseModel):
    action: str
    service: str | None = None  # Requerido para action="run"
    target: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


class CommandAck(BaseModel):
    request_id: str | None = None
    status: str  # "accepted" | "rejected" | "completed"
    result: dict[str, Any] | None = None
    error: str | None = None


# ─── Notificaciones (Orquestador → Notificador) ───────────────────────────────

class NotifyRequest(BaseModel):
    message: str
    level: NotifyLevel = NotifyLevel.info
    source: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ─── Respuesta genérica de la API ────────────────────────────────────────────

class ApiResponse(BaseModel):
    ok: bool = True
    message: str = "ok"
    data: Any | None = None
