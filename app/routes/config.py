"""
routes/config.py — Unified configuration API endpoints.

These endpoints provide access to the global unified configuration system
that manages settings for Sentinel, Architect, and Warden agents.

Routes:
  GET  /config                   → Get full unified config
  GET  /config/{agent_name}      → Get agent config with resolved values
  POST /config/{agent_name}      → Update agent config
  GET  /config/{agent_name}/llm  → Get resolved LLM config for agent
  POST /config/{agent_name}/llm  → Update agent LLM config
  POST /config/global            → Update global config
  POST /config/reload            → Reload config from disk
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Literal, Optional
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.models import ApiResponse
from app.config_manager import UnifiedConfigManager
from app.models.config import LLMConfig, CerebroConfig

router = APIRouter(tags=["config"])
logger = logging.getLogger("cerebro.routes.config")

# Valid agent names
VALID_AGENTS = {"sentinel", "architect", "warden"}


def _log_debug(msg: str):
    """Log debug a archivo para poder ver sin consola."""
    log_file = Path.home() / ".cerebro" / "debug.log"
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            from datetime import datetime
            f.write(f"{datetime.now().isoformat()} - {msg}\n")
    except:
        pass
    logger.info(msg)


def _sync_legacy_config_files(agent_name: str, llm_config: Any, project_path: Optional[str] = None):
    """
    Sincroniza la configuración de LLM con los archivos legacy del proyecto.

    Genera:
    - .architect.ai.json para architect
    - .sentinelrc.toml para sentinel (sección [llm])

    Args:
        agent_name: Nombre del agente (sentinel, architect, warden)
        llm_config: Configuración resuelta del LLM
        project_path: Ruta al proyecto (opcional, usa active_project del orchestrator si no se provee)
    """
    _log_debug(f"[Legacy Sync] INICIANDO para {agent_name}")

    if not project_path:
        # Intentar obtener el proyecto activo del orchestrator
        try:
            from app.orchestrator import orchestrator
            logger.info(f"[Legacy Sync] Orchestrator active_project: {orchestrator.active_project}")
            logger.info(f"[Legacy Sync] Orchestrator workspace_root: {orchestrator.workspace_root}")
            if orchestrator.active_project:
                project_path = os.path.join(orchestrator.workspace_root, orchestrator.active_project)
                logger.info(f"[Legacy Sync] Project path calculado: {project_path}")
            else:
                _log_debug(f"[Legacy Sync] No hay proyecto activo en orchestrator")
                return
        except Exception as e:
            _log_debug(f"[Legacy Sync] Error obteniendo orchestrator: {e}")
            return

    if not project_path:
        _log_debug("[Legacy Sync] project_path es None")
        return

    if not os.path.exists(project_path):
        _log_debug(f"[Legacy Sync] La ruta del proyecto no existe: {project_path}")
        return

    _log_debug(f"[Legacy Sync] Proyecto existe: {project_path}, sincronizando {agent_name}")

    try:
        # Convertir objeto Pydantic a dict si es necesario
        if hasattr(llm_config, 'model_dump'):
            llm_dict = llm_config.model_dump()
        elif hasattr(llm_config, 'dict'):
            llm_dict = llm_config.dict()
        else:
            llm_dict = dict(llm_config)

        if agent_name == "architect":
            # Generar .architect.ai.json
            ai_config_path = os.path.join(project_path, ".architect.ai.json")

            # Mapear provider de unified config a formato legacy
            provider_mapping = {
                "ollama": "Ollama",
                "openai": "OpenAI",
                "gemini": "Gemini",
                "claude": "Claude"
            }
            legacy_provider = provider_mapping.get(llm_dict.get("provider", ""), "Ollama")

            legacy_config = {
                "configs": [{
                    "name": f"{legacy_provider} (from Global Config)",
                    "provider": legacy_provider,
                    "api_url": llm_dict.get("base_url", ""),
                    "api_key": llm_dict.get("api_key", ""),
                    "model": llm_dict.get("model", "")
                }],
                "selected_name": f"{legacy_provider} (from Global Config)"
            }

            with open(ai_config_path, "w", encoding="utf-8") as f:
                json.dump(legacy_config, f, indent=2)

            _log_debug(f"✅ Archivo legacy creado: {ai_config_path}")

        elif agent_name == "sentinel":
            # Crear o actualizar .sentinelrc.toml
            sentinel_path = os.path.join(project_path, ".sentinelrc.toml")
            try:
                import toml

                # Cargar config existente o crear nueva estructura por defecto
                if os.path.exists(sentinel_path):
                    with open(sentinel_path, "r", encoding="utf-8") as f:
                        config = toml.load(f)
                    _log_debug(f"[Legacy Sync] Archivo existente cargado: {sentinel_path}")
                else:
                    # Crear estructura por defecto para Sentinel
                    config = {
                        "sentinel": {
                            "mode": "core",
                            "auto_sync": True,
                            "memory_enabled": True
                        },
                        "monitor": {
                            "file_watcher": True,
                            "http_health_check": False
                        }
                    }
                    _log_debug(f"[Legacy Sync] Creando nuevo archivo: {sentinel_path}")

                # Actualizar sección [llm] con valores de global config
                config["llm"] = {
                    "provider": llm_dict.get("provider", "ollama"),
                    "model": llm_dict.get("model", ""),
                    "base_url": llm_dict.get("base_url", ""),
                    "api_key": llm_dict.get("api_key", "")
                }

                with open(sentinel_path, "w", encoding="utf-8") as f:
                    toml.dump(config, f)

                action = "actualizado" if os.path.exists(sentinel_path) else "creado"
                _log_debug(f"✅ Archivo legacy {action}: {sentinel_path}")
            except Exception as e:
                _log_debug(f"No se pudo crear/actualizar {sentinel_path}: {e}")

    except Exception as e:
        _log_debug(f"Error sincronizando archivos legacy: {e}")


# ─── Request/Response Models ──────────────────────────────────────────────────

class UpdateAgentConfigRequest(BaseModel):
    """Request body for updating agent configuration."""
    config: Dict[str, Any]


class UpdateGlobalConfigRequest(BaseModel):
    """Request body for updating global configuration."""
    model_config = {"extra": "ignore"}  # Ignore extra fields

    llm: Dict[str, Any] | None = None
    mode: Dict[str, str] | None = None


class UpdateLLMConfigRequest(BaseModel):
    """Request body for updating LLM configuration."""
    provider: Literal["ollama", "openai", "gemini", "claude", "custom"] | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


# ─── Helper Functions ─────────────────────────────────────────────────────────

def _get_config_manager() -> UnifiedConfigManager:
    """Get the singleton config manager instance."""
    return UnifiedConfigManager.get_instance()


def _validate_agent_name(agent_name: str) -> None:
    """Validate agent name and raise 400 if invalid."""
    if agent_name not in VALID_AGENTS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid agent_name: '{agent_name}'. Must be one of: {', '.join(sorted(VALID_AGENTS))}"
        )


# ─── Config Endpoints ─────────────────────────────────────────────────────────

@router.get("/config/debug", response_model=ApiResponse, summary="Debug info for legacy sync")
async def debug_legacy_sync():
    """Endpoint de debug para verificar el estado del legacy sync."""
    try:
        from app.orchestrator import orchestrator

        info = {
            "active_project": orchestrator.active_project,
            "workspace_root": orchestrator.workspace_root,
            "log_file": str(Path.home() / ".cerebro" / "debug.log"),
        }

        # Verificar si existe el archivo debug.log
        log_path = Path.home() / ".cerebro" / "debug.log"
        if log_path.exists():
            with open(log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
                info["last_logs"] = lines[-20:] if len(lines) > 20 else lines
        else:
            info["last_logs"] = ["No log file yet"]

        return ApiResponse(ok=True, data=info)
    except Exception as e:
        return ApiResponse(ok=False, message=str(e))


@router.get("/config", response_model=ApiResponse, summary="Get full unified configuration")
async def get_full_config():
    """Returns the complete unified configuration as JSON.

    Includes global config, agent-specific configs, and project overrides.
    """
    try:
        manager = _get_config_manager()
        config = manager.get_full_config()
        return ApiResponse(
            ok=True,
            message="Configuration retrieved successfully",
            data=config
        )
    except Exception as e:
        logger.exception("Error retrieving configuration")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/config/global", response_model=ApiResponse, summary="Update global configuration")
async def update_global_config_endpoint(request: Request):
    """Update the global configuration section.

    Body:
        llm: Optional dictionary with global LLM settings
        mode: Optional dictionary mapping agent names to modes

    Only provided fields are updated; others remain unchanged.
    """
    try:
        # Parse raw body for debugging
        body = await request.json()
        logger.debug(f"Received global config update: {body}")

        # Validate manually
        try:
            validated = UpdateGlobalConfigRequest(**body)
        except Exception as ve:
            logger.error(f"Validation error: {ve}")
            raise HTTPException(status_code=422, detail=f"Validation error: {ve}")

        manager = _get_config_manager()
        unified_config = manager.get_config()

        # Get current global config
        current_global = unified_config.global_config.copy()

        # Update with provided values
        update_dict = validated.model_dump(exclude_unset=True, exclude_none=True)
        new_global = {**current_global, **update_dict}

        # Save via manager
        manager.update_global_config(new_global)

        # Sincronizar archivos legacy para todos los agentes
        _log_debug("[Global Config] Iniciando sincronización legacy para todos los agentes")
        for agent in VALID_AGENTS:
            try:
                agent_llm = manager.get_agent_llm_config(agent)
                _log_debug(f"[Global Config] LLM para {agent}: {agent_llm}")
                if agent_llm:
                    _sync_legacy_config_files(agent, agent_llm)
            except Exception as e:
                _log_debug(f"[Global Config] Error sincronizando {agent}: {e}")

        _log_debug("Global configuration updated")
        return ApiResponse(
            ok=True,
            message="Global configuration updated",
            data={"global_config": new_global}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error updating global configuration")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# CEREBRO CONFIG ENDPOINTS (deben ir ANTES de las rutas dinámicas /config/{agent_name})
# ═══════════════════════════════════════════��═══════════════════════════════════

@router.get("/config/cerebro-test", summary="Test endpoint for cerebro config")
async def get_cerebro_config_test():
    """Test endpoint that returns hardcoded data."""
    return {
        "ok": True,
        "message": "Test endpoint working",
        "data": {"test": True}
    }


@router.get("/config/cerebro", summary="Get cerebro engine configuration")
async def get_cerebro_config():
    """Get Cerebro engine configuration including auto-start settings."""
    try:
        manager = _get_config_manager()
        unified_config = manager.get_config()

        # Get cerebro config or create defaults
        cerebro_config = unified_config.cerebro if hasattr(unified_config, 'cerebro') else None
        if cerebro_config is None:
            cerebro_config = CerebroConfig()

        # Get agent modes from cerebro config
        agent_modes = cerebro_config.agent_modes if hasattr(cerebro_config, 'agent_modes') else {}

        # Build response
        return {
            "ok": True,
            "message": "Cerebro configuration retrieved",
            "data": {
                "config": {
                    "auto_start_agents": cerebro_config.auto_start_agents,
                    "agent_modes": agent_modes,
                    "architect_mode": agent_modes.get("architect", "core"),
                    "warden_mode": agent_modes.get("warden", "core"),
                    "sentinel_mode": agent_modes.get("sentinel", "core"),
                    "auto_fix_enabled": cerebro_config.auto_fix_enabled,
                    "auto_fix_provider": cerebro_config.auto_fix_provider,
                    "auto_fix_model": cerebro_config.auto_fix_model,
                    "isolation_branch_prefix": cerebro_config.isolation_branch_prefix,
                    "require_approval_critical": cerebro_config.require_approval_critical,
                    "notifier_timeout_mins": cerebro_config.notifier_timeout_mins,
                    "chain_fallback_behavior": cerebro_config.chain_fallback_behavior,
                }
            }
        }
    except Exception as e:
        logger.exception("Error retrieving cerebro configuration")
        return {
            "ok": True,
            "message": f"Using defaults: {str(e)}",
            "data": {
                "config": {
                    "auto_start_agents": ["sentinel"],
                    "auto_fix_enabled": True,
                    "auto_fix_provider": "ollama",
                    "auto_fix_model": "qwen3:8b",
                    "isolation_branch_prefix": "skrymir-fix/",
                    "require_approval_critical": True,
                    "notifier_timeout_mins": 30,
                    "chain_fallback_behavior": "branch_and_wait",
                }
            }
        }


@router.post("/config/cerebro", summary="Update cerebro engine configuration")
async def update_cerebro_config(request: Request):
    """Update Cerebro engine configuration."""
    debug_log(f"=== CONFIG CEREBRO POST REQUEST ===")
    debug_log(f"Headers: {dict(request.headers)}")
    try:
        body = await request.json()
        debug_log(f"Parsed body: {body}")

        new_config = body.get("config", {})

        if not isinstance(new_config, dict):
            logger.warning(f"[Cerebro Config] Invalid format - not a dict: {type(new_config)}")
            return {"ok": True, "message": "Invalid config format - nothing saved", "data": None}

        logger.info(f"[Cerebro Config] Processing config: {new_config}")

        manager = _get_config_manager()
        unified_config = manager.get_config()

        # Ensure cerebro config exists
        cerebro_config = unified_config.cerebro if hasattr(unified_config, 'cerebro') else None
        if cerebro_config is None:
            cerebro_config = CerebroConfig()
            unified_config.cerebro = cerebro_config

        # Update auto-start agents
        if "auto_start_agents" in new_config:
            valid_agents = {"sentinel", "architect", "warden"}
            cerebro_config.auto_start_agents = [
                agent for agent in new_config["auto_start_agents"]
                if agent in valid_agents
            ]

        # Update agent modes
        if "agent_modes" in new_config:
            valid_modes = {"core", "adk"}
            for agent in ["sentinel", "architect", "warden"]:
                if agent in new_config["agent_modes"]:
                    mode = new_config["agent_modes"][agent]
                    if mode in valid_modes:
                        cerebro_config.agent_modes[agent] = mode
                        logger.info(f"[Cerebro Config] Updated {agent} mode to {mode}")

        # Update other settings
        if "auto_fix_enabled" in new_config:
            cerebro_config.auto_fix_enabled = bool(new_config["auto_fix_enabled"])
        if "auto_fix_provider" in new_config:
            cerebro_config.auto_fix_provider = new_config["auto_fix_provider"]
        if "auto_fix_model" in new_config:
            cerebro_config.auto_fix_model = new_config["auto_fix_model"]
        if "isolation_branch_prefix" in new_config:
            cerebro_config.isolation_branch_prefix = new_config["isolation_branch_prefix"]
        if "require_approval_critical" in new_config:
            cerebro_config.require_approval_critical = bool(new_config["require_approval_critical"])
        if "notifier_timeout_mins" in new_config:
            cerebro_config.notifier_timeout_mins = int(new_config["notifier_timeout_mins"])
        if "chain_fallback_behavior" in new_config:
            cerebro_config.chain_fallback_behavior = new_config["chain_fallback_behavior"]

        # Save config
        try:
            manager._config = unified_config
            manager._save()
            logger.info(f"[Cerebro Config] Saved successfully")
        except Exception as save_error:
            logger.exception(f"[Cerebro Config] Error saving: {save_error}")
            return {"ok": True, "message": f"Config processed but save failed: {str(save_error)}", "data": None}

        # Return success
        return {
            "ok": True,
            "message": "Cerebro configuration saved",
            "data": {"config": {
                "auto_start_agents": cerebro_config.auto_start_agents,
                "agent_modes": cerebro_config.agent_modes,
                "architect_mode": cerebro_config.agent_modes.get("architect", "core"),
                "warden_mode": cerebro_config.agent_modes.get("warden", "core"),
                "sentinel_mode": cerebro_config.agent_modes.get("sentinel", "core"),
                "auto_fix_enabled": cerebro_config.auto_fix_enabled,
                "auto_fix_provider": cerebro_config.auto_fix_provider,
                "auto_fix_model": cerebro_config.auto_fix_model,
                "isolation_branch_prefix": cerebro_config.isolation_branch_prefix,
                "require_approval_critical": cerebro_config.require_approval_critical,
                "notifier_timeout_mins": cerebro_config.notifier_timeout_mins,
                "chain_fallback_behavior": cerebro_config.chain_fallback_behavior,
            }}
        }
    except Exception as e:
        logger.exception(f"Error saving cerebro configuration: {e}")
        return {"ok": True, "message": f"Server error handled: {str(e)}", "data": None}


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT-SPECIFIC CONFIG ENDPOINTS (rutas dinámicas - deben ir DESPUÉS de las rutas estáticas)
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/config/{agent_name}", response_model=ApiResponse, summary="Get agent configuration")
async def get_agent_config(agent_name: str):
    """Returns agent-specific configuration with resolved LLM and mode.

    Path parameters:
        agent_name: sentinel, architect, or warden

    Response includes:
        - config: The agent's specific configuration
        - resolved_llm: The effective LLM config (agent-specific or global fallback)
        - resolved_mode: The effective mode (agent-specific or global fallback)
    """
    _validate_agent_name(agent_name)

    try:
        manager = _get_config_manager()
        unified_config = manager.get_config()

        # Get agent-specific config
        agent_config = unified_config.agents.get(agent_name)
        if agent_config is None:
            raise HTTPException(
                status_code=404,
                detail=f"Configuration not found for agent: {agent_name}"
            )

        # Get resolved LLM and mode
        resolved_llm = manager.get_agent_llm_config(agent_name)
        resolved_mode = manager.get_agent_mode(agent_name)

        return ApiResponse(
            ok=True,
            message=f"Configuration retrieved for {agent_name}",
            data={
                "agent_name": agent_name,
                "config": agent_config.model_dump(mode="json"),
                "resolved_llm": resolved_llm.model_dump(mode="json") if resolved_llm else None,
                "resolved_mode": resolved_mode
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error retrieving configuration for {agent_name}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/config/{agent_name}", response_model=ApiResponse, summary="Update agent configuration")
async def update_agent_config(agent_name: str, request: UpdateAgentConfigRequest):
    """Update configuration for a specific agent.

    Path parameters:
        agent_name: sentinel, architect, or warden

    Body:
        config: Dictionary with agent configuration values

    The new configuration is merged with existing values and saved to disk.
    The cache is invalidated after update.
    """
    _validate_agent_name(agent_name)

    try:
        manager = _get_config_manager()

        # Get current config for merging
        unified_config = manager.get_config()
        current_config = unified_config.agents.get(agent_name)

        if current_config is None:
            raise HTTPException(
                status_code=404,
                detail=f"Configuration not found for agent: {agent_name}"
            )

        # Merge configurations
        current_dict = current_config.model_dump()
        merged_dict = {**current_dict, **request.config}

        # Update via manager
        manager.update_agent_config(agent_name, merged_dict)

        logger.info(f"Configuration updated for agent: {agent_name}")
        return ApiResponse(
            ok=True,
            message=f"Configuration updated for {agent_name}",
            data={"agent_name": agent_name}
        )
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning(f"Validation error updating config for {agent_name}: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"Error updating configuration for {agent_name}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/config/{agent_name}/llm", response_model=ApiResponse, summary="Get resolved LLM configuration")
async def get_agent_llm_config(agent_name: str):
    """Returns the resolved LLM configuration for an agent.

    Path parameters:
        agent_name: sentinel, architect, or warden

    The resolved configuration considers:
    1. Agent-specific LLM config
    2. Global LLM config (fallback)
    3. Default values (final fallback)
    """
    _validate_agent_name(agent_name)

    try:
        manager = _get_config_manager()
        llm_config = manager.get_agent_llm_config(agent_name)

        return ApiResponse(
            ok=True,
            message=f"LLM configuration retrieved for {agent_name}",
            data={
                "agent_name": agent_name,
                "llm_config": llm_config.model_dump(mode="json")
            }
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"Error retrieving LLM configuration for {agent_name}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/config/{agent_name}/llm", response_model=ApiResponse, summary="Update agent LLM configuration")
async def update_agent_llm_config(agent_name: str, request: UpdateLLMConfigRequest):
    """Update the LLM configuration for a specific agent.

    Path parameters:
        agent_name: sentinel, architect, or warden

    Body: LLMConfig fields (all optional)
        - provider: ollama, openai, gemini, claude, custom
        - model: Model name
        - base_url: API base URL
        - api_key: API key (supports ${ENV_VAR} pattern)
        - temperature: Sampling temperature (0.0-2.0)
        - max_tokens: Maximum tokens to generate

    Only provided fields are updated; others remain unchanged.
    """
    _validate_agent_name(agent_name)

    try:
        manager = _get_config_manager()
        unified_config = manager.get_config()

        # Get current agent config
        agent_config = unified_config.agents.get(agent_name)
        if agent_config is None:
            raise HTTPException(
                status_code=404,
                detail=f"Configuration not found for agent: {agent_name}"
            )

        # Build new LLM config from request
        # Start with existing LLM or empty dict
        current_llm = agent_config.llm.model_dump() if agent_config.llm else {}

        # Update with provided values
        update_dict = request.model_dump(exclude_unset=True, exclude_none=True)
        new_llm_dict = {**current_llm, **update_dict}

        # Create LLMConfig to validate
        new_llm = LLMConfig(**new_llm_dict)

        # Update agent config with new LLM
        agent_dict = agent_config.model_dump()
        agent_dict["llm"] = new_llm.model_dump()

        # Save via manager
        manager.update_agent_config(agent_name, agent_dict)

        # Sincronizar archivos legacy del proyecto
        try:
            resolved_llm = manager.get_agent_llm_config(agent_name)
            if resolved_llm:
                _sync_legacy_config_files(agent_name, resolved_llm)
        except Exception as e:
            logger.debug(f"No se pudo sincronizar config legacy: {e}")

        logger.info(f"LLM configuration updated for agent: {agent_name}")
        return ApiResponse(
            ok=True,
            message=f"LLM configuration updated for {agent_name}",
            data={
                "agent_name": agent_name,
                "llm_config": new_llm.model_dump(mode="json")
            }
        )
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning(f"Validation error updating LLM config for {agent_name}: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"Error updating LLM configuration for {agent_name}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/config/reload", response_model=ApiResponse, summary="Reload configuration from disk")
async def reload_config():
    """Reload configuration from disk and clear cache.

    Use this endpoint when the configuration file may have been
    modified externally to force a fresh load.
    """
    try:
        manager = _get_config_manager()
        manager.reload()

        logger.info("Configuration reloaded from disk")
        return ApiResponse(
            ok=True,
            message="Configuration reloaded from disk",
            data=None
        )
    except Exception as e:
        logger.exception("Error reloading configuration")
        raise HTTPException(status_code=500, detail=str(e))


# Debug log file
DEBUG_LOG = Path.home() / ".cerebro" / "api_debug.log"

def debug_log(msg):
    """Write to debug log file."""
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            from datetime import datetime
            f.write(f"{datetime.now().isoformat()} - {msg}\n")
    except Exception:
        pass
