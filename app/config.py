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
    warden_url: str = "http://127.0.0.1:4003"
    executor_url: str = "http://127.0.0.1:4004"
    notifier_url: str = "http://127.0.0.1:4100"

    # Google ADK (opcional)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    # Logging
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
