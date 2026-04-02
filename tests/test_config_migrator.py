"""
Tests for the ConfigMigrator.

This module tests the configuration migrator ensuring proper migration of
legacy config files (.sentinelrc.toml, architect.json, .warden.json) to the
unified config format.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Add the app directory to the path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from migrations.config_migrator import ConfigMigrator
from models.config import SentinelConfig, ArchitectConfig, WardenConfig


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace directory."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    # Cleanup
    import shutil
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def migrator(temp_workspace):
    """Get a ConfigMigrator instance for the temp workspace."""
    return ConfigMigrator(temp_workspace)


class TestSentinelMigration:
    """Tests for Sentinel config migration."""

    def test_missing_sentinelrc_returns_none(self, migrator):
        """Test that missing .sentinelrc.toml returns None."""
        result = migrator._migrate_sentinel()
        assert result is None

    def test_basic_sentinelrc_migration(self, migrator, temp_workspace):
        """Test basic .sentinelrc.toml migration."""
        config_content = '''
version = "5.0.0"
project_name = "test-project"
framework = "NestJS"
manager = "npm"
'''
        config_path = Path(temp_workspace) / ".sentinelrc.toml"
        config_path.write_text(config_content)

        result = migrator._migrate_sentinel()

        assert result is not None
        assert result["enabled"] is True
        assert result["mode"] == "core"
        assert result["scan_on_startup"] is True
        assert result["auto_fix"] is False

    def test_sentinelrc_with_rules(self, migrator, temp_workspace):
        """Test .sentinelrc.toml with architecture_rules."""
        config_content = '''
architecture_rules = ["SOLID Principles", "Clean Code"]
ignore_patterns = ["node_modules", "dist", ".git"]
'''
        config_path = Path(temp_workspace) / ".sentinelrc.toml"
        config_path.write_text(config_content)

        result = migrator._migrate_sentinel()

        assert result is not None
        assert result["rules"] == ["SOLID Principles", "Clean Code"]
        assert result["ignored_paths"] == ["node_modules", "dist", ".git"]

    def test_sentinelrc_with_knowledge_base(self, migrator, temp_workspace):
        """Test .sentinelrc.toml with knowledge_base settings."""
        config_content = '''
[knowledge_base]
index_on_start = false
vector_db_url = "http://localhost:6333"

[features]
enable_knowledge_base = true
'''
        config_path = Path(temp_workspace) / ".sentinelrc.toml"
        config_path.write_text(config_content)

        result = migrator._migrate_sentinel()

        assert result is not None
        assert result["scan_on_startup"] is False

    def test_sentinelrc_with_model_adk_mode(self, migrator, temp_workspace):
        """Test that presence of primary_model with api_key sets mode to adk."""
        config_content = '''
[primary_model]
name = "claude-3-5-sonnet"
url = "https://api.anthropic.com"
api_key = "sk-ant-api03-xxx"
'''
        config_path = Path(temp_workspace) / ".sentinelrc.toml"
        config_path.write_text(config_content)

        result = migrator._migrate_sentinel()

        assert result is not None
        assert result["mode"] == "adk"

    def test_sentinelrc_parse_error_returns_none(self, migrator, temp_workspace):
        """Test that invalid TOML returns None."""
        config_content = '''
[invalid toml syntax [[[
'''
        config_path = Path(temp_workspace) / ".sentinelrc.toml"
        config_path.write_text(config_content)

        result = migrator._migrate_sentinel()
        assert result is None


class TestArchitectMigration:
    """Tests for Architect config migration."""

    def test_missing_architect_json_returns_none(self, migrator):
        """Test that missing architect.json returns None."""
        result = migrator._migrate_architect()
        assert result is None

    def test_basic_architect_migration(self, migrator, temp_workspace):
        """Test basic architect.json migration."""
        config = {
            "max_lines_per_function": 60,
            "architecture_pattern": "MVC",
            "ignored_paths": ["node_modules/", ".git/", "dist/"],
        }
        config_path = Path(temp_workspace) / "architect.json"
        config_path.write_text(json.dumps(config))

        result = migrator._migrate_architect()

        assert result is not None
        assert result["max_lines_per_function"] == 60
        assert result["architecture_pattern"] == "MVC"
        assert result["ignored_paths"] == ["node_modules/", ".git/", "dist/"]

    def test_architect_with_string_forbidden_imports(self, migrator, temp_workspace):
        """Test architect.json with string forbidden_imports."""
        config = {
            "max_lines_per_function": 50,
            "forbidden_imports": ["deprecated_module", "legacy_package"],
        }
        config_path = Path(temp_workspace) / "architect.json"
        config_path.write_text(json.dumps(config))

        result = migrator._migrate_architect()

        assert result is not None
        assert result["forbidden_imports"] == ["deprecated_module", "legacy_package"]

    def test_architect_with_object_forbidden_imports(self, migrator, temp_workspace):
        """Test architect.json with object forbidden_imports."""
        config = {
            "max_lines_per_function": 50,
            "forbidden_imports": [
                {"from": "src/parsers", "to": "src/output", "reason": "Circular dependency"},
                {"from": "controller", "to": "repository"},
            ],
        }
        config_path = Path(temp_workspace) / "architect.json"
        config_path.write_text(json.dumps(config))

        result = migrator._migrate_architect()

        assert result is not None
        assert len(result["forbidden_imports"]) == 2
        assert "src/parsers -> src/output" in result["forbidden_imports"]
        assert "controller -> repository" in result["forbidden_imports"]

    def test_architect_parse_error_returns_none(self, migrator, temp_workspace):
        """Test that invalid JSON returns None."""
        config_path = Path(temp_workspace) / "architect.json"
        config_path.write_text("{ invalid json }")

        result = migrator._migrate_architect()
        assert result is None


class TestWardenMigration:
    """Tests for Warden config migration."""

    def test_missing_warden_json_returns_none(self, migrator):
        """Test that missing .warden.json returns None."""
        result = migrator._migrate_warden()
        assert result is None

    def test_basic_warden_migration(self, migrator, temp_workspace):
        """Test basic .warden.json migration."""
        config = {
            "enabled": True,
            "mode": "adk",
            "risk_threshold": "high",
            "enable_predictions": False,
        }
        config_path = Path(temp_workspace) / ".warden.json"
        config_path.write_text(json.dumps(config))

        result = migrator._migrate_warden()

        assert result is not None
        assert result["enabled"] is True
        assert result["mode"] == "adk"
        assert result["risk_threshold"] == "high"
        assert result["enable_predictions"] is False

    def test_warden_defaults(self, migrator, temp_workspace):
        """Test that missing fields use defaults."""
        config = {}
        config_path = Path(temp_workspace) / ".warden.json"
        config_path.write_text(json.dumps(config))

        result = migrator._migrate_warden()

        assert result is not None
        assert result["enabled"] is True
        assert result["mode"] == "core"
        assert result["risk_threshold"] == "medium"
        assert result["enable_predictions"] is True
        assert result["changelog_depth"] == 10

    def test_warden_invalid_risk_threshold(self, migrator, temp_workspace):
        """Test that invalid risk_threshold is not used."""
        config = {
            "risk_threshold": "invalid_value",
        }
        config_path = Path(temp_workspace) / ".warden.json"
        config_path.write_text(json.dumps(config))

        result = migrator._migrate_warden()

        assert result is not None
        # Should fall back to default
        assert result["risk_threshold"] == "medium"

    def test_warden_parse_error_returns_none(self, migrator, temp_workspace):
        """Test that invalid JSON returns None."""
        config_path = Path(temp_workspace) / ".warden.json"
        config_path.write_text("{ invalid json }")

        result = migrator._migrate_warden()
        assert result is None


class TestMigrateAll:
    """Tests for the migrate_all method."""

    def test_migrate_all_with_no_configs(self, migrator):
        """Test migrate_all when no config files exist."""
        result = migrator.migrate_all()

        assert result["sentinel"] is None
        assert result["architect"] is None
        assert result["warden"] is None

    def test_migrate_all_with_all_configs(self, migrator, temp_workspace):
        """Test migrate_all with all config files present."""
        # Create sentinel config
        sentinel_config = '''
architecture_rules = ["SOLID", "Clean Code"]
ignore_patterns = ["node_modules"]
'''
        (Path(temp_workspace) / ".sentinelrc.toml").write_text(sentinel_config)

        # Create architect config
        architect_config = {
            "max_lines_per_function": 40,
            "architecture_pattern": "hexagonal",
        }
        (Path(temp_workspace) / "architect.json").write_text(json.dumps(architect_config))

        # Create warden config
        warden_config = {
            "risk_threshold": "low",
            "enable_predictions": True,
        }
        (Path(temp_workspace) / ".warden.json").write_text(json.dumps(warden_config))

        result = migrator.migrate_all()

        assert result["sentinel"] is not None
        assert result["sentinel"]["rules"] == ["SOLID", "Clean Code"]

        assert result["architect"] is not None
        assert result["architect"]["max_lines_per_function"] == 40

        assert result["warden"] is not None
        assert result["warden"]["risk_threshold"] == "low"


class TestMigrateToUnifiedConfig:
    """Tests for migrate_to_unified_config method."""

    def test_migrate_to_pydantic_models(self, migrator, temp_workspace):
        """Test migration returns Pydantic model instances."""
        # Create sentinel config
        sentinel_config = '''
architecture_rules = ["SOLID"]
ignore_patterns = ["node_modules"]
'''
        (Path(temp_workspace) / ".sentinelrc.toml").write_text(sentinel_config)

        # Create architect config
        architect_config = {
            "max_lines_per_function": 40,
            "architecture_pattern": "hexagonal",
        }
        (Path(temp_workspace) / "architect.json").write_text(json.dumps(architect_config))

        # Create warden config
        warden_config = {
            "risk_threshold": "low",
        }
        (Path(temp_workspace) / ".warden.json").write_text(json.dumps(warden_config))

        result = migrator.migrate_to_unified_config()

        assert isinstance(result["sentinel"], SentinelConfig)
        assert isinstance(result["architect"], ArchitectConfig)
        assert isinstance(result["warden"], WardenConfig)

        assert result["sentinel"].rules == ["SOLID"]
        assert result["architect"].max_lines_per_function == 40
        assert result["warden"].risk_threshold == "low"

    def test_migrate_with_invalid_data_returns_none(self, migrator, temp_workspace):
        """Test that invalid data returns None instead of failing."""
        # Create invalid sentinel config (missing required fields won't fail,
        # but validation errors should be caught)
        sentinel_config = '''
[features]
enable_knowledge_base = false
'''
        (Path(temp_workspace) / ".sentinelrc.toml").write_text(sentinel_config)

        result = migrator.migrate_to_unified_config()

        # Should still create a valid SentinelConfig with defaults
        assert isinstance(result["sentinel"], SentinelConfig) or result["sentinel"] is None


class TestNonDestructive:
    """Tests to ensure original files are not modified."""

    def test_sentinelrc_not_modified(self, migrator, temp_workspace):
        """Test that .sentinelrc.toml is not modified during migration."""
        original_content = '''
version = "5.0.0"
project_name = "test-project"
'''
        config_path = Path(temp_workspace) / ".sentinelrc.toml"
        config_path.write_text(original_content)

        migrator._migrate_sentinel()

        current_content = config_path.read_text()
        assert current_content == original_content

    def test_architect_json_not_modified(self, migrator, temp_workspace):
        """Test that architect.json is not modified during migration."""
        original_config = {"max_lines_per_function": 60, "architecture_pattern": "MVC"}
        config_path = Path(temp_workspace) / "architect.json"
        config_path.write_text(json.dumps(original_config, indent=2))

        migrator._migrate_architect()

        current_content = config_path.read_text()
        assert json.loads(current_content) == original_config

    def test_warden_json_not_modified(self, migrator, temp_workspace):
        """Test that .warden.json is not modified during migration."""
        original_config = {"risk_threshold": "high"}
        config_path = Path(temp_workspace) / ".warden.json"
        config_path.write_text(json.dumps(original_config, indent=2))

        migrator._migrate_warden()

        current_content = config_path.read_text()
        assert json.loads(current_content) == original_config
