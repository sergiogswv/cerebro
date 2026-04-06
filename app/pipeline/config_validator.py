"""Configuration consistency validation.

Detects conflicts between pipeline configuration and individual agent configs.
"""

import logging
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass
from pydantic import BaseModel

from app.pipeline.models import PipelineConfig, ServiceConfig

logger = logging.getLogger("cerebro.pipeline")


class ConfigConflict(BaseModel):
    """Represents a configuration conflict."""
    agent: str
    severity: str  # 'error', 'warning', 'info'
    message: str
    pipeline_value: Any
    agent_value: Any
    suggestion: str


class ConfigValidationReport(BaseModel):
    """Report of configuration validation."""
    valid: bool
    conflicts: List[ConfigConflict]
    warnings: List[ConfigConflict]


class ConfigValidator:
    """
    Validates consistency between pipeline and agent configurations.

    Detects issues like:
    - Pipeline configura ADK pero agent tiene core en su config
    - Servicio habilitado en pipeline pero agente no está instalado
    - Timeout de pipeline menor que el timeout interno del agente
    """

    def __init__(self):
        self._agent_config_loaders = {
            "sentinel": self._load_sentinel_config,
            "warden": self._load_warden_config,
            "architect": self._load_architect_config,
        }

    def validate(self, pipeline_config: PipelineConfig, project_path: str) -> ConfigValidationReport:
        """
        Validate pipeline config against agent configs.

        Args:
            pipeline_config: The pipeline configuration
            project_path: Path to the project being analyzed

        Returns:
            ConfigValidationReport with conflicts and warnings
        """
        conflicts = []
        warnings = []

        for service in pipeline_config.auto_init.services:
            if not service.enabled:
                continue

            agent_conflicts = self._validate_agent(service, project_path)
            conflicts.extend([c for c in agent_conflicts if c.severity == "error"])
            warnings.extend([c for c in agent_conflicts if c.severity in ("warning", "info")])

        return ConfigValidationReport(
            valid=len(conflicts) == 0,
            conflicts=conflicts,
            warnings=warnings
        )

    def _validate_agent(
        self,
        service: ServiceConfig,
        project_path: str
    ) -> List[ConfigConflict]:
        """Validate a single agent's configuration."""
        conflicts = []

        # Check if agent config file exists and extract mode
        agent_config = self._get_agent_config(service.agent, project_path)

        if agent_config:
            # Check mode mismatch
            agent_mode = agent_config.get("mode", "core")
            if agent_mode != service.mode:
                conflicts.append(ConfigConflict(
                    agent=service.agent,
                    severity="warning",
                    message=f"Mode mismatch: pipeline configura '{service.mode}' pero {service.agent} configura '{agent_mode}'",
                    pipeline_value=service.mode,
                    agent_value=agent_mode,
                    suggestion=f"Alinea ambos a '{service.mode}' o '{agent_mode}'"
                ))

            # Check for conflicting timeout settings
            agent_timeout = agent_config.get("timeout_seconds")
            if agent_timeout and isinstance(agent_timeout, (int, float)):
                if agent_timeout > 300:  # Pipeline default
                    conflicts.append(ConfigConflict(
                        agent=service.agent,
                        severity="info",
                        message=f"Agente {service.agent} tiene timeout mayor al pipeline default",
                        pipeline_value="300s (default)",
                        agent_value=f"{agent_timeout}s",
                        suggestion="Considera aumentar el timeout del pipeline"
                    ))

            # Check LLM provider consistency
            agent_llm = agent_config.get("llm") or agent_config.get("models", {}).get("primary")
            if agent_llm:
                provider = agent_llm.get("provider")
                if provider == "ollama":
                    # Check if Ollama is actually available
                    if not self._check_ollama_available(agent_llm.get("base_url")):
                        conflicts.append(ConfigConflict(
                            agent=service.agent,
                            severity="warning",
                            message=f"{service.agent} configurado para Ollama pero no parece estar corriendo",
                            pipeline_value=None,
                            agent_value=agent_llm.get("base_url", "localhost:11434"),
                            suggestion="Verifica que Ollama esté corriendo o cambia el provider"
                        ))

        return conflicts

    def _get_agent_config(self, agent: str, project_path: str) -> Optional[Dict]:
        """Load agent configuration from its config file."""
        loader = self._agent_config_loaders.get(agent)
        if loader:
            return loader(project_path)
        return None

    def _load_sentinel_config(self, project_path: str) -> Optional[Dict]:
        """Load Sentinel config from .sentinelrc.toml."""
        import tomllib

        config_paths = [
            Path(project_path) / ".sentinelrc.toml",
            Path.home() / ".sentinelrc.toml",
        ]

        for path in config_paths:
            if path.exists():
                try:
                    with open(path, "rb") as f:
                        config = tomllib.load(f)
                        logger.debug(f"Loaded Sentinel config from {path}")
                        return config
                except Exception as e:
                    logger.warning(f"Failed to load Sentinel config from {path}: {e}")

        return None

    def _load_warden_config(self, project_path: str) -> Optional[Dict]:
        """Load Warden config if exists."""
        # Warden typically doesn't have its own config file
        # It uses Cerebro's configuration
        return None

    def _load_architect_config(self, project_path: str) -> Optional[Dict]:
        """Load Architect config from .architect.json or similar."""
        config_path = Path(project_path) / ".architect.json"
        if config_path.exists():
            try:
                import json
                with open(config_path, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load Architect config: {e}")
        return None

    def _check_ollama_available(self, base_url: str) -> bool:
        """Check if Ollama is actually running."""
        import urllib.request
        import socket

        try:
            url = base_url or "http://localhost:11434"
            req = urllib.request.Request(f"{url}/api/tags", method="GET")
            req.add_header("Content-Type", "application/json")

            with urllib.request.urlopen(req, timeout=2) as response:
                return response.status == 200
        except (urllib.error.URLError, socket.timeout, ConnectionRefusedError):
            return False
        except Exception:
            return False


class ConfigConsistencyChecker:
    """
    Background checker that warns about config inconsistencies.

    Can be run periodically or on startup.
    """

    def __init__(self, pipeline_config: PipelineConfig):
        self.pipeline_config = pipeline_config
        self.validator = ConfigValidator()
        self._last_check: Optional[ConfigValidationReport] = None

    def check_now(self, project_path: str) -> ConfigValidationReport:
        """Run validation immediately."""
        self._last_check = self.validator.validate(self.pipeline_config, project_path)

        # Log any issues found
        if self._last_check.conflicts:
            for conflict in self._last_check.conflicts:
                logger.error(f"Config conflict: {conflict.agent} - {conflict.message}")

        if self._last_check.warnings:
            for warning in self._last_check.warnings:
                logger.warning(f"Config warning: {warning.agent} - {warning.message}")

        if self._last_check.valid:
            logger.info("Configuration validation passed")

        return self._last_check

    def get_cached_report(self) -> Optional[ConfigValidationReport]:
        """Get the last validation report."""
        return self._last_check

    def has_critical_issues(self) -> bool:
        """Check if there are any critical (error-level) issues."""
        if not self._last_check:
            return False
        return any(c.severity == "error" for c in self._last_check.conflicts)
