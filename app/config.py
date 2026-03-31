from pydantic_settings import BaseSettings
from functools import lru_cache
from pathlib import Path
import platform

# Detectar ruta base según sistema operativo
_system = platform.system()
if _system == "Windows":
    # Windows: usar el directorio del usuario o una ruta configurable
    _default_workspace = str(Path.home() / "Documents" / "dev")
elif _system == "Darwin":
    # macOS
    _default_workspace = str(Path.home() / "dev")
else:
    # Linux (mantener compatibilidad)
    _default_workspace = "/home/protec/Documentos/dev"


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

    # LLM para el sidecar Warden ADK
    # gemini | claude | openai  (debe coincidir con LLM_PROVIDER en warden_agent/.env)
    warden_llm_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    # Ollama para Warden
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"

    # Logging
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
