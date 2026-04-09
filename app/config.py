from pydantic_settings import BaseSettings
from functools import lru_cache
from pathlib import Path
import platform
import os

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
    # "core"  → llama directamente al Warden Core Rust (:4003)
    # "adk"   → llama al sidecar Python con LLM + memoria (:4013)
    warden_mode: str = "core"  # "core" | "adk"
    warden_adk_url: str = "http://127.0.0.1:4013"

    # Architect — modo de operación
    # "core"  → llama directamente al Architect Core Rust (:4002)
    # "adk"   → llama al sidecar Python con LLM + memoria (:4012)
    architect_mode: str = "core"  # "core" | "adk"
    architect_adk_url: str = "http://127.0.0.1:4012"

    # Sentinel — modo de operación
    # "core"  → llama directamente al Sentinel Core Rust (:4001) - Soporta monitoreo de archivos
    # "adk"   → llama al sidecar Python con LLM + memoria (:4011) - Solo análisis bajo demanda
    sentinel_mode: str = "core"  # "core" | "adk" — default "core" para monitoreo de archivos
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
