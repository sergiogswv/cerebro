import json
import logging
from pathlib import Path
from typing import Optional

from app.pipeline.models import PipelineConfig, ServiceConfig

logger = logging.getLogger("cerebro.pipeline")


class PipelineConfigManager:
    """Manages pipeline configuration persistence."""

    _instance = None
    _config: Optional[PipelineConfig] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def get_instance(cls) -> "PipelineConfigManager":
        return cls()

    def __init__(self):
        if self._config is not None:
            return
        self._config_path = Path.home() / ".cerebro" / "pipeline.config.json"
        self._load_config()

    def _load_config(self) -> None:
        """Load config from disk or create defaults."""
        if self._config_path.exists():
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._config = PipelineConfig(**data)
                logger.info(f"Loaded pipeline config from {self._config_path}")
                return
            except Exception as e:
                logger.warning(f"Failed to load pipeline config: {e}, using defaults")

        self._config = self._default_config()
        self._save_config()

    def _default_config(self) -> PipelineConfig:
        """Create default configuration."""
        return PipelineConfig(
            auto_init={
                "enabled": False,
                "services": [
                    ServiceConfig(agent="sentinel", mode="adk", enabled=False, priority=1),
                    ServiceConfig(agent="warden", mode="core", enabled=False, priority=2),
                    ServiceConfig(agent="architect", mode="adk", enabled=False, priority=3),
                ]
            }
        )

    def _save_config(self) -> None:
        """Save config to disk."""
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._config_path, "w", encoding="utf-8") as f:
            json.dump(self._config.model_dump(), f, indent=2, default=str)

    def get_config(self) -> PipelineConfig:
        """Get current configuration."""
        return self._config

    def update_config(self, config: PipelineConfig) -> None:
        """Update and persist configuration."""
        self._config = config
        self._save_config()
        logger.info("Pipeline configuration updated")

    def update_partial(self, **updates) -> None:
        """Partial update of configuration."""
        current = self._config.model_dump()
        current.update(updates)
        self._config = PipelineConfig(**current)
        self._save_config()
