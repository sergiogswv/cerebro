import logging
import httpx
from app.models import OrchestratorCommand, NotifyRequest, NotifyLevel
from app.config import get_settings

logger = logging.getLogger("cerebro.dispatcher")
settings = get_settings()

# Mapa de agentes a sus URLs
AGENT_URLS: dict[str, str] = {
    "sentinel": settings.sentinel_url,
    "architect": settings.architect_url,
    "warden": settings.warden_url,
    "ejecutor": settings.executor_url,
}


async def send_command(agent: str, command: OrchestratorCommand) -> dict:
    """
    Envía un comando HTTP a un agente específico.
    Retorna el CommandAck o un dict de error.
    """
    url = AGENT_URLS.get(agent)
    if not url:
        logger.error(f"Agente desconocido: {agent}")
        return {"status": "rejected", "error": f"Agente '{agent}' no registrado"}

    endpoint = f"{url}/command"
    payload = command.model_dump(mode="json")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            logger.info(f"→ [{agent.upper()}] {command.action} | request_id={command.request_id}")
            response = await client.post(endpoint, json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.ConnectError:
        logger.warning(f"✗ [{agent.upper()}] No disponible en {endpoint}")
        return {"status": "rejected", "error": f"Agente '{agent}' no disponible"}
    except httpx.HTTPStatusError as e:
        logger.error(f"✗ [{agent.upper()}] HTTP {e.response.status_code}")
        return {"status": "rejected", "error": str(e)}
    except Exception as e:
        logger.exception(f"✗ [{agent.upper()}] Error inesperado")
        return {"status": "rejected", "error": str(e)}


async def send_raw_command(agent: str, payload: dict) -> dict:
    """
    Proxy transparente: envía un dict JSON al agente sin transformarlo.
    Cada agente define su propio contrato.
    """
    url = AGENT_URLS.get(agent)
    if not url:
        logger.error(f"Agente desconocido: {agent}")
        return {"status": "rejected", "error": f"Agente '{agent}' no registrado"}

    endpoint = f"{url}/command"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            logger.info(f"→ [{agent.upper()}] raw proxy | action={payload.get('action')}")
            response = await client.post(endpoint, json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.ConnectError:
        logger.warning(f"✗ [{agent.upper()}] No disponible en {endpoint}")
        return {"status": "rejected", "error": f"Agente '{agent}' no disponible"}
    except httpx.HTTPStatusError as e:
        logger.error(f"✗ [{agent.upper()}] HTTP {e.response.status_code}")
        return {"status": "rejected", "error": str(e)}
    except Exception as e:
        logger.exception(f"✗ [{agent.upper()}] Error inesperado")
        return {"status": "rejected", "error": str(e)}


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
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(endpoint, json=payload.model_dump(mode="json"))
            response.raise_for_status()
            return True
    except httpx.ConnectError:
        logger.warning(f"✗ [NOTIFIER] No disponible en {endpoint}")
        return False
    except Exception as e:
        logger.error(f"✗ [NOTIFIER] Error: {e}")
        return False
