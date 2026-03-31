import os
import json
import logging
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Dict, Any

from app.models import ApiResponse

router = APIRouter(tags=["config"])
logger = logging.getLogger("cerebro.routes.config")

CONFIG_FILE = Path("cerebro_settings.json")

def load_config() -> Dict[str, Any]:
    default_config = {
        "auto_fix_enabled": True,
        "auto_fix_provider": "ollama",
        "auto_fix_model": "qwen3:8b",
        "require_approval_critical": True,
        "isolation_branch_prefix": "skrymir-fix/",
        "notifier_timeout_mins": 30,
        "chain_fallback_behavior": "branch_and_wait",
        "auto_fix_max_retries": 3,
        "warden_mode": "core", # "core" | "adk"
        "warden_adk_url": "http://127.0.0.1:4013",
        "architect_mode": "core", # "core" | "adk"
        "architect_adk_url": "http://127.0.0.1:4012",
    }

    if not CONFIG_FILE.exists():
        return default_config

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Merge with defaults to ensure all keys exist
            return {**default_config, **data}
    except Exception as e:
        logger.error(f"Error cargando {CONFIG_FILE}: {e}")
        return default_config

def save_config(config_data: Dict[str, Any]):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=4)
    except Exception as e:
        logger.error(f"Error guardando {CONFIG_FILE}: {e}")
        raise

@router.get("/config", response_model=ApiResponse, summary="Obtener config global de Cerebro (modo agentes)")
async def get_config():
    """Retorna configuración clave: modos de agentes (core/adk)"""
    config = load_config()
    return ApiResponse(ok=True, message="Configuración obtenida", data={
        "architect_mode": config.get("architect_mode", "core"),
        "warden_mode": config.get("warden_mode", "core"),
        "auto_fix_enabled": config.get("auto_fix_enabled", True),
        "auto_fix_provider": config.get("auto_fix_provider", "ollama"),
    })

@router.get("/config/cerebro", response_model=ApiResponse, summary="Obtener config global completa de Cerebro")
async def get_cerebro_config():
    config = load_config()
    return ApiResponse(ok=True, message="Configuración obtenida", data={"config": config})

@router.post("/config/cerebro", response_model=ApiResponse, summary="Guardar config global de Cerebro")
async def set_cerebro_config(request: Request):
    try:
        body = await request.json()
        new_config = body.get("config")
        if not new_config:
            raise HTTPException(status_code=400, detail="Falta el campo 'config'")
        
        # Merge con lo existente
        current = load_config()
        updated = {**current, **new_config}
        
        save_config(updated)
        
        # Aquí podrías inyectar la config actualizada al Orchestrator o DecisionEngine en caliente
        # from app.orchestrator import orchestrator
        # orchestrator.settings.update(updated)
        
        logger.info("⚙️ Configuración global de Cerebro actualizada")
        return ApiResponse(ok=True, message="Configuración guardada", data={"config": updated})
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error guardando config")
        raise HTTPException(status_code=500, detail=str(e))
