"""
Tests for the unified configuration models.

This module tests the Pydantic models defined in cerebro/app/models/config.py
ensuring proper validation, defaults, and behavior.
"""

import pytest
from pydantic import ValidationError

import sys
from pathlib import Path

# Add the app directory to the path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from models.config import (
    AgentConfig,
    ArchitectConfig,
    LLMConfig,
    ProjectOverride,
    SentinelConfig,
    UnifiedConfig,
    WardenConfig,
)


class TestLLMConfig:
    """Tests for the LLMConfig model."""

    def test_basic_creation(self):
        """Test basic LLMConfig creation with required fields."""
        config = LLMConfig(provider="ollama", model="llama3.2")
        assert config.provider == "ollama"
        assert config.model == "llama3.2"
        assert config.base_url == "http://localhost:11434"  # Auto-set for ollama
        assert config.api_key is None
        assert config.temperature == 0.7  # Default
        assert config.max_tokens is None

    def test_all_providers(self):
        """Test that all provider values are accepted."""
        providers = ["ollama", "openai", "gemini", "claude", "custom"]
        for provider in providers:
            config = LLMConfig(provider=provider, model="test-model")
            assert config.provider == provider

    def test_temperature_bounds(self):
        """Test temperature validation (0.0-2.0)."""
        # Valid values
        assert LLMConfig(provider="ollama", model="test", temperature=0.0).temperature == 0.0
        assert LLMConfig(provider="ollama", model="test", temperature=1.0).temperature == 1.0
        assert LLMConfig(provider="ollama", model="test", temperature=2.0).temperature == 2.0

        # Invalid values
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(provider="ollama", model="test", temperature=-0.1)
        assert "temperature" in str(exc_info.value)

        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(provider="ollama", model="test", temperature=2.1)
        assert "temperature" in str(exc_info.value)

    def test_env_placeholder_validation(self):
        """Test that env placeholder pattern is accepted."""
        # Valid env placeholder
        config = LLMConfig(
            provider="openai",
            model="gpt-4",
            api_key="${OPENAI_API_KEY}"
        )
        assert config.api_key == "${OPENAI_API_KEY}"

        # Plain string is also accepted (for backward compatibility)
        config2 = LLMConfig(
            provider="openai",
            model="gpt-4",
            api_key="sk-test-key"
        )
        assert config2.api_key == "sk-test-key"

        # Empty string is allowed
        config3 = LLMConfig(provider="ollama", model="test", api_key="")
        assert config3.api_key == ""

    def test_base_url_validation(self):
        """Test base URL validation."""
        # Valid URLs
        assert LLMConfig(
            provider="ollama",
            model="test",
            base_url="http://localhost:11434"
        ).base_url == "http://localhost:11434"

        assert LLMConfig(
            provider="custom",
            model="test",
            base_url="https://api.example.com"
        ).base_url == "https://api.example.com"

        # Invalid URL
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(provider="ollama", model="test", base_url="invalid-url")
        assert "base_url" in str(exc_info.value)

    def test_ollama_default_base_url(self):
        """Test that ollama provider gets default base_url."""
        config = LLMConfig(provider="ollama", model="llama3.2")
        assert config.base_url == "http://localhost:11434"


class TestAgentConfig:
    """Tests for the base AgentConfig model."""

    def test_default_values(self):
        """Test default values for AgentConfig."""
        config = AgentConfig()
        assert config.enabled is True
        assert config.mode == "core"
        assert config.llm is None

    def test_custom_llm(self):
        """Test AgentConfig with custom LLM."""
        llm = LLMConfig(provider="gemini", model="gemini-2.0-flash")
        config = AgentConfig(enabled=False, mode="adk", llm=llm)
        assert config.enabled is False
        assert config.mode == "adk"
        assert config.llm is not None
        assert config.llm.provider == "gemini"

    def test_mode_values(self):
        """Test that only valid mode values are accepted."""
        # Valid modes
        assert AgentConfig(mode="core").mode == "core"
        assert AgentConfig(mode="adk").mode == "adk"

        # Invalid mode
        with pytest.raises(ValidationError):
            AgentConfig(mode="invalid")


