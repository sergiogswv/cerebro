import logging
import httpx
import asyncio
from app.models import OrchestratorCommand, NotifyRequest, NotifyLevel
from app.config import get_settings, SentinelMode, WardenMode, ArchitectMode

logger = logging.getLogger("cerebro.dispatcher")
settings = get_settings()

# Mapa de agentes a sus URLs.
def _resolve_warden_url() -> str:
    if settings.warden_mode == WardenMode.ADK.value:
        logger.info(f"🔱 Warden en modo ADK → {settings.warden_adk_url}")
        return settings.warden_adk_url
    return settings.warden_url

def _resolve_architect_url() -> str:
    if settings.architect_mode == ArchitectMode.ADK.value:
        logger.info(f"🏛️ Architect en modo ADK → {settings.architect_adk_url}")
        return settings.architect_adk_url
    return settings.architect_url


def _resolve_sentinel_url() -> str:
    if settings.sentinel_mode == SentinelMode.ADK_ONLY.value:
        logger.info(f"🛡️ Sentinel en modo ADK → {settings.sentinel_adk_url}")
        return settings.sentinel_adk_url
    return settings.sentinel_url


AGENT_URLS: dict[str, str] = {
    "sentinel":      _resolve_sentinel_url(),  # Resuelve según sentinel_mode (core o adk)
    "sentinel_core": settings.sentinel_url,    # Siempre apunta al Core (para comandos pro/monitor)
    "sentinel_adk":  settings.sentinel_adk_url, # Siempre apunta al ADK
    "architect":     _resolve_architect_url(),
    "architect_adk": settings.architect_adk_url,
    "warden":        _resolve_warden_url(),
    "warden_adk":    settings.warden_adk_url,
    "ejecutor":      settings.executor_url,
}


import time
from typing import Dict

class AgentCircuitBreaker:
    """
    Circuit breaker per-agent. Después de N fallos consecutivos,
    deja de intentar por X segundos.
    """
    def __init__(self, failure_threshold: int = 3, recovery_timeout: float = 60.0):
        self._failures: Dict[str, int] = {}
        self._open_until: Dict[str, float] = {}
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout

    def is_open(self, agent: str) -> bool:
        if agent in self._open_until:
            if time.time() < self._open_until[agent]:
                return True  # Circuito abierto, saltar
            del self._open_until[agent]
            self._failures[agent] = 0
        return False

    def record_failure(self, agent: str):
        self._failures[agent] = self._failures.get(agent, 0) + 1
        if self._failures[agent] >= self.failure_threshold:
            self._open_until[agent] = time.time() + self.recovery_timeout
            logger.warning(f"⚡ Circuit breaker ABIERTO para {agent}")

    def record_success(self, agent: str):
        self._failures.pop(agent, None)
        self._open_until.pop(agent, None)


circuit_breaker = AgentCircuitBreaker()


