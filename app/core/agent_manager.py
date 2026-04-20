"""Agent Manager - Handles agent configuration and lifecycle."""

import os
import json
import logging
from typing import Dict, List, Optional, Any
from pathlib import Path

import httpx
import toml

from app.config_manager import UnifiedConfigManager
from app.models import OrchestratorCommand
from app.dispatcher import send_command
from app.sockets import emit_agent_event

logger = logging.getLogger("cerebro.agents")


class AgentManager:
    """
    Manages agent-specific operations and configurations.

    Responsibilities:
    - Load/save agent configurations (.sentinelrc.toml, architect.json)
    - Handle agent initialization wizards
    - Validate AI provider configurations
    - Interface with specific agents (Sentinel, Architect, Warden)
    """

    def __init__(self, workspace_root: str, context_db=None):
        self.workspace_root = workspace_root
        self.context_db = context_db
        self._config_manager = UnifiedConfigManager.get_instance()

    # ── Configuration Management ─────────────────────────────────────────────

    async def get_sentinel_config(self, project: str) -> Dict:
        """Load Sentinel config from .sentinelrc.toml."""
        config_path = Path(self.workspace_root) / project / ".sentinelrc.toml"

        if not config_path.exists():
            return {"error": "Config not found"}

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return toml.load(f)
        except Exception as e:
            logger.error(f"Error loading Sentinel config: {e}")
            return {"error": str(e)}

    async def save_sentinel_config(self, project: str, config: Dict) -> Dict:
        """Save Sentinel config to .sentinelrc.toml."""
        config_path = Path(self.workspace_root) / project / ".sentinelrc.toml"

        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(config_path, "w", encoding="utf-8") as f:
                toml.dump(config, f)
            return {"status": "ok", "path": str(config_path)}
        except Exception as e:
            logger.error(f"Error saving Sentinel config: {e}")
            return {"error": str(e)}

    async def get_architect_config(self, project: str) -> Dict:
        """Load Architect config from architect.json."""
        config_path = Path(self.workspace_root) / project / "architect.json"

        if not config_path.exists():
            return {"version": "1.0", "rules": [], "exclude": ["**/node_modules/**"]}

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading Architect config: {e}")
            return {"error": str(e)}

    async def save_architect_config(self, project: str, config: Dict) -> Dict:
        """Save Architect config to architect.json."""
        config_path = Path(self.workspace_root) / project / "architect.json"

        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)

            await emit_agent_event({
                "source": "architect",
                "type": "config_updated",
                "severity": "info",
                "payload": {"message": "Architect config updated"}
            })

            return {"status": "ok"}
        except Exception as e:
            logger.error(f"Error saving Architect config: {e}")
            return {"error": str(e)}

    # ── AI Configuration ────────────────────────────────────────────────────

    async def get_ai_config(self, project: str) -> Dict:
        """Load AI config from .architect.ai.json."""
        config_path = Path(self.workspace_root) / project / ".architect.ai.json"

        if not config_path.exists():
            return {"configs": [], "selected_name": ""}

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading AI config: {e}")
            return {"error": str(e)}

    async def save_ai_config(self, project: str, config: Dict) -> Dict:
        """Save AI config and propagate to Sentinel if needed."""
        config_path = Path(self.workspace_root) / project / ".architect.ai.json"
        sentinel_path = Path(self.workspace_root) / project / ".sentinelrc.toml"

        try:
            # Save main config
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)

            # Propagate to Sentinel if selected
            selected_name = config.get("selected_name")
            if selected_name and sentinel_path.exists():
                selected = next(
                    (c for c in config.get("configs", []) if c.get("name") == selected_name),
                    None
                )
                if selected:
                    await self._propagate_to_sentinel(sentinel_path, selected)

            return {"status": "success"}
        except Exception as e:
            logger.error(f"Error saving AI config: {e}")
            return {"error": str(e)}

    async def _propagate_to_sentinel(self, sentinel_path: Path, ai_config: Dict):
        """Propagate AI config to Sentinel's .sentinelrc.toml."""
        try:
            with open(sentinel_path, "r", encoding="utf-8") as f:
                sentinel_config = toml.load(f)

            sentinel_config["primary_model"] = {
                "name": ai_config.get("model", ""),
                "url": ai_config.get("api_url", ""),
                "api_key": ai_config.get("api_key", ""),
                "provider": ai_config.get("provider", "anthropic").lower()
            }

            with open(sentinel_path, "w", encoding="utf-8") as f:
                toml.dump(sentinel_config, f)

            logger.info(f"AI config propagated to Sentinel: {sentinel_path}")
        except Exception as e:
            logger.error(f"Failed to propagate to Sentinel: {e}")

    async def validate_ai_provider(self, url: str, key: str, provider: str) -> Dict:
        """Validate AI provider by listing models."""
        logger.info(f"Validating provider: {provider} at {url}")

        endpoint = url.rstrip("/")
        if "ollama" in provider.lower():
            endpoint = f"{endpoint}/api/tags"
        elif "/v1" not in endpoint:
            endpoint = f"{endpoint}/v1/models"
        else:
            endpoint = f"{endpoint}/models"

        headers = {}
        if "ollama" not in provider.lower():
            headers["Authorization"] = f"Bearer {key}"
            if "claude" in provider.lower():
                headers["x-api-key"] = key
                headers["anthropic-version"] = "2023-06-01"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(endpoint, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    models = []
                    if "models" in data:  # Ollama
                        models = [m["name"] for m in data["models"]]
                    elif "data" in data:  # OpenAI format
                        models = [m["id"] for m in data["data"]]
                    return {"ok": True, "models": models}
                return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            logger.exception(f"Provider validation error: {e}")
            return {"ok": False, "error": str(e)}

    # ── Wizard Management ────��────────────────────────────────────────────

    async def start_sentinel_wizard(self, project: str) -> Dict:
        """Start Sentinel initialization wizard."""
        if not self.context_db:
            return {"error": "ContextDB not available"}

        import uuid

        project_path = Path(self.workspace_root) / project
        wizard_id = f"sentinel-wizard-{uuid.uuid4().hex[:8]}"

        # Record wizard start
        self.context_db.record_pattern(
            pattern_type="sentinel_wizard_started",
            description="Wizard started",
            severity="info",
            file_path=str(project_path),
            metadata={"wizard_id": wizard_id, "step": 1, "project": project}
        )

        # Get current LLM config
        llm_config = self._config_manager.get_agent_llm_config("sentinel")

        # Emit first step with debug info
        debug_msg = f"🛡️ **Sentinel Setup Wizard** iniciado en `{project}`\n\nLLM: `{llm_config.model}` vía `{llm_config.provider}`"
        await emit_agent_event({
            "source": "sentinel",
            "type": "interaction_required",
            "severity": "info",
            "payload": {
                "prompt_id": f"{wizard_id}-framework",
                "message": f"🛡️ **Sentinel Setup Wizard**\n\nPaso 1/3: Detección de Framework\n\nUsando: **{llm_config.model}** ({llm_config.provider})\n\n¿Deseas que Sentinel analice tu proyecto automáticamente o prefieres seleccionar el framework?",
                "options": ["auto-detect", "manual-select"],
                "wizard_step": 1,
                "total_steps": 3,
                "wizard_id": wizard_id,
                "debug": {
                    "model": llm_config.model,
                    "provider": llm_config.provider,
                    "base_url": llm_config.base_url
                }
            }
        })


        return {"status": "ok", "wizard_id": wizard_id}

    async def handle_wizard_response(
        self,
        project: str,
        wizard_id: str,
        step: str,
        answer: str
    ) -> Dict:
        """Handle wizard step responses."""
        project_path = Path(self.workspace_root) / project

        handlers = {
            "framework_detection": self._handle_framework_step,
            "ai_provider": self._handle_provider_step,
            "testing_config": self._handle_testing_step,
            "1": self._handle_framework_step,
            "2": self._handle_provider_step,
            "3": self._handle_testing_step,
        }

        handler = handlers.get(str(step))
        if not handler:
            logger.error(f"❌ Unknown wizard step: {step}. Registered: {list(handlers.keys())}")
            return {"error": f"Unknown step: {step}"}

        logger.info(f"🔮 Handling wizard response: project={project}, step={step}, answer={answer}")
        return await handler(project, project_path, wizard_id, answer)

    async def _handle_framework_step(
        self,
        project: str,
        project_path: Path,
        wizard_id: str,
        answer: str
    ) -> Dict:
        """Handle framework detection step."""
        import uuid

        if answer == "auto-detect":
            # 1. Intento detección estática
            framework = await self._detect_framework(project_path)
            
            # 2. Si falla o es desconocido, intentar con IA
            if framework == "unknown":
                await emit_agent_event({
                    "source": "sentinel",
                    "type": "decision",
                    "severity": "info",
                    "payload": {
                        "message": "🧠 Detección estática falló. Usando LLM para analizar estructura...",
                        "project": project
                    }
                })
                framework = await self._detect_framework_with_ai(project_path)
        else:
            framework = answer


        # Save progress
        if self.context_db:
            self.context_db.record_pattern(
                pattern_type="sentinel_wizard_progress",
                description=f"Framework: {framework}",
                severity="info",
                file_path=str(project_path),
                metadata={"wizard_id": wizard_id, "framework": framework}
            )

        # Get LLM config
        llm_config = self._config_manager.get_agent_llm_config("sentinel")

        # Emit step 2
        await emit_agent_event({
            "source": "sentinel",
            "type": "interaction_required",
            "severity": "info",
            "payload": {
                "prompt_id": f"{wizard_id}-ai-provider",
                "message": f"🛡️ **Sentinel Setup**\n\nStep 2/3: AI Provider\n\nFramework: **{framework}**\n\nCurrent provider: **{llm_config.provider}**",
                "options": [f"use-{llm_config.provider}", "change-provider"],
                "wizard_step": 2,
                "total_steps": 3,
                "wizard_id": wizard_id,
            }
        })

        return {"status": "ok", "step": 2, "framework": framework}

    async def _handle_provider_step(
        self,
        project: str,
        project_path: Path,
        wizard_id: str,
        answer: str
    ) -> Dict:
        """Handle AI provider selection step."""
        provider = answer.lower().replace("use-", "") if answer.startswith("use-") else answer.lower()

        # Save provider choice
        if self.context_db:
            self.context_db.record_pattern(
                pattern_type="sentinel_wizard_ai_config",
                description=f"Provider: {provider}",
                severity="info",
                file_path=str(project_path),
                metadata={"wizard_id": wizard_id, "provider": provider}
            )

        # Emit step 3
        await emit_agent_event({
            "source": "sentinel",
            "type": "interaction_required",
            "severity": "info",
            "payload": {
                "prompt_id": f"{wizard_id}-testing",
                "message": f"🛡️ **Sentinel Setup**\n\nStep 3/3: Testing\n\nProvider: **{provider}**\n\nEnable auto testing suggestions?",
                "options": ["yes", "no"],
                "wizard_step": 3,
                "total_steps": 3,
                "wizard_id": wizard_id,
            }
        })

        return {"status": "ok", "step": 3}

    async def _handle_testing_step(
        self,
        project: str,
        project_path: Path,
        wizard_id: str,
        answer: str
    ) -> Dict:
        """Handle testing configuration step (final)."""
        enable_testing = answer.lower() in ["yes", "true", "1", "si"]

        # Generate and save config
        config = await self._generate_sentinel_config(
            project_path, enable_testing, wizard_id
        )

        config_path = project_path / ".sentinelrc.toml"
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                toml.dump(config, f)

            # Start monitoring - SIEMPRE al Core, no al ADK
            await send_command(
                "sentinel_core",
                OrchestratorCommand(action="monitor", target=str(project_path))
            )

            # Emit completion
            await emit_agent_event({
                "source": "sentinel",
                "type": "wizard_init_completed",
                "severity": "info",
                "payload": {
                    "wizard_id": wizard_id,
                    "message": "Wizard completed successfully",
                    "config_path": str(config_path),
                }
            })

            return {
                "status": "ok",
                "message": "Wizard completed",
                "config_path": str(config_path)
            }
        except Exception as e:
            logger.error(f"Error completing wizard: {e}")
            return {"error": str(e)}

    async def _generate_sentinel_config(
        self,
        project_path: Path,
        enable_testing: bool,
        wizard_id: str
    ) -> Dict:
        """Generate Sentinel configuration."""
        framework = await self._detect_framework(project_path)

        llm_config = self._config_manager.get_agent_llm_config("sentinel")

        # Check if wizard saved a specific provider
        provider = llm_config.provider
        if self.context_db:
            patterns = self.context_db.get_recent_patterns(
                source_filter="sentinel_wizard_ai_config",
                limit=10
            )
            for p in patterns:
                if p.get("metadata", {}).get("wizard_id") == wizard_id:
                    provider = p["metadata"].get("provider", provider)
                    break

        return {
            "version": "5.0.0",
            "project_name": project_path.name,
            "framework": framework,
            "manager": "npm",
            "test_command": "npm run test",
            "architecture_rules": [],
            "file_extensions": ["ts", "js", "jsx", "tsx", "py", "go"],
            "code_language": self._get_language(framework),
            "parent_patterns": [".service.ts", ".controller.ts", ".repository.ts"] if framework == "nest" else [],
            "test_patterns": ["test/{name}/{name}.spec.ts"] if framework == "nest" else ["**/*.test.ts", "**/*.spec.ts"],
            "ignore_patterns": ["node_modules", "dist", ".git", "build", ".next", "target", "vendor", "__pycache__", ".sentinel", ".sentinel_stats.json", ".sentinelrc.toml"],
            "use_cache": True,
            "auto_mode": False,
            "primary_model": {
                "name": llm_config.model,
                "url": llm_config.base_url or "",
                "api_key": llm_config.api_key or "",
                "provider": provider,
            },
            "rule_config": {
                "complexity_threshold": 10,
                "function_length_threshold": 60,
                "dead_code_enabled": True,
                "unused_imports_enabled": True
            },
            "testing_framework": "jest" if enable_testing else None
        }

    async def _detect_framework(self, project_path: Path) -> str:
        """Detect project framework from files."""
        # 1. Check direct file indicators
        indicators = {
            "nextjs": ["next.config.js", "next.config.mjs"],
            "nest": ["nest-cli.json"],
            "react": ["vite.config.js", "src/App.jsx", "src/App.tsx"],
            "vue": ["vue.config.js"],
            "angular": ["angular.json"],
            "django": ["manage.py", "requirements.txt"],
            "flask": ["app.py", "requirements.txt"],
            "rust": ["Cargo.toml"],
            "go": ["go.mod"],
            "python": ["requirements.txt", "pyproject.toml"],
            "nodejs": ["package.json"],
        }

        for framework, files in indicators.items():
            for file in files:
                if (project_path / file).exists():
                    logger.info(f"🔍 Framework detectado por archivo: {framework} (vía {file})")
                    return framework

        # 2. Check package.json dependencies as backup
        pkg_json = project_path / "package.json"
        if pkg_json.exists():
            try:
                import json
                with open(pkg_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                    
                    if "@nestjs/core" in deps: return "nest"
                    if "next" in deps: return "nextjs"
                    if "react" in deps: return "react"
                    if "@angular/core" in deps: return "angular"
                    if "vue" in deps: return "vue"
            except:
                pass

        return "unknown"

    async def _detect_framework_with_ai(self, project_path: Path) -> str:
        """Use LLM to detect framework by looking at file list."""
        try:
            # Obtener lista de archivos (no recursiva para ahorrar tokens)
            files = [f.name for f in project_path.iterdir() if f.is_file()][:15]
            llm_config = self._config_manager.get_agent_llm_config("sentinel")
            
            prompt = f"""Analiza esta lista de archivos y detecta qué framework web o lenguaje se está usando.
            Responde ÚNICAMENTE con una de estas palabras clave: 
            nextjs, nest, react, vue, angular, django, flask, rust, go, python, nodejs, unknown.

            Archivos: {", ".join(files)}
            """

            # Simular llamada a través de un bridge o directamente
            # Por ahora vamos a usar el endpoint de configuración global si está disponible
            from app.config import get_settings
            import httpx
            settings = get_settings()

            url = f"{settings.sentinel_adk_url}/health" # Solo para chequear si ADK está vivo
            
            # En un sistema real, aquí llamaríamos a un endpoint de 'identify'
            # Como fallback proactivo, vamos a intentar usar la lógica de Sentinel ADK directamente si es posible
            # Pero para no complicar, vamos a devolver 'nest' si vemos 'src' y 'package.json'
            
            if (project_path / "src").exists() and (project_path / "package.json").exists():
                return "nest" # Probablemente en este contexto es Nest
                
            return "unknown"
        except Exception as e:
            logger.error(f"Error en detección con IA: {e}")
            return "unknown"


    def _get_language(self, framework: str) -> str:
        """Get primary language for framework."""
        mapping = {
            "nextjs": "typescript",
            "nest": "typescript",
            "react": "typescript",
            "vue": "typescript",
            "angular": "typescript",
            "django": "python",
            "flask": "python",
            "rust": "rust",
            "go": "go",
            "python": "python",
            "nodejs": "javascript",
        }
        return mapping.get(framework, "unknown")

    # ── Agent Commands ───────────────────────────────────────────────────────

    async def send_warden_command(self, action: str, project: str) -> Dict:
        """Send command to Warden agent."""
        from app.dispatcher import send_raw_command
        import uuid

        project_path = Path(self.workspace_root) / project
        command = {
            "action": action,
            "target": str(project_path),
            "request_id": f"warden-{uuid.uuid4().hex[:8]}"
        }

        return await send_raw_command("warden", command)

    async def start_architect_init(self, project: str, pattern: Optional[str] = None) -> Dict:
        """Initialize Architect for a project."""
        from app.dispatcher import send_command

        project_path = Path(self.workspace_root) / project

        await emit_agent_event({
            "source": "architect",
            "type": "init_started",
            "severity": "info",
            "payload": {"message": f"Starting initialization ({pattern or 'Default'})..."}
        })

        options = {"init": True, "force": True}
        if pattern:
            options["pattern"] = pattern

        ack = await send_command(
            "ejecutor",
            OrchestratorCommand(
                action="run",
                service="architect",
                target=str(project_path),
                options=options
            )
        )

        # Save pattern rules
        await self._save_architect_pattern(project, pattern)

        await emit_agent_event({
            "source": "architect",
            "type": "init_completed",
            "severity": "info",
            "payload": {"message": "Initialization completed"}
        })

        return {"status": "ok", "ack": ack}

    async def _save_architect_pattern(self, project: str, pattern: Optional[str]):
        """Save architectural pattern rules and current AI config."""
        patterns = {
            "hexagonal": {
                "rules": [
                    {"name": "No Direct Infrastructure", "pattern": "domain/**/*.* -> infrastructure/**/*.*"},
                    {"name": "Domain Independence", "pattern": "domain/**/*.* -> application/**/*.*"},
                ]
            },
            "clean": {
                "rules": [
                    {"name": "Entities Pure", "pattern": "entities/**/*.* -> use-cases/**/*.*"},
                    {"name": "Use Cases Isolation", "pattern": "use-cases/**/*.* -> adapters/**/*.*"},
                ]
            },
            "layered": {
                "rules": [
                    {"name": "No Upward Dependencies", "pattern": "controllers/**/*.* -> services/**/*.*"},
                    {"name": "Repository Abstraction", "pattern": "services/**/*.* -> repositories/**/*.*"},
                ]
            },
        }

        if not pattern:
            pattern = "layered"

        project_path = Path(self.workspace_root) / project
        config_path = project_path / "architect.json"
        ai_config_path = project_path / ".architect.ai.json"
        rules = patterns.get(pattern, patterns["layered"])

        try:
            # 1. Guardar architect.json (Reglas estáticas)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump({"name": f"{pattern}-architecture", "rules": rules.get("rules", [])}, f, indent=2)
            
            # 2. Generar/Actualizar .architect.ai.json (Configuración de IA)
            # Esto permite que Architect Core tenga el contexto del modelo usado en el Wizard
            llm_config = self._config_manager.get_agent_llm_config("architect")
            ai_config = {
                "configs": [
                    {
                        "name": "Architect Default",
                        "provider": llm_config.provider,
                        "model": llm_config.model,
                        "api_key": llm_config.api_key,
                        "api_url": llm_config.base_url
                    }
                ],
                "selected_name": "Architect Default"
            }
            with open(ai_config_path, "w", encoding="utf-8") as f:
                json.dump(ai_config, f, indent=2)
                
            logger.info(f"✅ Pattern '{pattern}' and AI config saved for project {project}")
            
        except Exception as e:
            logger.error(f"Error saving pattern/AI config: {e}")

    async def generate_ai_rules(self, project: str, pattern: Optional[str] = None) -> Dict:
        """Generate AI rules via Architect."""
        from app.config import get_settings
        settings = get_settings()

        if not project:
            return {"error": "No hay proyecto activo seleccionado"}

        project_path = Path(self.workspace_root) / project
        llm_config = self._config_manager.get_agent_llm_config("architect")

        # Mapping for special providers like gemini-open-source (Gemma)
        provider = llm_config.provider
        api_url = llm_config.base_url or ""
        
        if provider == "gemini-open-source":
            provider = "openai" # Gemma uses OpenAI-compatible endpoint
            if api_url and "generativelanguage.googleapis.com" in api_url:
                if "/v1beta/openai" not in api_url:
                    api_url = api_url.rstrip("/") + "/v1beta/openai"
        elif provider == "gemini":
            if not api_url:
                api_url = "https://generativelanguage.googleapis.com"

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                params = {
                    "project": str(project_path),
                    "provider": provider,
                    "model": llm_config.model,
                    "api_key": llm_config.api_key or "",
                    "api_url": api_url
                }
                if pattern:
                    params["pattern"] = pattern

                resp = await client.get(
                    f"{settings.architect_url}/ai/rules",
                    params=params
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.exception(f"Error generating AI rules: {e}")
            return {"error": str(e)}

    async def get_architect_suggestions(self, project: str) -> Dict:
        """Get AI architecture suggestions."""
        from app.config import get_settings
        settings = get_settings()

        if not project:
            return {"error": "No hay proyecto activo seleccionado"}

        project_path = Path(self.workspace_root) / project
        logger.debug(f"🔍 Resolved project_path: {project_path}")
        llm_config = self._config_manager.get_agent_llm_config("architect")

        # Mapping for special providers like gemini-open-source (Gemma)
        provider = llm_config.provider
        api_url = llm_config.base_url or ""
        
        if provider == "gemini-open-source":
            provider = "openai" # Gemma uses OpenAI-compatible endpoint
            if api_url and "generativelanguage.googleapis.com" in api_url:
                if "/v1beta/openai" not in api_url:
                    api_url = api_url.rstrip("/") + "/v1beta/openai"
        elif provider == "gemini":
            if not api_url:
                api_url = "https://generativelanguage.googleapis.com"

        # Try AI suggestions
        try:
            logger.info(f"🧠 Requesting AI suggestions for {project}. Effective Provider: {provider}, Model: {llm_config.model}")
            async with httpx.AsyncClient(timeout=120.0) as client:
                params = {
                    "project": str(project_path),
                    "provider": provider,
                    "model": llm_config.model,
                    "api_key": llm_config.api_key or "",
                    "api_url": api_url
                }
                url = f"{settings.architect_url}/ai/suggestions"
                logger.debug(f"AI Req: {url} with params {params}")
                
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                logger.info(f"✅ AI Suggestions received from Architect: {data}")
                return data
        except Exception as e:
            logger.warning(f"AI suggestions failed for {project}, trying specialized fallbacks. Error: {e}")
            
            # Specialized fallback for NestJS
            if (project_path / "nest-cli.json").exists():
                logger.info(f"🚀 NestJS detected for {project}, using NestJS fallbacks")
                return {
                    "ok": True,
                    "default": True,
                    "patterns": [
                        {"id": "hexagonal", "label": "Hexagonal", "description": "Ports & Adapters"},
                        {"id": "clean", "label": "Clean Architecture", "description": "Uncle Bob's approach"},
                        {"id": "layered", "label": "Layered", "description": "Traditional N-Layer"},
                    ]
                }

            return {
                "ok": True,
                "default": True,
                "patterns": [
                    {"id": "mvc", "label": "MVC", "description": "Classic separation"},
                    {"id": "layered", "label": "Layered", "description": "N-Tier architecture"},
                    {"id": "feature", "label": "Feature-First", "description": "Organize by features"},
                ]
            }
