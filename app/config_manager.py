"""
Unified configuration manager for the Skrymir multi-agent system.

This module provides a singleton manager for configuration with environment
variable expansion, caching, and thread-safe operations.
"""

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from app.models.config import (
    AgentConfig,
    ArchitectConfig,
    LLMConfig,
    ProjectOverride,
    SentinelConfig,
    UnifiedConfig,
    WardenConfig,
)


class UnifiedConfigManager:
    """Singleton manager for unified configuration.

    This class provides thread-safe access to the global configuration
    with caching, environment variable expansion, and automatic file
    persistence.

    The configuration is stored at ~/.cerebro/global.config.json

    Example:
        >>> manager = UnifiedConfigManager.get_instance()
        >>> llm_config = manager.get_agent_llm_config("sentinel")
        >>> print(llm_config.model)
    """

    _instance: Optional["UnifiedConfigManager"] = None
    _lock: threading.Lock = threading.Lock()
    _init_lock: threading.Lock = threading.Lock()

    def __init__(self) -> None:
        """Initialize the config manager.

        Do not call directly - use get_instance() instead.
        """
        if not hasattr(self, "_initialized"):
            self._config_path: Path = Path.home() / ".cerebro" / "global.config.json"
            self._config: Optional[UnifiedConfig] = None
            self._cache: Dict[Tuple[str, ...], Any] = {}
            self._cache_lock: threading.Lock = threading.Lock()
            self._load_or_create()
            self._initialized = True

    @classmethod
    def get_instance(cls) -> "UnifiedConfigManager":
        """Get the singleton instance of the config manager.

        This method is thread-safe and ensures only one instance exists.

        Returns:
            The singleton UnifiedConfigManager instance
        """
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _ensure_config_dir(self) -> None:
        """Ensure the config directory exists."""
        config_dir = self._config_path.parent
        config_dir.mkdir(parents=True, exist_ok=True)

    def _load_or_create(self) -> None:
        """Load existing config or create with defaults.

        If the config file exists, load it. Otherwise, create the directory
        structure and initialize with default values.
        """
        self._ensure_config_dir()

        if self._config_path.exists():
            self._load()
        else:
            self._config = UnifiedConfig()
            self._save()

    def _load(self) -> None:
        """Load configuration from JSON file.

        Reads the config file and deserializes it into a UnifiedConfig object.
        If the file is corrupted or invalid, creates a new default config.
        """
        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Expand environment variables in loaded data
            data = self._expand_env_vars(data)
            self._config = UnifiedConfig(**data)
        except (json.JSONDecodeError, Exception) as e:
            # If file is corrupted, create default config
            print(f"Warning: Failed to load config from {self._config_path}: {e}")
            print("Creating default configuration...")
            self._config = UnifiedConfig()
            self._save()

    def _save(self) -> bool:
        """Save configuration to JSON file.

        Serializes the current UnifiedConfig and writes it to disk.

        Returns:
            True if save was successful, False otherwise
        """
        self._ensure_config_dir()

        if self._config is None:
            return True

        try:
            with open(self._config_path, "w", encoding="utf-8") as f:
                json.dump(
                    self._config.model_dump(mode="json"),
                    f,
                    indent=2,
                    ensure_ascii=False
                )
            return True
        except PermissionError as e:
            logging.error(f"Permission denied when saving config to {self._config_path}: {e}")
        except OSError as e:
            logging.error(f"OS error when saving config to {self._config_path}: {e}")
        except (TypeError, ValueError) as e:
            logging.error(f"JSON encoding error when saving config: {e}")
        except Exception as e:
            logging.error(f"Unexpected error when saving config to {self._config_path}: {e}")

        return False

    def reload(self) -> None:
        """Reload configuration from disk and clear cache.

        This method forces a fresh load from the config file and invalidates
        all cached values. Use this when the config file may have been
        modified externally.
        """
        with self._cache_lock:
            self._cache.clear()
        self._load()

    def _expand_env_vars(self, value: Any) -> Any:
        """Recursively expand ${ENV_VAR} patterns in strings, dicts, and lists.

        Args:
            value: The value to expand (string, dict, list, or other)

        Returns:
            The value with all ${ENV_VAR} patterns expanded to their
            environment variable values. If an environment variable is
            not set, the placeholder is left as-is.
        """
        if isinstance(value, str):
            pattern = r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}"

            def expand_match(match: re.Match) -> str:
                var_name = match.group(1)
                return os.environ.get(var_name, match.group(0))

            return re.sub(pattern, expand_match, value)
        elif isinstance(value, dict):
            return {k: self._expand_env_vars(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [self._expand_env_vars(item) for item in value]
        else:
            return value

    def _invalidate_cache(self) -> None:
        """Clear the configuration cache."""
        with self._cache_lock:
            self._cache.clear()

    def _get_cache_key(self, *parts: str) -> Tuple[str, ...]:
        """Generate a cache key from parts.

        Returns a tuple to avoid delimiter collision issues that could occur
        with string joining (e.g., ["llm", "agent:name"] vs ["llm:agent", "name"]).
        """
        return parts

    def _get_cached(self, key: Tuple[str, ...]) -> Any:
        """Get a value from cache."""
        with self._cache_lock:
            return self._cache.get(key)

    def _set_cached(self, key: Tuple[str, ...], value: Any) -> None:
        """Set a value in cache."""
        with self._cache_lock:
            self._cache[key] = value

    def get_agent_llm_config(self, agent_name: str) -> LLMConfig:
        """Get the effective LLM configuration for an agent.

        Resolves the LLM configuration using the following priority:
        1. Agent-specific LLM config
        2. Global LLM config
        3. Default values (ollama/qwen3:8b)

        Args:
            agent_name: One of "sentinel", "architect", "warden"

        Returns:
            LLMConfig: The resolved LLM configuration

        Raises:
            ValueError: If agent_name is not recognized
        """
        if agent_name not in ("sentinel", "architect", "warden"):
            raise ValueError(f"Unknown agent: {agent_name}")

        cache_key = self._get_cache_key("llm", agent_name)
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        if self._config is None:
            self._config = UnifiedConfig()

        # 1. Check agent-specific LLM config
        agent_config = self._config.agents.get(agent_name)
        if agent_config and hasattr(agent_config, "llm") and agent_config.llm:
            self._set_cached(cache_key, agent_config.llm)
            return agent_config.llm

        # 2. Fall back to global LLM config
        global_llm = self._config.global_config.get("llm")
        if global_llm:
            llm_config = LLMConfig(**global_llm)
            self._set_cached(cache_key, llm_config)
            return llm_config

        # 3. Default values
        default_llm = LLMConfig(
            provider="ollama",
            model="qwen3:8b",
            base_url="http://localhost:11434"
        )
        self._set_cached(cache_key, default_llm)
        return default_llm

    def get_agent_mode(self, agent_name: str) -> str:
        """Get the mode for an agent.

        Args:
            agent_name: One of "sentinel", "architect", "warden"

        Returns:
            str: The agent mode ("core" or "adk")

        Raises:
            ValueError: If agent_name is not recognized
        """
        if agent_name not in ("sentinel", "architect", "warden"):
            raise ValueError(f"Unknown agent: {agent_name}")

        if self._config is None:
            self._config = UnifiedConfig()

        agent_config = self._config.agents.get(agent_name)
        if agent_config and hasattr(agent_config, "mode"):
            return agent_config.mode

        # Fall back to global mode
        return self._config.global_config.get("mode", "core")

    def get_project_config(
        self, project_path: str, agent_name: Optional[str] = None
    ) -> Union[Optional[ProjectOverride], Optional[AgentConfig]]:
        """Get project-specific configuration.

        Retrieves the project override configuration. If agent_name is provided,
        returns the specific agent config for that project (merged with global).

        Args:
            project_path: Path to the project
            agent_name: Optional agent name to get specific config for

        Returns:
            If agent_name is None: ProjectOverride or None
            If agent_name is provided: AgentConfig or None
        """
        if self._config is None:
            self._config = UnifiedConfig()

        project_override = self._config.projects.get(project_path)

        if agent_name is None:
            return project_override

        if project_override is None:
            return None

        # Return specific agent config from project override
        if agent_name == "sentinel":
            return project_override.sentinel
        elif agent_name == "architect":
            return project_override.architect
        elif agent_name == "warden":
            return project_override.warden

        return None

    def update_agent_config(
        self, agent_name: str, config: Union[Dict[str, Any], AgentConfig]
    ) -> None:
        """Update configuration for a specific agent.

        Args:
            agent_name: One of "sentinel", "architect", "warden"
            config: The new configuration (dict or AgentConfig object)

        Raises:
            ValueError: If agent_name is not recognized
        """
        if agent_name not in ("sentinel", "architect", "warden"):
            raise ValueError(f"Unknown agent: {agent_name}")

        if self._config is None:
            self._config = UnifiedConfig()

        # Convert dict to appropriate config type
        if isinstance(config, dict):
            if agent_name == "sentinel":
                config = SentinelConfig(**config)
            elif agent_name == "architect":
                config = ArchitectConfig(**config)
            else:  # warden
                config = WardenConfig(**config)

        self._config.agents[agent_name] = config
        self._invalidate_cache()
        self._save()

    def update_global_config(self, global_config: Dict[str, Any]) -> None:
        """Update the global configuration.

        Args:
            global_config: The new global configuration dictionary
        """
        if self._config is None:
            self._config = UnifiedConfig()

        self._config.global_config = global_config
        self._invalidate_cache()
        self._save()

    def get_full_config(self) -> Dict[str, Any]:
        """Get the complete configuration as a dictionary.

        Returns:
            Dict containing the full configuration including version,
            global_config, agents, and projects
        """
        if self._config is None:
            self._config = UnifiedConfig()

        return self._config.model_dump(mode="json")

    def get_config(self) -> UnifiedConfig:
        """Get the UnifiedConfig object directly.

        Returns:
            The current UnifiedConfig instance
        """
        if self._config is None:
            self._config = UnifiedConfig()

        return self._config

    def add_project_override(
        self, project_path: str, override: Union[Dict[str, Any], ProjectOverride]
    ) -> None:
        """Add or update a project-specific configuration override.

        Args:
            project_path: Path to the project
            override: The project override configuration
        """
        if self._config is None:
            self._config = UnifiedConfig()

        if isinstance(override, dict):
            override = ProjectOverride(**override)

        self._config.projects[project_path] = override
        self._save()

    def remove_project_override(self, project_path: str) -> bool:
        """Remove a project-specific configuration override.

        Args:
            project_path: Path to the project

        Returns:
            True if the override was removed, False if it didn't exist
        """
        if self._config is None:
            return False

        if project_path in self._config.projects:
            del self._config.projects[project_path]
            self._save()
            return True

        return False

    def reset_to_defaults(self) -> None:
        """Reset configuration to default values.

        This will create a fresh default configuration and save it to disk.
        All custom settings will be lost.
        """
        self._config = UnifiedConfig()
        self._invalidate_cache()
        self._save()


# Convenience function for quick access
def get_config_manager() -> UnifiedConfigManager:
    """Get the singleton UnifiedConfigManager instance.

    Returns:
        The UnifiedConfigManager singleton
    """
    return UnifiedConfigManager.get_instance()
