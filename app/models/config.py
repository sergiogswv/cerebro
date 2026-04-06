"""
Unified configuration models for the Skrymir multi-agent system.

This module provides Pydantic models for the global configuration that unifies
settings across Sentinel, Architect, and Warden agents.
"""

from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator, model_validator
import re


class LLMConfig(BaseModel):
    """LLM configuration for agents.

    Supports multiple providers with environment variable placeholder
    patterns for sensitive values like API keys.
    """

    provider: Literal["ollama", "openai", "gemini", "claude", "custom"] = Field(
        description="LLM provider to use"
    )
    model: str = Field(description="Model name to use")
    base_url: Optional[str] = Field(
        default=None,
        description="Base URL for the LLM API (e.g., for Ollama or custom endpoints)"
    )
    api_key: Optional[str] = Field(
        default=None,
        description="API key, supports ${ENV_VAR} pattern for environment variable substitution"
    )
    temperature: float = Field(
        default=0.7,
        ge=0.0,
        le=2.0,
        description="Sampling temperature (0.0-2.0)"
    )
    max_tokens: Optional[int] = Field(
        default=None,
        description="Maximum tokens to generate"
    )

    @field_validator("api_key")
    @classmethod
    def validate_env_placeholder(cls, v: Optional[str]) -> Optional[str]:
        """Validate that API key uses proper env placeholder pattern.

        Supports ${ENV_VAR} pattern for environment variable substitution.
        Plain strings are also accepted for backward compatibility.
        """
        if v is None or v == "":
            return v

        # Check for ${ENV_VAR} pattern
        env_pattern = r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$"
        match = re.match(env_pattern, v)

        if match:
            # Valid env placeholder pattern
            return v

        # Plain string is accepted but could be a security concern
        # We allow it for local development and non-sensitive configs
        return v

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, v: Optional[str]) -> Optional[str]:
        """Validate base URL format."""
        if v is None or v == "":
            return v

        # Basic URL validation
        if not v.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")

        return v

    @model_validator(mode="after")
    def validate_provider_config(self) -> "LLMConfig":
        """Validate provider-specific configuration."""
        if self.provider == "ollama" and not self.base_url:
            # Ollama requires base_url, default to localhost
            self.base_url = "http://localhost:11434"

        return self


class AgentConfig(BaseModel):
    """Base configuration for all agents.

    Provides common settings that apply to Sentinel, Architect, and Warden.
    """

    enabled: bool = Field(
        default=True,
        description="Whether the agent is enabled"
    )
    mode: Literal["core", "adk"] = Field(
        default="core",
        description="Agent mode: 'core' for native implementation, 'adk' for Python sidecar with LLM"
    )
    llm: Optional[LLMConfig] = Field(
        default=None,
        description="LLM configuration for ADK mode (uses global default if not set)"
    )


class SentinelConfig(AgentConfig):
    """Configuration for the Sentinel agent (code quality and standards)."""

    rules: List[str] = Field(
        default_factory=lambda: ["pep8", "type_hints", "docstrings"],
        description="Active linting and quality rules"
    )
    ignored_paths: List[str] = Field(
        default_factory=lambda: [".venv", "node_modules", "__pycache__", ".git", ".pytest_cache"],
        description="Paths to exclude from analysis"
    )
    scan_on_startup: bool = Field(
        default=True,
        description="Automatically scan workspace on startup"
    )
    auto_fix: bool = Field(
        default=False,
        description="Automatically apply fixes for detected issues"
    )


class ArchitectConfig(AgentConfig):
    """Configuration for the Architect agent (code structure and architecture)."""

    max_lines_per_function: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Maximum recommended lines per function"
    )
    architecture_pattern: str = Field(
        default="layered",
        description="Architecture pattern to enforce (e.g., layered, hexagonal, clean)"
    )
    forbidden_imports: List[str] = Field(
        default_factory=list,
        description="Import patterns that are not allowed"
    )
    ignored_paths: List[str] = Field(
        default_factory=lambda: [".venv", "node_modules", "__pycache__", ".git"],
        description="Paths to exclude from analysis"
    )


class WardenConfig(AgentConfig):
    """Configuration for the Warden agent (change management and risk assessment)."""

    risk_threshold: str = Field(
        default="medium",
        pattern="^(low|medium|high|critical)$",
        description="Minimum risk level to trigger review"
    )
    enable_predictions: bool = Field(
        default=True,
        description="Enable AI-powered change impact prediction"
    )
    changelog_depth: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Number of recent changes to include in context"
    )


class ProjectOverride(BaseModel):
    """Per-project configuration overrides.

    Allows specific projects to have different settings from the global defaults.
    """

    project_path: str = Field(
        description="Absolute or relative path to the project"
    )
    sentinel: Optional[SentinelConfig] = Field(
        default=None,
        description="Sentinel-specific overrides for this project"
    )
    architect: Optional[ArchitectConfig] = Field(
        default=None,
        description="Architect-specific overrides for this project"
    )
    warden: Optional[WardenConfig] = Field(
        default=None,
        description="Warden-specific overrides for this project"
    )


