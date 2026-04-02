"""
Tests for the UnifiedConfigManager.

This module tests the configuration manager ensuring proper singleton behavior,
caching, environment variable expansion, and configuration resolution.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import pytest

# Add the app directory to the path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from config_manager import UnifiedConfigManager, get_config_manager
from models.config import (
    ArchitectConfig,
    LLMConfig,
    ProjectOverride,
    SentinelConfig,
    UnifiedConfig,
    WardenConfig,
)


@pytest.fixture
def temp_config_dir():
    """Create a temporary directory for config files."""
    temp_dir = tempfile.mkdtemp()
    original_home = os.environ.get("HOME")
    original_userprofile = os.environ.get("USERPROFILE")

    # Set HOME to temp directory for the test
    os.environ["HOME"] = temp_dir
    os.environ["USERPROFILE"] = temp_dir

    # Reset singleton before each test
    UnifiedConfigManager._instance = None

    yield temp_dir

    # Cleanup
    shutil.rmtree(temp_dir, ignore_errors=True)
    if original_home:
        os.environ["HOME"] = original_home
    else:
        os.environ.pop("HOME", None)
    if original_userprofile:
        os.environ["USERPROFILE"] = original_userprofile
    else:
        os.environ.pop("USERPROFILE", None)

    # Reset singleton after test
    UnifiedConfigManager._instance = None


@pytest.fixture
def manager(temp_config_dir):
    """Get a fresh config manager instance."""
    return UnifiedConfigManager.get_instance()


class TestConfigManagerCreation:
    """Tests for ConfigManager initialization and singleton behavior."""

    def test_creates_default_config(self, temp_config_dir, manager):
        """Test that manager creates default config when file doesn't exist."""
        config_path = Path(temp_config_dir) / ".cerebro" / "global.config.json"

        # Config file should be created automatically
        assert config_path.exists()

        # Verify default structure
        with open(config_path, "r") as f:
            data = json.load(f)

        assert "version" in data
        assert "global_config" in data
        assert "agents" in data
        assert "projects" in data
        assert "sentinel" in data["agents"]
        assert "architect" in data["agents"]
        assert "warden" in data["agents"]

    def test_singleton_pattern(self, temp_config_dir):
        """Test that get_instance returns the same instance."""
        manager1 = UnifiedConfigManager.get_instance()
        manager2 = UnifiedConfigManager.get_instance()

        assert manager1 is manager2

    def test_thread_safe_initialization(self, temp_config_dir):
        """Test that singleton initialization is thread-safe."""
        # Reset singleton
        UnifiedConfigManager._instance = None
        managers = []
        errors = []

        def get_instance():
            try:
                mgr = UnifiedConfigManager.get_instance()
                managers.append(mgr)
            except Exception as e:
                errors.append(e)

        # Create multiple threads trying to get instance simultaneously
        threads = [threading.Thread(target=get_instance) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All should get the same instance
        assert len(errors) == 0
        assert len(managers) == 10
        assert all(m is managers[0] for m in managers)


class TestConfigLoading:
    """Tests for configuration loading."""

    def test_loads_existing_config(self, temp_config_dir):
        """Test that manager loads existing config file."""
        config_path = Path(temp_config_dir) / ".cerebro" / "global.config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)

        # Create a custom config
        custom_config = {
            "version": "1.0.0",
            "global_config": {
                "mode": "adk",
                "llm": {
                    "provider": "openai",
                    "model": "gpt-4",
                    "temperature": 0.5
                }
            },
            "agents": {
                "sentinel": {
                    "enabled": True,
                    "mode": "core",
                    "rules": ["custom_rule"],
                    "ignored_paths": [".venv"],
                    "scan_on_startup": False,
                    "auto_fix": True
                },
                "architect": {
                    "enabled": True,
                    "mode": "adk",
                    "llm": {
                        "provider": "gemini",
                        "model": "gemini-2.0-flash"
                    },
                    "max_lines_per_function": 30,
                    "architecture_pattern": "hexagonal",
                    "forbidden_imports": ["banned_lib"],
                    "ignored_paths": [".venv", "node_modules"]
                },
                "warden": {
                    "enabled": False,
                    "mode": "core",
                    "risk_threshold": "high",
                    "enable_predictions": False,
                    "changelog_depth": 20
                }
            },
            "projects": {}
        }

        with open(config_path, "w") as f:
            json.dump(custom_config, f)

        # Reset singleton to force reload
        UnifiedConfigManager._instance = None

        manager = UnifiedConfigManager.get_instance()
        config = manager.get_full_config()

        assert config["global_config"]["mode"] == "adk"
        assert config["agents"]["sentinel"]["rules"] == ["custom_rule"]
        assert config["agents"]["architect"]["max_lines_per_function"] == 30
        assert config["agents"]["warden"]["enabled"] is False

    def test_reload_clears_cache(self, temp_config_dir, manager):
        """Test that reload clears the cache."""
        # Get something to cache
        _ = manager.get_agent_llm_config("sentinel")

        # Modify config directly
        config_path = Path(temp_config_dir) / ".cerebro" / "global.config.json"
        with open(config_path, "r") as f:
            data = json.load(f)

        data["global_config"]["llm"]["model"] = "modified-model"

        with open(config_path, "w") as f:
            json.dump(data, f)

        # Reload and verify cache is cleared
        manager.reload()
        llm_config = manager.get_agent_llm_config("sentinel")

        assert llm_config.model == "modified-model"


class TestLLMConfigResolution:
    """Tests for LLM configuration resolution."""

    def test_agent_specific_llm_overrides_global(self, temp_config_dir):
        """Test that agent-specific LLM config takes precedence."""
        config_path = Path(temp_config_dir) / ".cerebro" / "global.config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)

        custom_config = {
            "version": "1.0.0",
            "global_config": {
                "mode": "core",
                "llm": {
                    "provider": "ollama",
                    "model": "global-model",
                    "temperature": 0.5
                }
            },
            "agents": {
                "sentinel": {
                    "enabled": True,
                    "mode": "core",
                    "rules": [],
                    "ignored_paths": [],
                    "scan_on_startup": True,
                    "auto_fix": False
                },
                "architect": {
                    "enabled": True,
                    "mode": "adk",
                    "llm": {
                        "provider": "openai",
                        "model": "agent-specific-model",
                        "temperature": 0.9
                    },
                    "max_lines_per_function": 50,
                    "architecture_pattern": "layered",
                    "forbidden_imports": [],
                    "ignored_paths": []
                },
                "warden": {
                    "enabled": True,
                    "mode": "core",
                    "risk_threshold": "medium",
                    "enable_predictions": True,
                    "changelog_depth": 10
                }
            },
            "projects": {}
        }

        with open(config_path, "w") as f:
            json.dump(custom_config, f)

        # Reset singleton
        UnifiedConfigManager._instance = None

        manager = UnifiedConfigManager.get_instance()

        # Architect should use its specific config
        architect_llm = manager.get_agent_llm_config("architect")
        assert architect_llm.provider == "openai"
        assert architect_llm.model == "agent-specific-model"
        assert architect_llm.temperature == 0.9

        # Sentinel should fall back to global
        sentinel_llm = manager.get_agent_llm_config("sentinel")
        assert sentinel_llm.provider == "ollama"
        assert sentinel_llm.model == "global-model"
        assert sentinel_llm.temperature == 0.5

    def test_uses_global_llm_when_no_agent_override(self, temp_config_dir, manager):
        """Test that global LLM is used when agent has no specific config."""
        llm_config = manager.get_agent_llm_config("sentinel")

        # Should get default values when no global or agent config is set
        assert llm_config is not None

    def test_caching_of_llm_config(self, temp_config_dir, manager):
        """Test that LLM config is cached."""
        # First call should cache
        llm1 = manager.get_agent_llm_config("sentinel")
        llm2 = manager.get_agent_llm_config("sentinel")

        # Should return same object (cached)
        assert llm1 is llm2

        # Update config should invalidate cache
        manager.update_global_config({
            "mode": "core",
            "llm": {
                "provider": "gemini",
                "model": "gemini-2.0-flash",
                "temperature": 0.8
            }
        })

        # After update, should be different object
        llm3 = manager.get_agent_llm_config("sentinel")
        assert llm3.provider == "gemini"
        assert llm3.model == "gemini-2.0-flash"

    def test_default_llm_when_no_config(self, temp_config_dir):
        """Test default LLM values when no configuration exists."""
        config_path = Path(temp_config_dir) / ".cerebro" / "global.config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)

        # Create config without any LLM settings
        minimal_config = {
            "version": "1.0.0",
            "global_config": {"mode": "core"},
            "agents": {
                "sentinel": {"enabled": True, "mode": "core"},
                "architect": {"enabled": True, "mode": "core"},
                "warden": {"enabled": True, "mode": "core"}
            },
            "projects": {}
        }

        with open(config_path, "w") as f:
            json.dump(minimal_config, f)

        # Reset singleton
        UnifiedConfigManager._instance = None

        manager = UnifiedConfigManager.get_instance()

        # Should get default values
        llm = manager.get_agent_llm_config("sentinel")
        assert llm.provider == "ollama"
        assert llm.model == "qwen3:8b"
        assert llm.base_url == "http://localhost:11434"

    def test_invalid_agent_name_raises_error(self, manager):
        """Test that invalid agent name raises ValueError."""
        with pytest.raises(ValueError, match="Unknown agent"):
            manager.get_agent_llm_config("invalid_agent")


class TestAgentMode:
    """Tests for agent mode retrieval."""

    def test_get_agent_mode(self, temp_config_dir, manager):
        """Test getting agent mode."""
        # Default mode should be "core"
        assert manager.get_agent_mode("sentinel") == "core"
        assert manager.get_agent_mode("architect") == "core"
        assert manager.get_agent_mode("warden") == "core"

    def test_get_agent_mode_custom(self, temp_config_dir):
        """Test getting custom agent mode."""
        config_path = Path(temp_config_dir) / ".cerebro" / "global.config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)

        custom_config = {
            "version": "1.0.0",
            "global_config": {"mode": "adk"},
            "agents": {
                "sentinel": {
                    "enabled": True,
                    "mode": "adk",
                    "rules": [],
                    "ignored_paths": [],
                    "scan_on_startup": True,
                    "auto_fix": False
                },
                "architect": {
                    "enabled": True,
                    "mode": "core",
                    "max_lines_per_function": 50,
                    "architecture_pattern": "layered",
                    "forbidden_imports": [],
                    "ignored_paths": []
                },
                "warden": {
                    "enabled": True,
                    "mode": "core",
                    "risk_threshold": "medium",
                    "enable_predictions": True,
                    "changelog_depth": 10
                }
            },
            "projects": {}
        }

        with open(config_path, "w") as f:
            json.dump(custom_config, f)

        # Reset singleton
        UnifiedConfigManager._instance = None

        manager = UnifiedConfigManager.get_instance()

        assert manager.get_agent_mode("sentinel") == "adk"
        assert manager.get_agent_mode("architect") == "core"
        assert manager.get_agent_mode("warden") == "core"


class TestEnvironmentVariableExpansion:
    """Tests for environment variable expansion."""

    def test_expand_env_vars_in_string(self, manager):
        """Test expanding env vars in a string."""
        os.environ["TEST_VAR"] = "test_value"

        result = manager._expand_env_vars("Hello ${TEST_VAR}")
        assert result == "Hello test_value"

    def test_expand_env_vars_in_dict(self, manager):
        """Test expanding env vars in a dictionary."""
        os.environ["API_KEY"] = "secret123"
        os.environ["BASE_URL"] = "https://api.example.com"

        data = {
            "api_key": "${API_KEY}",
            "url": "${BASE_URL}/v1",
            "static": "no_vars_here"
        }

        result = manager._expand_env_vars(data)
        assert result["api_key"] == "secret123"
        assert result["url"] == "https://api.example.com/v1"
        assert result["static"] == "no_vars_here"

    def test_expand_env_vars_in_list(self, manager):
        """Test expanding env vars in a list."""
        os.environ["ITEM1"] = "first"
        os.environ["ITEM2"] = "second"

        data = ["${ITEM1}", "static", "${ITEM2}"]
        result = manager._expand_env_vars(data)
        assert result == ["first", "static", "second"]

    def test_expand_missing_env_var_keeps_placeholder(self, manager):
        """Test that missing env vars keep the placeholder."""
        # Ensure VAR doesn't exist
        os.environ.pop("MISSING_VAR", None)

        result = manager._expand_env_vars("${MISSING_VAR}")
        assert result == "${MISSING_VAR}"

    def test_env_var_expansion_in_config_file(self, temp_config_dir):
        """Test env var expansion when loading config file."""
        config_path = Path(temp_config_dir) / ".cerebro" / "global.config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)

        os.environ["OLLAMA_URL"] = "http://custom:11434"
        os.environ["MODEL_NAME"] = "custom-model"

        config_with_env = {
            "version": "1.0.0",
            "global_config": {
                "mode": "core",
                "llm": {
                    "provider": "ollama",
                    "model": "${MODEL_NAME}",
                    "base_url": "${OLLAMA_URL}",
                    "temperature": 0.7
                }
            },
            "agents": {
                "sentinel": {
                    "enabled": True,
                    "mode": "core",
                    "rules": [],
                    "ignored_paths": [],
                    "scan_on_startup": True,
                    "auto_fix": False
                },
                "architect": {
                    "enabled": True,
                    "mode": "core",
                    "max_lines_per_function": 50,
                    "architecture_pattern": "layered",
                    "forbidden_imports": [],
                    "ignored_paths": []
                },
                "warden": {
                    "enabled": True,
                    "mode": "core",
                    "risk_threshold": "medium",
                    "enable_predictions": True,
                    "changelog_depth": 10
                }
            },
            "projects": {}
        }

        with open(config_path, "w") as f:
            json.dump(config_with_env, f)

        # Reset singleton
        UnifiedConfigManager._instance = None

        manager = UnifiedConfigManager.get_instance()
        llm = manager.get_agent_llm_config("sentinel")

        assert llm.base_url == "http://custom:11434"
        assert llm.model == "custom-model"


class TestProjectConfig:
    """Tests for project-specific configuration."""

    def test_get_project_config(self, temp_config_dir, manager):
        """Test getting project configuration."""
        # Initially no projects
        assert manager.get_project_config("/some/project") is None

    def test_add_project_override(self, temp_config_dir, manager):
        """Test adding project override."""
        override = ProjectOverride(
            project_path="/my/project",
            sentinel=SentinelConfig(rules=["custom"])
        )

        manager.add_project_override("/my/project", override)

        # Retrieve the project config
        project_config = manager.get_project_config("/my/project")
        assert project_config is not None
        assert project_config.sentinel.rules == ["custom"]

    def test_get_project_agent_config(self, temp_config_dir, manager):
        """Test getting specific agent config for project."""
        override = ProjectOverride(
            project_path="/my/project",
            sentinel=SentinelConfig(rules=["project_specific"], mode="adk")
        )

        manager.add_project_override("/my/project", override)

        sentinel_config = manager.get_project_config("/my/project", "sentinel")
        assert sentinel_config is not None
        assert sentinel_config.rules == ["project_specific"]
        assert sentinel_config.mode == "adk"

    def test_remove_project_override(self, temp_config_dir, manager):
        """Test removing project override."""
        override = ProjectOverride(project_path="/my/project")
        manager.add_project_override("/my/project", override)

        assert manager.get_project_config("/my/project") is not None

        removed = manager.remove_project_override("/my/project")
        assert removed is True
        assert manager.get_project_config("/my/project") is None

        # Removing non-existent returns False
        removed = manager.remove_project_override("/nonexistent")
        assert removed is False


class TestConfigUpdates:
    """Tests for configuration updates."""

    def test_update_agent_config(self, temp_config_dir, manager):
        """Test updating agent configuration."""
        manager.update_agent_config("sentinel", {
            "enabled": False,
            "mode": "adk",
            "rules": ["strict"],
            "ignored_paths": ["build"],
            "scan_on_startup": False,
            "auto_fix": True
        })

        config = manager.get_full_config()
        assert config["agents"]["sentinel"]["enabled"] is False
        assert config["agents"]["sentinel"]["mode"] == "adk"
        assert config["agents"]["sentinel"]["rules"] == ["strict"]

    def test_update_global_config(self, temp_config_dir, manager):
        """Test updating global configuration."""
        manager.update_global_config({
            "mode": "adk",
            "llm": {
                "provider": "gemini",
                "model": "gemini-2.0-flash",
                "temperature": 0.5
            }
        })

        config = manager.get_full_config()
        assert config["global_config"]["mode"] == "adk"
        assert config["global_config"]["llm"]["provider"] == "gemini"

    def test_update_clears_cache(self, temp_config_dir, manager):
        """Test that updates invalidate the cache."""
        # Get initial config to populate cache
        llm1 = manager.get_agent_llm_config("sentinel")

        # Update config
        manager.update_global_config({
            "mode": "core",
            "llm": {
                "provider": "openai",
                "model": "gpt-4",
                "temperature": 0.5
            }
        })

        # Should get new value, not cached
        llm2 = manager.get_agent_llm_config("sentinel")
        assert llm2.provider == "openai"
        assert llm2.model == "gpt-4"

    def test_invalid_agent_name_raises_on_update(self, manager):
        """Test that invalid agent name raises ValueError on update."""
        with pytest.raises(ValueError, match="Unknown agent"):
            manager.update_agent_config("invalid_agent", {"enabled": False})


class TestUtilityFunctions:
    """Tests for utility functions."""

    def test_get_config_manager(self, temp_config_dir):
        """Test the get_config_manager convenience function."""
        manager = get_config_manager()
        assert manager is UnifiedConfigManager.get_instance()

    def test_get_full_config(self, temp_config_dir, manager):
        """Test getting full config as dict."""
        full_config = manager.get_full_config()

        assert isinstance(full_config, dict)
        assert "version" in full_config
        assert "global_config" in full_config
        assert "agents" in full_config
        assert "projects" in full_config

    def test_get_config_object(self, temp_config_dir, manager):
        """Test getting UnifiedConfig object directly."""
        config = manager.get_config()
        assert isinstance(config, UnifiedConfig)

    def test_reset_to_defaults(self, temp_config_dir, manager):
        """Test resetting to default configuration."""
        # Modify config
        manager.update_global_config({
            "mode": "adk",
            "llm": {
                "provider": "custom",
                "model": "custom-model"
            }
        })

        # Reset
        manager.reset_to_defaults()

        # Should be back to defaults
        config = manager.get_full_config()
        assert config["version"] == "1.0.0"
        assert config["global_config"]["mode"] == "core"


class TestPersistence:
    """Tests for configuration persistence."""

    def test_config_persisted_to_file(self, temp_config_dir, manager):
        """Test that config changes are saved to file."""
        manager.update_global_config({
            "mode": "adk",
            "llm": {
                "provider": "openai",
                "model": "gpt-4",
                "temperature": 0.5
            }
        })

        # Read directly from file
        config_path = Path(temp_config_dir) / ".cerebro" / "global.config.json"
        with open(config_path, "r") as f:
            data = json.load(f)

        assert data["global_config"]["mode"] == "adk"
        assert data["global_config"]["llm"]["provider"] == "openai"

    def test_corrupted_config_creates_default(self, temp_config_dir):
        """Test that corrupted config file creates default."""
        config_path = Path(temp_config_dir) / ".cerebro" / "global.config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)

        # Write invalid JSON
        with open(config_path, "w") as f:
            f.write("{invalid json")

        # Reset singleton
        UnifiedConfigManager._instance = None

        manager = UnifiedConfigManager.get_instance()
        config = manager.get_full_config()

        # Should have valid default config
        assert "version" in config
        assert "agents" in config


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
