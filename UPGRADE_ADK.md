# 🧠 Plan de Upgrade: CEREBRO — Orquestador ADK

> **Objetivo:** Evolucionar el orquestador actual de un sistema basado en reglas deterministas (FastAPI) a un **Agente de IA Real** usando el **Google Agent Development Kit (ADK)**, dotándolo de razonamiento dinámico y memoria persistente.

---

## 🏗️ Nueva Arquitectura (ADK Core)

El nuevo Cerebro dejará de ser solo una API de paso para convertirse en el **Coordinador Multi-Agente**.

- **Framework:** `google-adk` (Python)
- **Modelo:** Gemini 2.0 Flash (vía Google AI Studio o Vertex AI)
- **Estructura:** `LlmAgent` con capacidades de delegación hacia `SubAgents`.

### 1. Sistema de Memoria Persistente (Long-term Memory)
Para que Cerebro sea un "agente real", debe recordar interacciones pasadas.
- **Memoria de Sesión (Short-term):** Gestión nativa de historial de ADK para mantener el contexto del chat actual.
- **Base de Conocimiento (Long-term):** 
    - Integración con **SQLite** (existente) para el historial estructurado.
    - **Vector DB (ChromaDB/Pinecone):** Para almacenar "lecciones aprendidas" y resúmenes de arquitectura del proyecto, permitiendo una recuperación por similitud semántica.

### 2. Conversión a Agente ADK
```python
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

# Cerebro se define como el Agente Principal
cerebro = LlmAgent(
    name="Skrymir-Cerebro",
    model="gemini-2.0-flash",
    instruction="""
        Eres el Orquestador Central de Skrymir Suite. 
        Tu misión es coordinar a Sentinel, Architect y Warden.
        Tienes acceso a memoria histórica del proyecto.
        Cuando un agente reporta un evento, analiza el contexto global 
        y decide si debes delegar una tarea a otro agente o notificar al usuario.
    """,
    tools=[...], # Wrappers de los servicios existentes
    memory=CustomProjectMemory() # Persistencia real
)
```

---

## 🛠️ Pasos de Implementación

### Fase 1: Instalación y Configuración (P0)
1. Instalar dependencias: `pip install google-adk chromadb`.
2. Configurar `GEMINI_API_KEY` en el entorno.
3. Crear el entrypoint `adk_main.py` en la carpeta `cerebro/`.

### Fase 2: Definición de Tools (P0)
Cerebro debe ver a los demás agentes como herramientas invocables.
- `Tool: InvokeSentinel(action, target)`
- `Tool: InvokeArchitect(action, target)`
- `Tool: InvokeWarden(action, target)`
- `Tool: SendTelegram(message, severity)`

### Fase 3: Integración de Memoria (P1)
1. Implementar `ProjectKnowledgeRetriever`: Una herramienta que busca en el historial de eventos anteriores.
2. Guardar automáticamente resúmenes de incidentes resueltos en la base de datos de contexto.

### Fase 4: Despliegue del Ciclo de Pensamiento (P1)
- Reemplazar el `DecisionEngine` basado en reglas por un loop de pensamiento del LLM que evalúe:
    - ¿Qué pasó? (Evento recibido)
    - ¿Qué sé de esto? (Consulta a memoria)
    - ¿Qué debo hacer? (Decisión del LLM)
    
### Fase 5: Motor de Refactorización Activa (Auto-Fix Nivel 5) (P2)
Skrymir pasará de Nivel 3 (Sugerencias) a Nivel 5 (Agente Autónomo de Ingeniería). Cerebro no editará código directamente para no violar el Principio de Responsabilidad Única, sino que usará a `Executor` como puente hacia LLMs CLI.
1. **Delegación a Executor (Las Manos):** Cuando Cerebro dictamine resolver un bug arquitectónico detectado por Warden/Architect, enviará un comando estandarizado a `Executor`.
2. **Provider-Agnostic CLI (Aider / Claude):** `Executor` abrirá un subproceso aislado usando `aider` (para modelos locales/Ollama) o `claude code`, pasándole el contexto de la falla y la instrucción de reparación.
3. **Aislamiento Git (Safety Mesh):** Cerebro ordenará estrictamente que el CLI aislee el fix en una rama divergente (ej. `skrymir-fix/warden-hotspot`) para generar automáticamente un *Pull Request* sin tocar `main`.
4. **Human-In-The-Loop:** Implementación de tiempos de espera en escalaciones críticas, cayendo a aislamientos de Git si el usuario no responde vía Telegram.

---

## ✅ Beneficios del Upgrade
- **Razonamiento Zero-shot:** Maneja situaciones no previstas sin reglas estáticas.
- **Contexto de Proyecto:** Al tener memoria persistente, Cerebro sabrá que un error recurrente en `auth.ts` ya fue analizado ayer por Architect.
- **Comunicación Natural:** Interacción vía Telegram mucho más fluida y contextual.
