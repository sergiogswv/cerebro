"""
ConfigMigrator - Migrates legacy config files to unified format.

This module provides a non-destructive migrator that reads legacy configuration
files (.sentinelrc.toml, architect.json, .warden.json) and converts them to
unified config dictionaries without modifying the original files.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import toml

from models.config import SentinelConfig, ArchitectConfig, WardenConfig

logger = logging.getLogger(__name__)


class ConfigMigrator:
    """Migrates legacy config files to unified format.

    This class reads legacy configuration files from a workspace and converts
    them to unified config dictionaries. The original files are never modified.

    Attributes:
        workspace_root: Path to the workspace containing legacy configs
    """

    def __init__(self, workspace_root: str) -> None:
        """Initialize the migrator with a workspace root path.

        Args:
            workspace_root: Path to the workspace containing legacy configs
        """
        self.workspace_root = Path(workspace_root)

    def migrate_all(self) -> Dict[str, Optional[Dict[str, Any]]]:
        """Migrate all legacy config files in the workspace.

        Returns:
            Dictionary with keys 'sentinel', 'architect', 'warden' containing
            the migrated config dicts or None if file doesn't exist or failed
        """
        return {
            "sentinel": self._migrate_sentinel(),
            "architect": self._migrate_architect(),
            "warden": self._migrate_warden(),
        }

    def _migrate_sentinel(self) -> Optional[Dict[str, Any]]:
        """Migrate .sentinelrc.toml to SentinelConfig dict.

        Returns:
            SentinelConfig as dict, or None if file not found or parse error
        """
        config_path = self.workspace_root / ".sentinelrc.toml"

        if not config_path.exists():
            return None

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                legacy = toml.load(f)
        except toml.TomlDecodeError as e:
            logger.error(f"Failed to parse {config_path}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error reading {config_path}: {e}")
            return None

        # Map legacy fields to new SentinelConfig fields
        migrated: Dict[str, Any] = {
            "enabled": True,  # Default, derived from features if present
            "mode": "core",
            "rules": [],
            "ignored_paths": [],
            "scan_on_startup": True,
            "auto_fix": False,
        }

        # Map fields from legacy config
        if "architecture_rules" in legacy:
            migrated["rules"] = legacy["architecture_rules"]

        if "ignore_patterns" in legacy:
            migrated["ignored_paths"] = legacy["ignore_patterns"]

        if "features" in legacy and isinstance(legacy["features"], dict):
            features = legacy["features"]
            # If enable_knowledge_base is False, might indicate disabled
            if features.get("enable_knowledge_base") is False:
                migrated["enabled"] = False

        if "knowledge_base" in legacy and isinstance(legacy["knowledge_base"], dict):
            kb = legacy["knowledge_base"]
            if "index_on_start" in kb:
                migrated["scan_on_startup"] = kb["index_on_start"]

        # Try to determine mode from primary_model presence
        if "primary_model" in legacy and isinstance(legacy["primary_model"], dict):
            model = legacy["primary_model"]
            if model.get("name") and model.get("api_key"):
                migrated["mode"] = "adk"

        return migrated

    def _migrate_architect(self) -> Optional[Dict[str, Any]]:
        """Migrate architect.json to ArchitectConfig dict.

        Returns:
            ArchitectConfig as dict, or None if file not found or parse error
        """
        config_path = self.workspace_root / "architect.json"

        if not config_path.exists():
            return None

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                legacy = json.load(f)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse {config_path}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error reading {config_path}: {e}")
            return None

        # Map legacy fields to new ArchitectConfig fields
        migrated: Dict[str, Any] = {
            "enabled": True,
            "mode": "core",
            "max_lines_per_function": 50,
            "architecture_pattern": "layered",
            "forbidden_imports": [],
            "ignored_paths": [],
        }

        # Map fields from legacy config
        if "max_lines_per_function" in legacy:
            migrated["max_lines_per_function"] = legacy["max_lines_per_function"]

        if "architecture_pattern" in legacy:
            migrated["architecture_pattern"] = legacy["architecture_pattern"]

        if "ignored_paths" in legacy:
            migrated["ignored_paths"] = legacy["ignored_paths"]

        # Handle forbidden_imports - can be list of strings or list of objects
        if "forbidden_imports" in legacy:
            imports = legacy["forbidden_imports"]
            if isinstance(imports, list):
                forbidden: list[str] = []
                for imp in imports:
                    if isinstance(imp, str):
                        forbidden.append(imp)
                    elif isinstance(imp, dict):
                        # Convert object format to string format
                        from_path = imp.get("from", "")
                        to_path = imp.get("to", "")
                        if from_path and to_path:
                            forbidden.append(f"{from_path} -> {to_path}")
                migrated["forbidden_imports"] = forbidden

        return migrated

    def _migrate_warden(self) -> Optional[Dict[str, Any]]:
        """Migrate .warden.json to WardenConfig dict.

        Returns:
            WardenConfig as dict, or None if file not found or parse error
        """
        config_path = self.workspace_root / ".warden.json"

        if not config_path.exists():
            return None

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                legacy = json.load(f)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse {config_path}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error reading {config_path}: {e}")
            return None

        # Map legacy fields to new WardenConfig fields
        migrated: Dict[str, Any] = {
            "enabled": True,
            "mode": "core",
            "risk_threshold": "medium",
            "enable_predictions": True,
            "changelog_depth": 10,
        }

        # Map fields from legacy config
        if "enabled" in legacy:
            migrated["enabled"] = legacy["enabled"]

        if "mode" in legacy:
            migrated["mode"] = legacy["mode"]

        if "risk_threshold" in legacy:
            # Validate and map risk threshold
            threshold = legacy["risk_threshold"]
            if threshold in ("low", "medium", "high", "critical"):
                migrated["risk_threshold"] = threshold

        if "enable_predictions" in legacy:
            migrated["enable_predictions"] = legacy["enable_predictions"]

        if "changelog_depth" in legacy:
            migrated["changelog_depth"] = legacy["changelog_depth"]

        return migrated

    def migrate_to_unified_config(
        self,
    ) -> Dict[str, Optional[SentinelConfig | ArchitectConfig | WardenConfig]]:
        """Migrate all configs and return as Pydantic model instances.

        Returns:
            Dictionary with keys 'sentinel', 'architect', 'warden' containing
            the migrated config objects or None if migration failed
        """
        migrated = self.migrate_all()
        result: Dict[str, Optional[SentinelConfig | ArchitectConfig | WardenConfig]] = {}

        if migrated["sentinel"]:
            try:
                result["sentinel"] = SentinelConfig(**migrated["sentinel"])
            except Exception as e:
                logger.error(f"Failed to create SentinelConfig from migrated data: {e}")
                result["sentinel"] = None
        else:
            result["sentinel"] = None

        if migrated["architect"]:
            try:
                result["architect"] = ArchitectConfig(**migrated["architect"])
            except Exception as e:
                logger.error(f"Failed to create ArchitectConfig from migrated data: {e}")
                result["architect"] = None
        else:
            result["architect"] = None

        if migrated["warden"]:
            try:
                result["warden"] = WardenConfig(**migrated["warden"])
            except Exception as e:
                logger.error(f"Failed to create WardenConfig from migrated data: {e}")
                result["warden"] = None
        else:
            result["warden"] = None

        return result
