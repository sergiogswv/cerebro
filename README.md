# 🧠 Cerebro — The Orchestration Engine

> **El Agente Orquestador y Núcleo de Aprendizaje de Skrymir Suite.**
>
> Desarrollado por: **Sergio Guadarrama** — [sergio.gs8925@gmail.com](mailto:sergio.gs8925@gmail.com)

---

## 📋 Descripción

Cerebro es el corazón de la suite. Es una API construida con **FastAPI** que actúa como el HUB central de comunicación entre todos los agentes (Sentinel, Architect, Warden y Executor).

### Funcionalidades Clave:
- **Decision Engine:** Evalúa eventos entrantes y decide si requieren acción (Autofix) o solo notificación.
- **Context DB:** Almacena el historial de decisiones y patrones de código en SQLite.
- **Human-in-the-Loop:** Gestiona las solicitudes de aprobación para cambios críticos.
- **Learning Loop:** Registra tu feedback para refinar las futuras decisiones de los agentes.

---
**Sergio Guadarrama** — [sergio.gs8925@gmail.com](mailto:sergio.gs8925@gmail.com)
Parte de la **Skrymir Suite**
