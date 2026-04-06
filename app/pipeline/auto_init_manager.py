import asyncio
import logging
from typing import List

from app.pipeline.models import AutoInitConfig, ServiceConfig

logger = logging.getLogger("cerebro.pipeline")


class AutoInitManager:
    """Manages automatic initialization of services on startup."""

    def __init__(self, config: AutoInitConfig):
        self.config = config
        self.started_services: List[str] = []

    async def initialize(self) -> dict:
        """
        Initialize all enabled services according to priority order.

        Returns:
            Dict with status of each service
        """
        if not self.config.enabled:
            logger.info("Auto-init disabled, skipping")
            return {"status": "skipped"}

        results = {}

        # Sort by priority (lower number = higher priority)
        services = sorted(self.config.services, key=lambda s: s.priority)

        for service in services:
            if not service.enabled:
                results[service.agent] = {"status": "disabled"}
                continue

            # Apply startup delay
            if service.startup_delay_seconds > 0:
                logger.info(f"Waiting {service.startup_delay_seconds}s before starting {service.agent}")
                await asyncio.sleep(service.startup_delay_seconds)

            result = await self._start_service(service)
            results[service.agent] = result

        return results

    async def _start_service(self, service: ServiceConfig) -> dict:
        """Start a single service."""
        from app.config import get_settings
        from app.dispatcher import send_command
        from app.models import OrchestratorCommand

        settings = get_settings()

        # Determine URL based on mode
        if service.agent == "sentinel":
            url = settings.sentinel_adk_url if service.mode == "adk" else settings.sentinel_url
        elif service.agent == "warden":
            url = settings.warden_adk_url if service.mode == "adk" else settings.warden_url
        elif service.agent == "architect":
            url = settings.architect_adk_url if service.mode == "adk" else settings.architect_url
        else:
            return {"status": "error", "error": f"Unknown agent: {service.agent}"}

        # Check if already running
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{url}/health")
                if response.status_code == 200:
                    logger.info(f"Service {service.agent} already running")
                    self.started_services.append(service.agent)
                    return {"status": "already_running"}
        except Exception:
            pass  # Not running, proceed to start

        # Send start command via executor
        try:
            service_key = f"{service.agent}_{service.mode}" if service.mode == "adk" else service.agent
            ack = await send_command(
                "ejecutor",
                OrchestratorCommand(action="open", service=service_key)
            )

            if ack.get("status") == "ok":
                self.started_services.append(service.agent)
                logger.info(f"Started {service.agent} in {service.mode} mode")
                return {"status": "started", "mode": service.mode}
            else:
                error = ack.get("error", "Unknown error")
                logger.error(f"Failed to start {service.agent}: {error}")
                return {"status": "error", "error": error}

        except Exception as e:
            logger.error(f"Exception starting {service.agent}: {e}")
            return {"status": "error", "error": str(e)}