class CerebroConfig(BaseModel):
    """Configuration for the Cerebro orchestrator engine."""

    auto_start_agents: List[str] = Field(
        default_factory=lambda: ["sentinel"],
        description="List of agent names to auto-start when Cerebro initializes"
    )
    auto_fix_enabled: bool = Field(
        default=True,
        description="Allow auto-delegation to Executor for code fixes"
    )
    auto_fix_provider: str = Field(
        default="ollama",
        description="LLM provider for auto-fix operations"
    )
    auto_fix_model: str = Field(
        default="qwen3:8b",
        description="Model name for auto-fix operations"
    )
    isolation_branch_prefix: str = Field(
        default="skrymir-fix/",
        description="Prefix for git isolation branches"
    )
    require_approval_critical: bool = Field(
        default=True,
        description="Require human approval for critical changes"
    )
    notifier_timeout_mins: int = Field(
        default=30,
        description="Timeout for notifier approval requests"
    )
    chain_fallback_behavior: str = Field(
        default="branch_and_wait",
        description="Fallback behavior when no human response"
    )
    agent_modes: Dict[str, Literal["core", "adk"]] = Field(
        default_factory=lambda: {
            "sentinel": "core",
            "architect": "core",
            "warden": "core"
        },
        description="Mode configuration for each agent (core or adk)"
    )

    @field_validator("auto_start_agents")
    @classmethod
    def validate_auto_start_agents(cls, v: List[str]) -> List[str]:
        """Validate that auto-start agents are valid."""
        valid_agents = {"sentinel", "architect", "warden"}
        return [agent for agent in v if agent in valid_agents]


class UnifiedConfig(BaseModel):
    """Root configuration model for the entire Skrymir system.

    This model represents the complete configuration stored in
    ~/.cerebro/global.config.json
    """

    version: str = Field(
        default="1.0.0",
        pattern=r"^\d+\.\d+\.\d+$",
        description="Configuration schema version"
    )

    cerebro: CerebroConfig = Field(
        default_factory=CerebroConfig,
        description="Cerebro orchestrator engine configuration"
    )

    global_config: Dict[str, Any] = Field(
        default_factory=lambda: {
            "mode": "core",
            "llm": {
                "provider": "ollama",
                "model": "llama3.2",
                "temperature": 0.7
            }
        },
        description="Global default settings including mode and LLM config"
    )

    agents: Dict[str, Union[SentinelConfig, ArchitectConfig, WardenConfig]] = Field(
        default_factory=lambda: {
            "sentinel": SentinelConfig(),
            "architect": ArchitectConfig(),
            "warden": WardenConfig()
        },
        description="Agent-specific configurations"
    )

    projects: Dict[str, ProjectOverride] = Field(
        default_factory=dict,
        description="Per-project configuration overrides, keyed by project path"
    )

    @field_validator("agents")
    @classmethod
    def validate_agents(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        """Ensure required agent keys exist."""
        required = {"sentinel", "architect", "warden"}
        for key in required:
            if key not in v:
                # Create default config if missing
                if key == "sentinel":
                    v[key] = SentinelConfig()
                elif key == "architect":
                    v[key] = ArchitectConfig()
                elif key == "warden":
                    v[key] = WardenConfig()
        return v

    def get_agent_config(self, agent_name: str) -> Optional[AgentConfig]:
        """Get configuration for a specific agent.

        Args:
            agent_name: One of "sentinel", "architect", "warden"

        Returns:
            Agent configuration or None if not found
        """
        config = self.agents.get(agent_name)
        if config is None:
            return None

        # Cast to specific type based on agent name
        if agent_name == "sentinel":
            return SentinelConfig(**config.model_dump()) if isinstance(config, dict) else config
        elif agent_name == "architect":
            return ArchitectConfig(**config.model_dump()) if isinstance(config, dict) else config
        elif agent_name == "warden":
            return WardenConfig(**config.model_dump()) if isinstance(config, dict) else config

        return None

    def get_project_override(self, project_path: str) -> Optional[ProjectOverride]:
        """Get configuration override for a specific project.

        Args:
            project_path: Path to the project

        Returns:
            Project override or None if not configured
        """
        return self.projects.get(project_path)

    def resolve_llm_config(self, agent_name: str) -> Optional[LLMConfig]:
        """Resolve the effective LLM configuration for an agent.

        Falls back through: agent-specific -> global default -> None

        Args:
            agent_name: One of "sentinel", "architect", "warden"

        Returns:
            Resolved LLM configuration or None
        """
        agent_config = self.agents.get(agent_name)
        if agent_config and hasattr(agent_config, "llm") and agent_config.llm:
            return agent_config.llm

        # Fall back to global config
        global_llm = self.global_config.get("llm")
        if global_llm:
            return LLMConfig(**global_llm)

        return None
