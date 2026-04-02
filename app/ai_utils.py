"""
Módulo de IA para generación de reglas arquitectónicas
Similar a la implementación en Rust del CLI Architect
"""
import logging
import os
import re
import json
from typing import List, Dict, Any, Optional
import httpx

logger = logging.getLogger("cerebro.ai")

# ─── Estructuras de datos ─────────────────────────────────────────────────────

class AIConfig:
    """Configuración de un proveedor de IA"""
    def __init__(self, name: str, provider: str, api_url: str, api_key: str, model: str):
        self.name = name
        self.provider = provider  # Claude, OpenAI, Gemini, Ollama, etc.
        self.api_url = api_url
        self.api_key = api_key
        self.model = model

class SuggestedRule:
    """Regla sugerida por IA"""
    def __init__(self, from_pattern: str, to_pattern: str, reason: str):
        self.from_pattern = from_pattern
        self.to_pattern = to_pattern
        self.reason = reason

    def to_dict(self) -> dict:
        return {
            "from": self.from_pattern,
            "to": self.to_pattern,
            "reason": self.reason
        }

class AISuggestionResponse:
    """Respuesta completa de sugerencias de IA"""
    def __init__(self, pattern: str, suggested_max_lines: int, rules: List[SuggestedRule]):
        self.pattern = pattern
        self.suggested_max_lines = suggested_max_lines
        self.rules = rules

    def to_dict(self) -> dict:
        return {
            "pattern": self.pattern,
            "suggested_max_lines": self.suggested_max_lines,
            "rules": [r.to_dict() for r in self.rules]
        }

class ArchOption:
    """Opción de arquitectura para selección"""
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description
        }

# ─── Descubrimiento del proyecto ──────────────────────────────────────────────

def detect_framework(project_path: str) -> str:
    """Detecta el framework del proyecto basado en archivos de configuración"""
    if not os.path.exists(project_path):
        return "unknown"

    # NestJS
    if os.path.exists(os.path.join(project_path, "nest-cli.json")):
        return "NestJS"

    # Next.js
    if os.path.exists(os.path.join(project_path, "next.config.js")) or \
       os.path.exists(os.path.join(project_path, "next.config.mjs")):
        return "Next.js"

    # Angular
    if os.path.exists(os.path.join(project_path, "angular.json")):
        return "Angular"

    # React (Create React App o Vite)
    if os.path.exists(os.path.join(project_path, "vite.config.ts")) or \
       os.path.exists(os.path.join(project_path, "vite.config.js")):
        return "Vite+React"
    if os.path.exists(os.path.join(project_path, "package.json")):
        try:
            with open(os.path.join(project_path, "package.json"), "r", encoding="utf-8") as f:
                pkg = json.load(f)
                deps = pkg.get("dependencies", {})
                if "react" in deps:
                    if "next" in deps:
                        return "Next.js"
                    return "React"
                if "vue" in deps:
                    return "Vue"
                if "@angular/core" in deps:
                    return "Angular"
        except:
            pass

    # Django
    if os.path.exists(os.path.join(project_path, "manage.py")):
        return "Django"

    # Flask
    if os.path.exists(os.path.join(project_path, "requirements.txt")):
        try:
            with open(os.path.join(project_path, "requirements.txt"), "r", encoding="utf-8") as f:
                content = f.read().lower()
                if "flask" in content:
                    return "Flask"
                if "fastapi" in content:
                    return "FastAPI"
        except:
            pass

    # Spring Boot
    if os.path.exists(os.path.join(project_path, "pom.xml")):
        try:
            with open(os.path.join(project_path, "pom.xml"), "r", encoding="utf-8") as f:
                content = f.read().lower()
                if "spring-boot" in content:
                    return "Spring Boot"
        except:
            pass

    # Laravel
    if os.path.exists(os.path.join(project_path, "artisan")):
        return "Laravel"

    # Go (Gin, Echo, etc.)
    if os.path.exists(os.path.join(project_path, "go.mod")):
        return "Go"

    return "Generic"

