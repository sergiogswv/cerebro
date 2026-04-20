# Agent Integration Guide

This document defines the standard for integrating a new agent into the Skrymir Suite, specifically how it communicates with **Cerebro** (the central orchestrator).

## Architecture Overview

Cerebro acts as the kernel of the system. Agents are independent services that communicate with Cerebro via HTTP/JSON.

- **Outbound (Cerebro → Agent)**: Cerebro sends instructions called **Commands**.
- **Inbound (Agent → Cerebro)**: Agents report findings or state changes called **Events**.

---

## 1. Outbound: Implementing the Command Endpoint

Every agent **must** expose an HTTP POST endpoint at `/command`. This is how Cerebro triggers actions in your agent.

### The Request Payload (`OrchestratorCommand`)
Cerebro will send a JSON with the following structure:

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

- **action**: The name of the operation (e.g., `scan`, `analyze`, `fix`).
- **target**: The project name or file path to act upon.
- **options**: A dictionary with specific parameters for the action.
- **request_id**: A unique ID used for tracking the operation.

### The Response (`CommandAck`)
The agent must respond immediately with a confirmation:

```json
{
  "request_id": "uuid-string",
  "status": "accepted | rejected | completed",
  "result": {},
  "error": "string (optional)"
}
```

---

## 2. Inbound: Sending Events to Cerebro

To report results, logs, or status changes, the agent must send a POST request to Cerebro's central events endpoint: `http://localhost:4000/api/events`.

### The Event Payload (`AgentEvent`)
```json
{
  "source": "your_agent_name",
  "type": "event_type_name",
  "severity": "info | warning | error | critical",
  "timestamp": "ISO-8601 string",
  "payload": {
    "any_data": "here"
  }
}
```

- **source**: Identifies your agent (e.g., `sentinel`, `warden`).
- **type**: Specific event identifier (e.g., `vulnerability_found`, `analysis_completed`).
- **payload**: The actual data. If it's a code finding, use fields like `file`, `line`, `finding`, and `recommendation` for better Dashboard integration.

---

## 3. Integration Steps

### Step 1: Implement the API
Develop your agent (Python, Rust, Node, etc.) ensuring it has the `/command` endpoint and can make HTTP requests to Cerebro.

### Step 2: Register in Cerebro Dispatcher
Add your agent's naming and URL to `cerebro/app/dispatcher.py` and the corresponding environment variables in `cerebro/app/config.py`.

### Step 3: Register in Dashboard (Optional)
To see your agent's events in the **Comms Timeline**, add your source identifier and icon to `dashboard/src/components/events/CommsTimeline.jsx` in the `SOURCE_META` object.

---

## Best Practices
1. **Idempotency**: Ensure that running the same command twice doesn't cause side effects if possible.
2. **Asynchronous Execution**: If an action is slow, return `status: "accepted"` immediately and report the final result via an `Event` later.
3. **Structured Context**: Always include the `target` (project) and `file` in the payload whenever relevant so Cerebro can maintain project-specific history.