class TestSentinelConfig:
    """Tests for SentinelConfig model."""

    def test_default_values(self):
        """Test default SentinelConfig values."""
        config = SentinelConfig()
        assert config.enabled is True
        assert config.mode == "core"
        assert config.rules == ["pep8", "type_hints", "docstrings"]
        assert ".venv" in config.ignored_paths
        assert config.scan_on_startup is True
        assert config.auto_fix is False

    def test_custom_rules(self):
        """Test custom rule configuration."""
        config = SentinelConfig(
            rules=["custom_rule", "imports"],
            scan_on_startup=False,
            auto_fix=True
        )
        assert config.rules == ["custom_rule", "imports"]
        assert config.scan_on_startup is False
        assert config.auto_fix is True

    def test_ignored_paths(self):
        """Test ignored paths configuration."""
        config = SentinelConfig(
            ignored_paths=["build", "dist", ".tox"]
        )
        assert config.ignored_paths == ["build", "dist", ".tox"]


class TestArchitectConfig:
    """Tests for ArchitectConfig model."""

    def test_default_values(self):
        """Test default ArchitectConfig values."""
        config = ArchitectConfig()
        assert config.enabled is True
        assert config.mode == "core"
        assert config.max_lines_per_function == 50
        assert config.architecture_pattern == "layered"
        assert config.forbidden_imports == []

    def test_max_lines_validation(self):
        """Test max_lines_per_function bounds."""
        # Valid values
        assert ArchitectConfig(max_lines_per_function=1).max_lines_per_function == 1
        assert ArchitectConfig(max_lines_per_function=500).max_lines_per_function == 500

        # Invalid values
        with pytest.raises(ValidationError):
            ArchitectConfig(max_lines_per_function=0)

        with pytest.raises(ValidationError):
            ArchitectConfig(max_lines_per_function=501)

    def test_forbidden_imports(self):
        """Test forbidden imports configuration."""
        config = ArchitectConfig(
            forbidden_imports=["requests", "urllib"],
            architecture_pattern="hexagonal"
        )
        assert config.forbidden_imports == ["requests", "urllib"]
        assert config.architecture_pattern == "hexagonal"


class TestWardenConfig:
    """Tests for WardenConfig model."""

    def test_default_values(self):
        """Test default WardenConfig values."""
        config = WardenConfig()
        assert config.enabled is True
        assert config.mode == "core"
        assert config.risk_threshold == "medium"
        assert config.enable_predictions is True
        assert config.changelog_depth == 10

    def test_risk_threshold_values(self):
        """Test valid risk threshold values."""
        for threshold in ["low", "medium", "high", "critical"]:
            config = WardenConfig(risk_threshold=threshold)
            assert config.risk_threshold == threshold

    def test_invalid_risk_threshold(self):
        """Test invalid risk threshold rejection."""
        with pytest.raises(ValidationError):
            WardenConfig(risk_threshold="invalid")

    def test_changelog_depth_bounds(self):
        """Test changelog_depth bounds."""
        # Valid values
        assert WardenConfig(changelog_depth=1).changelog_depth == 1
        assert WardenConfig(changelog_depth=100).changelog_depth == 100

        # Invalid values
        with pytest.raises(ValidationError):
            WardenConfig(changelog_depth=0)

        with pytest.raises(ValidationError):
            WardenConfig(changelog_depth=101)


class TestProjectOverride:
    """Tests for ProjectOverride model."""

    def test_basic_creation(self):
        """Test basic ProjectOverride creation."""
        config = ProjectOverride(project_path="/home/user/project")
        assert config.project_path == "/home/user/project"
        assert config.sentinel is None
        assert config.architect is None
        assert config.warden is None

    def test_with_overrides(self):
        """Test ProjectOverride with agent overrides."""
        sentinel_override = SentinelConfig(rules=["strict"])
        config = ProjectOverride(
            project_path="/home/user/project",
            sentinel=sentinel_override,
            architect=ArchitectConfig(max_lines_per_function=30)
        )
        assert config.sentinel is not None
        assert config.sentinel.rules == ["strict"]
        assert config.architect is not None
        assert config.architect.max_lines_per_function == 30
        assert config.warden is None