def get_dependencies(project_path: str) -> List[str]:
    """Obtiene lista de dependencias del proyecto"""
    deps = []

    # package.json (Node.js)
    pkg_path = os.path.join(project_path, "package.json")
    if os.path.exists(pkg_path):
        try:
            with open(pkg_path, "r", encoding="utf-8") as f:
                pkg = json.load(f)
                deps.extend(pkg.get("dependencies", {}).keys())
                deps.extend(pkg.get("devDependencies", {}).keys())
        except:
            pass

    # requirements.txt (Python)
    req_path = os.path.join(project_path, "requirements.txt")
    if os.path.exists(req_path):
        try:
            with open(req_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        pkg_name = re.split(r'[=<>~!;]', line)[0].strip()
                        if pkg_name:
                            deps.append(pkg_name)
        except:
            pass

    # go.mod (Go)
    go_mod_path = os.path.join(project_path, "go.mod")
    if os.path.exists(go_mod_path):
        try:
            with open(go_mod_path, "r", encoding="utf-8") as f:
                in_require = False
                for line in f:
                    line = line.strip()
                    if line == "require (":
                        in_require = True
                        continue
                    if in_require and line == ")":
                        in_require = False
                        continue
                    if in_require and line and not line.startswith("//"):
                        pkg = line.split()[0] if line.split() else ""
                        if pkg:
                            deps.append(pkg)
        except:
            pass

    return list(set(deps))  # Eliminar duplicados

def get_folder_structure(project_path: str) -> List[str]:
    """Obtiene estructura de carpetas principales (excluyendo node_modules, etc.)"""
    folders = []
    ignored = {"node_modules", "__pycache__", ".git", "venv", "env", ".venv",
               "dist", "build", ".next", "target", "vendor", "bin", "obj"}

    src_path = os.path.join(project_path, "src")
    scan_path = src_path if os.path.isdir(src_path) else project_path

    for entry in os.scandir(scan_path):
        if entry.is_dir() and entry.name not in ignored and not entry.name.startswith("."):
            rel_path = os.path.relpath(entry.path, project_path).replace("\\", "/")
            folders.append(rel_path)

    return folders

def get_key_architectural_files(project_path: str) -> List[str]:
    """Obtiene archivos arquitectónicos clave (controllers, services, entities, etc.)"""
    key_files = []

    # Patrones para identificar archivos arquitectónicos
    ts_js_patterns = ["controller", "service", "entity", "repository", "dto",
                      "module", "handler", "resolver", "guard", "interceptor"]
    py_patterns = ["_controller", "_service", "_repository", "_model", "_view",
                   "_handler", "controller_", "service_"]

    src_path = os.path.join(project_path, "src")
    scan_path = src_path if os.path.isdir(src_path) else project_path

    for root, dirs, files in os.walk(scan_path):
        # Ignorar carpetas no relevantes
        dirs[:] = [d for d in dirs if d not in {"node_modules", "__pycache__", ".git",
                                                  "venv", "dist", "build", ".next"}]

        for file in files:
            file_lower = file.lower()
            rel_path = os.path.relpath(os.path.join(root, file), project_path).replace("\\", "/")

            # Verificar patrones
            is_key = False
            if any(p in file_lower for p in ts_js_patterns) and \
               (file.endswith(".ts") or file.endswith(".tsx") or file.endswith(".js")):
                is_key = True
            elif any(p in file_lower for p in py_patterns) and file.endswith(".py"):
                is_key = True

            if is_key:
                key_files.append(rel_path)
                if len(key_files) >= 20:  # Limitar a 20 archivos clave
                    return key_files

    return key_files

def get_project_context(project_path: str) -> Dict[str, Any]:
    """Obtiene contexto completo del proyecto para la IA"""
    return {
        "framework": detect_framework(project_path),
        "dependencies": get_dependencies(project_path),
        "folder_structure": get_folder_structure(project_path),
        "key_files": get_key_architectural_files(project_path)
    }

# ─── Consulta a APIs de IA ────────────────────────────────────────────────────

async def consultar_ia(prompt: str, ai_config: AIConfig) -> Optional[str]:
    """Consulta a la API de IA configurada"""
    logger.info(f"🔍 consultar_ia: Enviando prompt a {ai_config.name} ({ai_config.provider}) - {ai_config.api_url}")
    logger.debug(f"Prompt: {prompt[:200]}...")

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0, read=165.0)) as client:
            provider = ai_config.provider.lower()

            # Claude (Anthropic)
            if "claude" in provider or "anthropic" in provider:
                url = f"{ai_config.api_url.rstrip('/')}/v1/messages"
                headers = {
                    "x-api-key": ai_config.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                }
                body = {
                    "model": ai_config.model,
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content": prompt}]
                }
                logger.debug(f"POST {url} with headers: {headers.keys()}")
                logger.info(f"Enviando request a {url}")
                try:
                    resp = await client.post(url, headers=headers, json=body)
                    logger.info(f"Respuesta HTTP status: {resp.status_code}")
                    resp.raise_for_status()
                    data = resp.json()
                    logger.debug(f"Response data keys: {data.keys()}")
                    logger.debug(f"Content array: {data.get('content', [])}")
                    # Extraer texto del contenido (puede tener thinking + text)
                    content = data.get("content", [])
                    if not content:
                        logger.error("No content in response")
                        return None
                    # Buscar el bloque de tipo "text"
                    text_content = None
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            text_content = item.get("text")
                            break
                    # Fallback: usar el primer elemento si no hay tipo "text"
                    if not text_content:
                        text_content = content[0].get("text") if isinstance(content[0], dict) else str(content[0])
                    logger.info(f"Texto extraido: {text_content[:100] if text_content else 'None'}...")
                    return text_content
                except httpx.HTTPStatusError as e:
                    logger.error(f"HTTP error: {e} - Response: {e.response.text[:500]}")
                    return None
                except httpx.ReadTimeout as e:
                    logger.error(f"Read timeout: {e}")
                    return None
                except Exception as e:
                    logger.error(f"Error en request: {e}")
                    return None

            # Gemini (Google)
            elif "gemini" in provider:
                url = f"{ai_config.api_url.rstrip('/')}/v1beta/models/{ai_config.model}:generateContent?key={ai_config.api_key}"
                headers = {"content-type": "application/json"}
                body = {
                    "contents": [{"parts": [{"text": prompt}]}]
                }
                resp = await client.post(url, headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]

            # OpenAI compatible (OpenAI, Groq, Ollama, Kimi, DeepSeek)
            else:
                # Para Ollama, asegurar que la URL tenga /v1 si no la tiene
                base_url = ai_config.api_url.rstrip('/')
                if not base_url.endswith('/v1') and 'ollama' in ai_config.provider.lower():
                    base_url = f"{base_url}/v1"
                url = f"{base_url}/chat/completions"

                logger.info(f"🔍 [consultar_ia] Ollama URL: {url}")
                logger.info(f"🔍 [consultar_ia] Modelo: {ai_config.model}")

                headers = {"content-type": "application/json"}
                if ai_config.api_key:
                    headers["authorization"] = f"Bearer {ai_config.api_key}"
                body = {
                    "model": ai_config.model,
                    "messages": [
                        {"role": "system", "content": "Eres un experto en frameworks de desarrollo."},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.1
                }
                logger.info(f"🔍 [consultar_ia] Enviando request a {url}")
                resp = await client.post(url, headers=headers, json=body)
                logger.info(f"🔍 [consultar_ia] Status: {resp.status_code}")
                resp.raise_for_status()
                data = resp.json()
                logger.info(f"🔍 [consultar_ia] Respuesta recibida: {str(data)[:200]}")
                return data["choices"][0]["message"]["content"]

    except Exception as e:
        logger.error(f"🔍 [consultar_ia] Error consultando IA ({ai_config.name}): {e}")
        import traceback
        logger.error(f"🔍 [consultar_ia] Traceback: {traceback.format_exc()}")
        return None

async def consultar_ia_con_fallback(prompt: str, configs: List[AIConfig]) -> Optional[str]:
    """Intenta consultar varias IAs en orden hasta que una funcione"""
    if not configs:
        logger.warning("⚠️ No hay configs de IA para consultar")
        return None

    logger.info(f"🔍 [consultar_ia_con_fallback] Iniciando con {len(configs)} config(s)")

    for i, config in enumerate(configs):
        logger.info(f"🔍 [consultar_ia_con_fallback] Config {i+1}/{len(configs)}: {config.name} ({config.provider}) - URL: {config.api_url} - Model: {config.model}")
        try:
            result = await consultar_ia(prompt, config)
            if result:
                logger.info(f"✅ [consultar_ia_con_fallback] Config '{config.name}' respondió correctamente")
                return result
            logger.warning(f"❌ [consultar_ia_con_fallback] Config '{config.name}' retornó None")
        except Exception as e:
            logger.error(f"❌ [consultar_ia_con_fallback] Config '{config.name}' lanzó excepción: {e}")

    logger.error("🔍 [consultar_ia_con_fallback] TODAS las configs fallaron")
    return None

def extraer_json_flexible(text: str) -> Optional[str]:
    """Extrae JSON de una respuesta que puede contener texto adicional o markdown"""
    if not text:
        return None

    # Si hay bloques de código markdown, extraer contenido
    content = text
    if "```json" in text:
        parts = text.split("```json")
        if len(parts) > 1:
            content = parts[1].split("```")[0].strip()
    elif "```" in text:
        parts = text.split("```")
        if len(parts) > 1:
            content = parts[1].strip()

    # Encontrar inicio y fin del JSON
    start = content.find('{')
    end = content.rfind('}')

    if start == -1 or end == -1:
        return None

    json_str = content[start:end+1].strip()

    # Validar que sea JSON válido
    try:
        json.loads(json_str)
        return json_str
    except:
        return None

# ─── Funciones principales de sugerencia ──────────────────────────────────────

async def sugerir_top_6_arquitecturas(framework: str, ai_configs: List[AIConfig]) -> List[ArchOption]:
    """Sugiere top 6 arquitecturas basadas en el framework"""
    prompt = f"""Eres Arquitecto de Software Senior. Framework: '{framework}'.

Devuelve EXACTAMENTE 6 opciones de arquitectura ordenadas por relevancia para este framework.

FORMATO JSON EXACTO:
{{
  "options": [
    {{"name": "Hexagonal", "description": "Ports & Adapters - domain logic isolated from external concerns"}},
    {{"name": "Clean Architecture", "description": "Uncle Bob's layers: Entities → UseCases → Interface Adapters"}},
    {{"name": "Layered (N-Tier)", "description": "Presentation → Business Logic → Data Access layers"}},
    {{"name": "Feature-Based", "description": "Organize by features/modules, not by technical layers"}},
    {{"name": "Vertical Slice", "description": "Features as vertical slices containing all layers"}},
    {{"name": "Micro-frontends", "description": "Independent deployable frontend modules"}}
  ]
}}

IMPORTANTE:
- Devuelve EXACTAMENTE 6 opciones
- Elige las mejores para {framework}
- Descripciones concisas (max 60 chars)
- Solo JSON, sin texto extra"""

    response_text = await consultar_ia_con_fallback(prompt, ai_configs)
    logger.info(f"Respuesta IA (top 6): {response_text[:500] if response_text else 'None'}...")

    if not response_text:
        return []

    json_str = extraer_json_flexible(response_text)
    logger.info(f"JSON extraido: {json_str}")

    if not json_str:
        return []

    try:
        data = json.loads(json_str)
        options = data.get("options", [])
        logger.info(f"Options obtenidas: {len(options)}")
        return [ArchOption(opt["name"], opt["description"]) for opt in options[:6]]
    except Exception as e:
        logger.error(f"Error parseando options: {e}")
        return []

async def sugerir_reglas_para_patron(
    pattern_name: str,
    context: Dict[str, Any],
    ai_configs: List[AIConfig]
) -> Optional[AISuggestionResponse]:
    """Genera reglas específicas para un patrón seleccionado"""
    framework = context.get('framework', 'generico')
    prompt = f"""Patron: {pattern_name} ({framework}).
Responde SOLO JSON: {{"pattern":"{pattern_name}","suggested_max_lines":60,"rules":[{{"from":"X","to":"Y","reason":"porque"}}]}}
Genera 5-7 reglas de importaciones prohibidas."""

    response_text = await consultar_ia_con_fallback(prompt, ai_configs)
    logger.info(f"Respuesta IA (reglas para {pattern_name}): {response_text[:500] if response_text else 'None'}...")

    if not response_text:
        logger.warning(f"No se obtuvo respuesta para reglas de {pattern_name}")
        return None

    json_str = extraer_json_flexible(response_text)
    logger.info(f"JSON extraido (reglas): {json_str[:200] if json_str else 'None'}...")

    if not json_str:
        logger.warning(f"No se pudo extraer JSON para reglas de {pattern_name}")
        return None

    try:
        data = json.loads(json_str)
        rules = [
            SuggestedRule(r["from"], r["to"], r["reason"])
            for r in data.get("rules", [])
        ]
        logger.info(f"Reglas obtenidas: {len(rules)}")
        return AISuggestionResponse(
            pattern=data.get("pattern", pattern_name),
            suggested_max_lines=data.get("suggested_max_lines", 60),
            rules=rules
        )
    except Exception as e:
        logger.error(f"Error parseando respuesta de IA: {e}")
        return None

async def detectar_framework_con_ia(
    project_path: str,
    ai_configs: List[AIConfig]
) -> Optional[str]:
    """Detecta el framework del proyecto usando IA"""
    context = get_project_context(project_path)

    logger.info(f"🔍 [detectar_framework_con_ia] Iniciando detección para: {project_path}")
    logger.info(f"🔍 [detectar_framework_con_ia] Configs disponibles: {len(ai_configs)}")
    for cfg in ai_configs:
        logger.info(f"🔍 [detectar_framework_con_ia] Config: {cfg.name} ({cfg.provider}) - {cfg.model} @ {cfg.api_url}")

    prompt = f"""Eres un experto en tecnologías de desarrollo. Analiza la siguiente información de un proyecto y detecta el framework/librería principal.

Información del proyecto:
- Framework detectado por archivos: {context['framework']}
- Dependencias principales: {', '.join(context['dependencies'][:20]) if context['dependencies'] else 'Ninguna'}
- Estructura de carpetas: {', '.join(context['folder_structure'][:15]) if context['folder_structure'] else 'N/A'}
- Archivos clave: {', '.join(context['key_files'][:10]) if context['key_files'] else 'Ninguno'}

Responde EXACTAMENTE con el nombre del framework en una sola palabra o frase corta.
Opciones comunes: Next.js, NestJS, React, Vue, Angular, Django, Flask, FastAPI, Laravel, Spring Boot, Gin, Express, Svelte, Remix, Nuxt.js, etc.

Si no estás seguro, responde "unknown".

Respuesta (solo el nombre del framework):"""

    logger.info(f"🔍 [detectar_framework_con_ia] Enviando prompt a IA...")
    try:
        response_text = await consultar_ia_con_fallback(prompt, ai_configs)
    except Exception as e:
        logger.error(f"🔍 [detectar_framework_con_ia] ERROR en consultar_ia_con_fallback: {e}")
        import traceback
        logger.error(f"🔍 [detectar_framework_con_ia] Traceback: {traceback.format_exc()}")
        return None

    logger.info(f"🔍 [detectar_framework_con_ia] Respuesta recibida: {response_text[:200] if response_text else 'None'}")

    if not response_text:
        logger.warning("🔍 [detectar_framework_con_ia] No se recibió respuesta de la IA")
        return None

    # Limpiar la respuesta (quitar espacios, saltos de línea, etc.)
    framework = response_text.strip().strip('"\'').split('\n')[0].strip()

    logger.info(f"🔍 [detectar_framework_con_ia] Framework detectado: {framework}")

    if framework and framework.lower() != 'unknown':
        return framework

    return None

async def sugerir_arquitectura_inicial(
    context: Dict[str, Any],
    ai_configs: List[AIConfig]
) -> Optional[AISuggestionResponse]:
    """Sugiere arquitectura inicial basada en análisis del proyecto"""
    prompt = f"""Eres un Arquitecto de Software Senior. Analiza este proyecto {context['framework']} con:
- Dependencias: {context['dependencies'][:15] if context['dependencies'] else 'N/A'}
- Estructura: {context['folder_structure'][:15] if context['folder_structure'] else 'N/A'}
- Archivos clave: {context['key_files'][:15] if context['key_files'] else 'N/A'}

TAREA:
Identifica el patrón arquitectónico (Hexagonal, Clean, MVC, etc.) y sugiere reglas de importaciones prohibidas basándote en las mejores prácticas.

RESPONDE EXCLUSIVAMENTE EN FORMATO JSON:
{{
  "pattern": "Nombre del patrón",
  "suggested_max_lines": 60,
  "rules": [
    {{ "from": "patrón_origen", "to": "patrón_prohibido", "reason": "explicación corta" }}
  ]
}}"""

    response_text = await consultar_ia_con_fallback(prompt, ai_configs)
    if not response_text:
        return None

    json_str = extraer_json_flexible(response_text)
    if not json_str:
        return None

    try:
        data = json.loads(json_str)
        rules = [
            SuggestedRule(r["from"], r["to"], r["reason"])
            for r in data.get("rules", [])
        ]
        return AISuggestionResponse(
            pattern=data.get("pattern", "Custom"),
            suggested_max_lines=data.get("suggested_max_lines", 60),
            rules=rules
        )
    except:
        return None
