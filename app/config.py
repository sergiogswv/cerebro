from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Servidor
    port: int = 4000
    orchestrator_mode: str = "rest"  # "rest" | "adk"

    # URLs de agentes
    sentinel_url: str = "http://localhost:4001"
    architect_url: str = "http://localhost:4002"
    warden_url: str = "http://localhost:4003"
    executor_url: str = "http://localhost:4004"
    notifier_url: str = "http://localhost:4100"

    # Google ADK (opcional)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    # Logging
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