class TestUnifiedConfig:
    """Tests for UnifiedConfig root model."""

    def test_default_values(self):
        """Test default UnifiedConfig values."""
        config = UnifiedConfig()
        assert config.version == "1.0.0"
        assert "mode" in config.global_config
        assert "llm" in config.global_config
        assert "sentinel" in config.agents
        assert "architect" in config.agents
        assert "warden" in config.agents
        assert isinstance(config.agents["sentinel"], SentinelConfig)
        assert isinstance(config.agents["architect"], ArchitectConfig)
        assert isinstance(config.agents["warden"], WardenConfig)

    def test_version_validation(self):
        """Test version format validation."""
        # Valid versions
        assert UnifiedConfig(version="1.0.0").version == "1.0.0"
        assert UnifiedConfig(version="2.5.3").version == "2.5.3"
        assert UnifiedConfig(version="10.0.0").version == "10.0.0"

        # Invalid versions
        with pytest.raises(ValidationError):
            UnifiedConfig(version="invalid")

        with pytest.raises(ValidationError):
            UnifiedConfig(version="1.0")

        with pytest.raises(ValidationError):
            UnifiedConfig(version="v1.0.0")

    def test_agent_accessors(self):
        """Test agent getter methods."""
        config = UnifiedConfig()

        sentinel = config.get_agent_config("sentinel")
        assert isinstance(sentinel, SentinelConfig)

        architect = config.get_agent_config("architect")
        assert isinstance(architect, ArchitectConfig)

        warden = config.get_agent_config("warden")
        assert isinstance(warden, WardenConfig)

        # Non-existent agent
        assert config.get_agent_config("unknown") is None

    def test_project_overrides(self):
        """Test project override handling."""
        override = ProjectOverride(
            project_path="/home/user/project",
            sentinel=SentinelConfig(rules=["custom"])
        )
        config = UnifiedConfig(projects={"/home/user/project": override})

        retrieved = config.get_project_override("/home/user/project")
        assert retrieved is not None
        assert retrieved.sentinel.rules == ["custom"]

        # Non-existent project
        assert config.get_project_override("/nonexistent") is None

    def test_resolve_llm_config(self):
        """Test LLM config resolution with fallback."""
        # Agent-specific LLM
        agent_llm = LLMConfig(provider="openai", model="gpt-4")
        sentinel = SentinelConfig(llm=agent_llm)
        config = UnifiedConfig(agents={"sentinel": sentinel})

        resolved = config.resolve_llm_config("sentinel")
        assert resolved is not None
        assert resolved.provider == "openai"
        assert resolved.model == "gpt-4"

        # Fall back to global config
        config2 = UnifiedConfig(
            global_config={
                "mode": "adk",
                "llm": {"provider": "gemini", "model": "flash"}
            },
            agents={"sentinel": SentinelConfig(llm=None)}
        )
        resolved2 = config2.resolve_llm_config("sentinel")
        assert resolved2 is not None
        assert resolved2.provider == "gemini"

    def test_custom_agent_configs(self):
        """Test creating UnifiedConfig with custom agent configurations."""
        config = UnifiedConfig(
            agents={
                "sentinel": SentinelConfig(
                    rules=["strict_mode"],
                    scan_on_startup=False
                ),
                "architect": ArchitectConfig(
                    max_lines_per_function=40,
                    forbidden_imports=["banned_lib"]
                ),
                "warden": WardenConfig(
                    risk_threshold="high",
                    changelog_depth=20
                )
            }
        )

        sentinel = config.agents["sentinel"]
        assert sentinel.rules == ["strict_mode"]
        assert sentinel.scan_on_startup is False

        architect = config.agents["architect"]
        assert architect.max_lines_per_function == 40
        assert architect.forbidden_imports == ["banned_lib"]

        warden = config.agents["warden"]
        assert warden.risk_threshold == "high"
        assert warden.changelog_depth == 20


class TestConfigValidationErrors:
    """Tests for validation error scenarios."""

    def test_invalid_provider(self):
        """Test that invalid provider is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(provider="invalid", model="test")
        assert "provider" in str(exc_info.value)

    def test_invalid_model_type(self):
        """Test that invalid model type is rejected."""
        with pytest.raises(ValidationError):
            LLMConfig(provider="ollama", model=123)  # Should be string

    def test_missing_required_fields(self):
        """Test missing required fields."""
        with pytest.raises(ValidationError):
            LLMConfig()  # Missing provider and model

    def test_nested_validation_error(self):
        """Test that nested model validation works."""
        with pytest.raises(ValidationError) as exc_info:
            UnifiedConfig(
                agents={
                    "architect": ArchitectConfig(max_lines_per_function=1000)  # Invalid value
                }
            )
        assert "max_lines_per_function" in str(exc_info.value) or "ValidationError" in str(type(exc_info.value))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
