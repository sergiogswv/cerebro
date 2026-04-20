from pydantic_settings import BaseSettings
from functools import lru_cache
from pathlib import Path
import platform
import os
from enum import Enum

class SentinelMode(str, Enum):
    CORE_ONLY = "core_only"    # Solo análisis estático Rust (sin LLM)
    ADK_ONLY  = "adk_only"     # Solo análisis LLM via ADK
    HYBRID    = "hybrid"       # Core detecta, ADK analiza (modo actual pero con intención)

class ArchitectMode(str, Enum):
    CORE = "core"  # Directamente al Architect Core Rust (:4002)
    ADK  = "adk"   # Sidecar Python con LLM + memoria (:4012)

class WardenMode(str, Enum):
    CORE = "core"  # Directamente al Warden Core Rust (:4003)
    ADK  = "adk"   # Sidecar Python con LLM + memoria (:4013)

# Detectar ruta base dinámicamente según la ubicación de este archivo
# __file__ -> cerebro/app/config.py
# 1er dirname -> cerebro/app
# 2do dirname -> cerebro
# 3er dirname -> skrymir-suite (workspace root)
_default_workspace = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Settings(BaseSettings):
    # Servidor
    port: int = 4000
    orchestrator_mode: str = "rest"  # "rest" | "adk"
    workspace_root: str = _default_workspace

    # URLs de agentes
    sentinel_url: str = "http://127.0.0.1:4001"
    architect_url: str = "http://127.0.0.1:4002"
    warden_url: str = "http://127.0.0.1:4003"   # Core Rust (sin IA)
    executor_url: str = "http://127.0.0.1:4004"
    notifier_url: str = "http://127.0.0.1:4100"

    # Warden — modo de operación
    warden_mode: str = WardenMode.CORE.value
    warden_adk_url: str = "http://127.0.0.1:4013"

    # Architect — modo de operación
    architect_mode: str = ArchitectMode.CORE.value
    architect_adk_url: str = "http://127.0.0.1:4012"

    # Sentinel — modo de operación
    # "core_only"  → Solo análisis estático Rust (sin LLM)
    # "adk_only"   → Solo análisis LLM via ADK
    # "hybrid"     → Core Rust detecta cambios, ADK python los analiza con LLM
    sentinel_mode: str = SentinelMode.HYBRID.value
    sentinel_adk_url: str = "http://127.0.0.1:4011"

    # LLM para el sidecar Warden ADK
    # gemini | claude | openai  (debe coincidir con LLM_PROVIDER en warden_agent/.env)
    warden_llm_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    # Ollama para Warden
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"

    # Autofix / Aider Configuration
    autofix_llm_provider: str = "gemini"  # ollama | openai | anthropic | gemini
    autofix_llm_model: str = "gemma-4-31b-it"
    autofix_api_key: str = ""
    autofix_api_base: str = ""

    # Logging
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