async def send_command(agent: str, command: OrchestratorCommand, max_retries: int = 3, retry_delay: float = 0.5) -> dict:
    """
    Envía un comando HTTP a un agente específico con reintentos.
    Retorna el CommandAck o un dict de error.
    """
    url = AGENT_URLS.get(agent)
    if not url:
        logger.error(f"Agente desconocido: {agent}")
        return {"status": "rejected", "error": f"Agente '{agent}' no registrado"}

    if circuit_breaker.is_open(agent):
        logger.warning(f"⚡ Circuit breaker abierto para {agent}, saltando petición.")
        return {"status": "rejected", "error": f"Circuit breaker status OPEN for {agent}"}

    endpoint = f"{url}/command"
    payload = command.model_dump(mode="json")

    # Reintentos con backoff exponencial para errores de conexión
    last_error = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                logger.info(f"→ [{agent.upper()}] {command.action} | request_id={command.request_id} (intento {attempt + 1}/{max_retries})")
                response = await client.post(endpoint, json=payload)
                response.raise_for_status()
                if attempt > 0:
                    logger.info(f"✅ [{agent.upper()}] Comando exitoso después de {attempt + 1} intentos")
                circuit_breaker.record_success(agent)
                return response.json()
        except httpx.ConnectError as e:
            last_error = e
            logger.warning(f"✗ [{agent.upper()}] No disponible en {endpoint} (intento {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                wait_time = retry_delay * (2 ** attempt)  # Backoff exponencial: 0.5s, 1s, 2s
                logger.info(f"⏳ Reintentando en {wait_time}s...")
                await asyncio.sleep(wait_time)
        except httpx.HTTPStatusError as e:
            logger.error(f"✗ [{agent.upper()}] HTTP {e.response.status_code}")
            return {"status": "rejected", "error": str(e)}
        except Exception as e:
            logger.exception(f"✗ [{agent.upper()}] Error inesperado")
            circuit_breaker.record_failure(agent)
            return {"status": "rejected", "error": str(e)}

    circuit_breaker.record_failure(agent)
    logger.error(f"✗ [{agent.upper()}] Agotados {max_retries} reintentos")
    return {"status": "rejected", "error": f"Agente '{agent}' no disponible después de {max_retries} intentos"}


async def send_raw_command(agent: str, payload: dict, max_retries: int = 3, retry_delay: float = 0.5) -> dict:
    """
    Proxy transparente: envía un dict JSON al agente sin transformarlo.
    Cada agente define su propio contrato.
    """
    url = AGENT_URLS.get(agent)
    if not url:
        logger.error(f"Agente desconocido: {agent}")
        return {"status": "rejected", "error": f"Agente '{agent}' no registrado"}

    if circuit_breaker.is_open(agent):
        logger.warning(f"⚡ Circuit breaker abierto para {agent}, saltando petición.")
        return {"status": "rejected", "error": f"Circuit breaker status OPEN for {agent}"}

    endpoint = f"{url}/command"

    # Reintentos con backoff exponencial para errores de conexión
    last_error = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                logger.info(f"→ [{agent.upper()}] raw proxy | action={payload.get('action')} (intento {attempt + 1}/{max_retries})")
                response = await client.post(endpoint, json=payload)
                response.raise_for_status()
                if attempt > 0:
                    logger.info(f"✅ [{agent.upper()}] Comando exitoso después de {attempt + 1} intentos")
                circuit_breaker.record_success(agent)
                return response.json()
        except httpx.ConnectError as e:
            last_error = e
            logger.warning(f"✗ [{agent.upper()}] No disponible en {endpoint} (intento {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                wait_time = retry_delay * (2 ** attempt)  # Backoff exponencial: 0.5s, 1s, 2s
                logger.info(f"⏳ Reintentando en {wait_time}s...")
                await asyncio.sleep(wait_time)
        except httpx.HTTPStatusError as e:
            logger.error(f"✗ [{agent.upper()}] HTTP {e.response.status_code}")
            return {"status": "rejected", "error": str(e)}
        except Exception as e:
            logger.exception(f"✗ [{agent.upper()}] Error inesperado")
            circuit_breaker.record_failure(agent)
            return {"status": "rejected", "error": str(e)}

    circuit_breaker.record_failure(agent)
    logger.error(f"✗ [{agent.upper()}] Agotados {max_retries} reintentos")
    return {"status": "rejected", "error": f"Agente '{agent}' no disponible después de {max_retries} intentos"}


async def notify(message: str, level: NotifyLevel = NotifyLevel.info, source: str | None = None) -> bool:
    """
    Envía una notificación al Agente Notificador (Telegram).
    Retorna True si se entregó con éxito.
    """
    payload = NotifyRequest(message=message, level=level, source=source)
    endpoint = f"{settings.notifier_url}/notify"

    emoji = {"info": "ℹ️", "warning": "⚠️", "error": "❌", "critical": "🚨"}.get(level, "")
    logger.info(f"→ [NOTIFIER] {emoji} {level.upper()} | {message[:60]}...")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(endpoint, json=payload.model_dump(mode="json"))
            response.raise_for_status()
            return True
    except httpx.ConnectError:
        logger.warning(f"✗ [NOTIFIER] No disponible en {endpoint}")
        return False
    except Exception as e:
        logger.error(f"✗ [NOTIFIER] Error: {e}")
        return False
