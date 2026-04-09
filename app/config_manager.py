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

try:
    import toml
    TOML_AVAILABLE = True
except ImportError:
    TOML_AVAILABLE = False
    toml = None

from app.models.config import (
    AgentConfig,
    ArchitectConfig,
    CerebroConfig,
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
            
            from pydantic import ValidationError
            try:
                self._config = UnifiedConfig.model_validate(data)
            except ValidationError as ve:
                logging.error(f"❌ Configuration validation error: {ve}")
                # Intentar preservación parcial: Usar defaults y luego sobreescribir con lo que sea válido del JSON
                self._config = UnifiedConfig()
                
                # Cargar secciones una a una de forma segura
                if "cerebro" in data:
                    try: self._config.cerebro = CerebroConfig.model_validate(data["cerebro"])
                    except: logging.warning("⚠️ No se pudo recuperar sección 'cerebro' del config")
                
                if "global_config" in data:
                    self._config.global_config.update(data["global_config"])

                if "agents" in data:
                    # Intentar recuperar agentes válidos
                    for agent_name, agent_data in data["agents"].items():
                        try: 
                            # Esto es un poco rudimentario pero ayuda a no perder todo
                            if agent_name in self._config.agents:
                                current_agent = self._config.agents[agent_name]
                                # Solo actualizar si coinciden campos básicos
                                pass 
                        except: pass
                
                # No guardamos inmediatamente para no persistir un estado corrupto
                # El usuario deberá guardar desde el Dashboard para arreglarlo
                
        except (json.JSONDecodeError, FileNotFoundError) as e:
            logging.warning(f"⚠️ Config corrupted or missing ({e}), using defaults.")
            self._config = UnifiedConfig()
            self._save()
        except Exception as e:
            logging.error(f"❌ Unexpected error loading config: {e}")
            if self._config is None: self._config = UnifiedConfig()

    def _save(self) -> bool:
        """Save configuration to JSON file.

        Serializes the current UnifiedConfig and writes it to disk.
        Also syncs LLM configuration to Sentinel Core's .sentinelrc.toml

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

            # Sync to Sentinel Core config
            self._sync_to_sentinel_config()

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

    def _sync_to_sentinel_config(self) -> None:
        """Sync global LLM configuration to Sentinel Core's .sentinelrc.toml.

        Updates the .sentinelrc.toml file in the active project's directory
        with the global LLM configuration.
        """
        from pathlib import Path

        if not TOML_AVAILABLE or self._config is None:
            return

        global_llm = self._config.global_config.get("llm")
        if not global_llm:
            return

        # Get active project from cerebro config
        cerebro_config = self._config.cerebro if hasattr(self._config, 'cerebro') else None
        if not cerebro_config:
            return

        # Try to find project path from active_project
        project_path = None
        try:
            from app.orchestrator import orchestrator
            if orchestrator.active_project:
                # Usar get_project_path para obtener la ruta correcta
                project_path = orchestrator._projects.get_project_path(orchestrator.active_project)
        except Exception:
            pass

        if not project_path:
            return

        sentinel_config_path = Path(project_path) / ".sentinelrc.toml"
        if not sentinel_config_path.exists():
            # No sentinel config to update
            return

        try:
            # Read existing config
            with open(sentinel_config_path, "r", encoding="utf-8") as f:
                sentinel_config = toml.load(f)

            # Map global LLM config to Sentinel format
            provider = global_llm.get("provider", "anthropic")
            model = global_llm.get("model", "claude-3-5-sonnet-20241022")
            base_url = global_llm.get("base_url", "")
            api_key = global_llm.get("api_key", "")

            # Map provider names
            sentinel_provider = provider
            sentinel_url = base_url

            if provider == "gemini-open-source":
                sentinel_provider = "openai"  # Gemma uses OpenAI-compatible endpoint
                # Ensure URL has /v1beta/openai/ path
                if base_url and "generativelanguage.googleapis.com" in base_url:
                    sentinel_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
            elif provider == "gemini":
                sentinel_provider = "gemini"
                sentinel_url = base_url or "https://generativelanguage.googleapis.com"
            elif provider == "ollama":
                sentinel_provider = "ollama"
                sentinel_url = base_url or "http://localhost:11434"

            # Update primary_model section
            sentinel_config["primary_model"] = {
                "name": model,
                "url": sentinel_url,
                "api_key": api_key,
                "provider": sentinel_provider,
            }

            # Write back
            with open(sentinel_config_path, "w", encoding="utf-8") as f:
                toml.dump(sentinel_config, f)

            logging.info(f"✅ Synced LLM config to Sentinel Core: {sentinel_config_path}")

            # Also sync to Sentinel ADK .env file
            self._sync_to_sentinel_adk_env(global_llm)

        except Exception as e:
            logging.warning(f"⚠️ Failed to sync config to Sentinel: {e}")

    def _sync_to_sentinel_adk_env(self, global_llm: dict) -> None:
        """Sync LLM configuration to Sentinel ADK's .env file.

        Updates the .env file in sentinel_adk directory with the global LLM configuration.
        """
        from pathlib import Path

        # Find sentinel_adk directory (sibling to cerebro directory)
        cerebro_dir = Path(__file__).parent
        sentinel_adk_dir = cerebro_dir.parent / "sentinel" / "sentinel_adk"

        if not sentinel_adk_dir.exists():
            logging.debug("Sentinel ADK directory not found, skipping .env sync")
            return

        env_path = sentinel_adk_dir / ".env"

        # Read existing .env or create new
        env_lines = []
        env_vars = {}

        if env_path.exists():
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip('\n')
                    if '=' in line and not line.startswith('#'):
                        key, val = line.split('=', 1)
                        env_vars[key] = val
                    env_lines.append(line)

        # Map global LLM config to ADK env vars
        provider = global_llm.get("provider", "gemini")
        model = global_llm.get("model", "gemini-2.0-flash")
        base_url = global_llm.get("base_url", "")
        api_key = global_llm.get("api_key", "")

        # Map provider
        if provider == "gemini-open-source":
            adk_provider = "gemini-open-source"
            adk_model = model  # e.g., "gemma-4-31b-it"
            adk_base_url = base_url or "https://generativelanguage.googleapis.com"
        elif provider == "gemini":
            adk_provider = "gemini"
            adk_model = model
            adk_base_url = base_url or "https://generativelanguage.googleapis.com"
        elif provider == "ollama":
            adk_provider = "ollama"
            adk_model = model
            adk_base_url = base_url or "http://localhost:11434"
        elif provider == "claude":
            adk_provider = "claude"
            adk_model = model
        elif provider == "openai":
            adk_provider = "openai"
            adk_model = model
        else:
            adk_provider = provider
            adk_model = model
            adk_base_url = base_url

        # Update env vars
        env_vars["LLM_PROVIDER"] = adk_provider
        env_vars["GEMINI_MODEL"] = adk_model
        if api_key:
            if provider in ("gemini", "gemini-open-source"):
                env_vars["GOOGLE_API_KEY"] = api_key
            elif provider == "claude":
                env_vars["ANTHROPIC_API_KEY"] = api_key
            elif provider == "openai":
                env_vars["OPENAI_API_KEY"] = api_key
        if adk_base_url and provider in ("gemini", "gemini-open-source"):
            env_vars["GOOGLE_API_BASE_URL"] = adk_base_url

        # Rebuild .env content
        new_lines = []
        updated_keys = set()

        for line in env_lines:
            if '=' in line and not line.startswith('#'):
                key = line.split('=', 1)[0]
                if key in env_vars:
                    new_lines.append(f"{key}={env_vars[key]}")
                    updated_keys.add(key)
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)

        # Add any new vars that weren't in the file
        for key, val in env_vars.items():
            if key not in updated_keys:
                new_lines.append(f"{key}={val}")

        # Write back
        with open(env_path, "w", encoding="utf-8") as f:
            f.write('\n'.join(new_lines) + '\n')

        logging.info(f"✅ Synced LLM config to Sentinel ADK: {env_path}")

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
