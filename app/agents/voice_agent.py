import logging
import asyncio
from typing import Optional
from app.config_manager import get_config_manager
from app.ai_utils import AIConfig, consultar_ia
from app.sockets import sio
import asyncio

logger = logging.getLogger("cerebro.agents.voice")

class VoiceAgent:
    """
    Isolated agent for voice interactions.
    Handles generating text content for voice output and orchestrating
    voice-related events.
    """

    def __init__(self):
        self.config_manager = get_config_manager()
        self._llm_config = None

    def _get_llm_config(self) -> Optional[AIConfig]:
        """Resolves the LLM configuration to use for voice generation."""
        try:
            # Use global LLM config for voice agent
            full_cfg = self.config_manager.get_full_config()
            global_llm = full_cfg.get("global_config", {}).get("llm")
            
            if not global_llm:
                logger.warning("No global LLM config found for VoiceAgent")
                return None
            
            return AIConfig(
                name="VoiceAgentLLM",
                provider=global_llm.get("provider", "gemini-open-source"),
                api_url=global_llm.get("base_url", "https://generativelanguage.googleapis.com"),
                api_key=global_llm.get("api_key", ""),
                model=global_llm.get("model", "gemma-4-31b-it")
            )
        except Exception as e:
            logger.error(f"Error resolving LLM config for VoiceAgent: {e}")
            return None

    async def _scan_system_state(self) -> dict:
        """Performs a quick diagnostic of the Skrymir suite state."""
        from app.config import get_settings
        from app.sockets import agent_ready_state
        import httpx
        
        settings = get_settings()
        state = {
            "executor_online": False,
            "active_project": "Ninguno",
            "agents_ready": [],
            "agents_offline": []
        }

        # 1. Check Executor
        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                resp = await client.get(f"{settings.ejecutor_url}/status")
                if resp.status_code == 200:
                    state["executor_online"] = True
        except:
            pass

        # 2. Check Active Project
        try:
            from app.orchestrator import orchestrator
            state["active_project"] = orchestrator.active_project or "Ninguno"
        except:
            pass

        # 3. Check Agents
        for agent, ready in agent_ready_state.items():
            if ready:
                state["agents_ready"].append(agent)
            else:
                state["agents_offline"].append(agent)

        return state

    async def generate_welcome(self):
        """Generates and emits a welcome message with real-time system context."""
        logger.info("🎙️ VoiceAgent: Performing system scan for welcome message...")
        
        system_state = await self._scan_system_state()
        ai_cfg = self._get_llm_config()
        
        if not ai_cfg:
            logger.error("Cannot generate welcome message: No AI configuration available")
            return

        # Build context-aware prompt
        status_desc = (
            f"- Executor: {'ONLINE' if system_state['executor_online'] else 'OFFLINE'}\n"
            f"- Proyecto Activo: {system_state['active_project']}\n"
            f"- Agentes Listos: {', '.join(system_state['agents_ready']) if system_state['agents_ready'] else 'Ninguno'}\n"
            f"- Agentes Offline: {', '.join(system_state['agents_offline']) if system_state['agents_offline'] else 'Todos'}"
        )

        prompt = (
            "Eres Cerebro, el núcleo de inteligencia de Skrymir Suite. Acabas de iniciar.\n"
            "CONTEXTO DEL SISTEMA ACTUAL:\n"
            f"{status_desc}\n\n"
            "TAREA:\n"
            "Genera un mensaje de bienvenida corto y profesional (máximo 2 frases).\n"
            "Menciona de forma natural el estado del sistema (ej: si el ejecutor está offline, o si ya hay un proyecto listo).\n"
            "Si todo está offline, anima al usuario a activar el entorno. Si un proyecto está activo, prepárate para la acción.\n"
            "Usa un tono futurista, elegante y seguro. Solo texto plano, sin emojis."
        )

        try:
            import uuid
            event_id = str(uuid.uuid4())
            message = await consultar_ia(prompt, ai_cfg)
            if message:
                message = message.strip()
                logger.info(f"🎙️ VoiceAgent Context Welcome: {message}")
                
                # Get voice preferences from config
                full_cfg = self.config_manager.get_full_config()
                voice_prefs = full_cfg.get("cerebro", {}).get("voice", {
                    "gender": "neutral",
                    "accent": "es-MX",
                    "rate": 0.92,
                    "pitch": 0.9
                })

                # Emit events with system metadata and voice preferences
                await sio.emit("system_notification", {
                    "id": event_id,
                    "title": "Cerebro Diagnostics",
                    "message": message,
                    "type": "info"
                })
                
                await sio.emit("voice_event", {
                    "id": event_id,
                    "text": message,
                    "agent": "cerebro",
                    "action": "welcome",
                    "system_state": system_state,
                    "voice_config": voice_prefs,
                    "timestamp": asyncio.get_event_loop().time()
                })
            else:
                logger.warning("VoiceAgent failed to generate context-aware welcome message")
        except Exception as e:
            logger.error(f"Error in VoiceAgent context welcome sequence: {e}")

# Global instance for initialization
voice_agent = VoiceAgent()
