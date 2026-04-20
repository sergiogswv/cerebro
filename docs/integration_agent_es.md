# Guía de Integración de Agentes

Este documento define el estándar para integrar un nuevo agente dentro de la Skrymir Suite, específicamente cómo debe comunicarse con **Cerebro** (el orquestador central).

## Resumen de la Arquitectura

Cerebro actúa como el kernel del sistema. Los agentes son servicios independientes que se comunican con Cerebro a través de HTTP/JSON.

- **Salida (Cerebro → Agente)**: Cerebro envía instrucciones llamadas **Comandos**.
- **Entrada (Agente → Cerebro)**: Los agentes reportan hallazgos o cambios de estado llamados **Eventos**.

---

## 1. Salida: Implementar el Endpoint de Comandos

Cada agente **debe** exponer un endpoint HTTP POST en `/command`. Así es como Cerebro dispara acciones en tu agente.

### El Payload de la Petición (`OrchestratorCommand`)
Cerebro enviará un JSON con la siguiente estructura:

```json
{
  "action": "string",
  "target": "string",
  "options": {
    "key": "value"
  },
  "request_id": "uuid-string"
}
```

- **action**: El nombre de la operación (ej. `scan`, `analyze`, `fix`).
- **target**: El nombre del proyecto o ruta del archivo sobre el que actuar.
- **options**: Un diccionario con parámetros específicos para la acción.
- **request_id**: Un ID único utilizado para el seguimiento de la operación.

### La Respuesta (`CommandAck`)
El agente debe responder inmediatamente con una confirmación:

```json
{
  "request_id": "uuid-string",
  "status": "accepted | rejected | completed",
  "result": {},
  "error": "string (opcional)"
}
```

---

## 2. Entrada: Enviar Eventos a Cerebro

Para reportar resultados, logs o cambios de estado, el agente debe enviar una petición POST al endpoint central de eventos de Cerebro: `http://localhost:4000/api/events`.

### El Payload del Evento (`AgentEvent`)
```json
{
  "source": "nombre_de_tu_agente",
  "type": "nombre_tipo_evento",
  "severity": "info | warning | error | critical",
  "timestamp": "ISO-8601 string",
  "payload": {
    "datos_aqui": "valor"
  }
}
```

- **source**: Identifica a tu agente (ej. `sentinel`, `warden`).
- **type**: Identificador específico del evento (ej. `vulnerability_found`, `analysis_completed`).
- **payload**: Los datos reales. Si es un hallazgo de código, usa campos como `file`, `line`, `finding`, y `recommendation` para una mejor integración con el Dashboard.

---

## 3. Pasos para la Integración

### Paso 1: Implementar la API
Desarrolla tu agente (Python, Rust, Node, etc.) asegurándote de que tenga el endpoint `/command` y pueda realizar peticiones HTTP a Cerebro.

### Paso 2: Registrar en el Dispatcher de Cerebro
Añade el nombre y la URL de tu agente en `cerebro/app/dispatcher.py` y las variables de entorno correspondientes en `cerebro/app/config.py`.

### Paso 3: Registro en el Dashboard (Opcional)
Para ver los eventos de tu agente en el **Comms Timeline**, añade tu identificador de fuente y un icono en `dashboard/src/components/events/CommsTimeline.jsx` dentro del objeto `SOURCE_META`.

---

## Mejores Prácticas
1. **Idempotencia**: Asegúrate de que ejecutar el mismo comando dos veces no cause efectos secundarios indeseados si es posible.
2. **Ejecución Asíncrona**: Si una acción es lenta, responde inmediatamente con `status: "accepted"` y reporta el resultado final a través de un **Evento** más tarde.
3. **Contexto Estructurado**: Incluye siempre el `target` (proyecto) y el archivo (`file`) en el payload cuando sea relevante para que Cerebro mantenga el historial específico por proyecto.
