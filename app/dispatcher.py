import logging
import httpx
import asyncio
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


async def send_command(agent: str, command: OrchestratorCommand, max_retries: int = 3, retry_delay: float = 0.5) -> dict:
    """
    Envía un comando HTTP a un agente específico con reintentos.
    Retorna el CommandAck o un dict de error.
    """
    url = AGENT_URLS.get(agent)
    if not url:
        logger.error(f"Agente desconocido: {agent}")
        return {"status": "rejected", "error": f"Agente '{agent}' no registrado"}

    endpoint = f"{url}/command"
    payload = command.model_dump(mode="json")

    # Reintentos con backoff exponencial para errores de conexión
    last_error = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                logger.info(f"→ [{agent.upper()}] {command.action} | request_id={command.request_id} (intento {attempt + 1}/{max_retries})")
                response = await client.post(endpoint, json=payload)
                response.raise_for_status()
                if attempt > 0:
                    logger.info(f"✅ [{agent.upper()}] Comando exitoso después de {attempt + 1} intentos")
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
            return {"status": "rejected", "error": str(e)}

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

    endpoint = f"{url}/command"

    # Reintentos con backoff exponencial para errores de conexión
    last_error = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                logger.info(f"→ [{agent.upper()}] raw proxy | action={payload.get('action')} (intento {attempt + 1}/{max_retries})")
                response = await client.post(endpoint, json=payload)
                response.raise_for_status()
                if attempt > 0:
                    logger.info(f"✅ [{agent.upper()}] Comando exitoso después de {attempt + 1} intentos")
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
            return {"status": "rejected", "error": str(e)}

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
