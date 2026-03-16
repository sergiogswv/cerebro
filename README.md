# 🧠 CEREBRO

> **The Central Nervous System of the SKRYMIR Suite**

CEREBRO is a high-performance orchestrator and event distributor designed to synchronize the SKRYMIR intelligence agents. Built with **FastAPI** and **Socket.io**, it acts as the bridge between background agents (Sentinel, Architect, Warden) and the user interface (Skrymir Hub).

---

## 🚀 Role in the Ecosystem

Cerebro manages three critical communication flows:
1. **Agent-to-Backend:** Receives structured events (audits, changes, security alerts) via REST API.
2. **Backend-to-Agent:** Dispatches commands (start monitoring, run lint, answer prompt) to agents.
3. **Backend-to-UI:** Broadcasts real-time updates to the Dashboard using WebSockets for a lag-free experience.

---

## 🛠 Features

- **Event Routing:** Intelligent distribution of agent findings.
- **WebSocket Hub:** Real-time state synchronization.
- **Project Context:** Management of active workspaces and environment variables.
- **Service Awareness:** Integration with the Executor API to monitor agent health.

---

## ⚙️ Requirements

- Python 3.9+
- Pip (Python Package Manager)

---

## 📦 Installation & Setup

```bash
# 1. Clone the repository
git clone https://github.com/sergiogswv/cerebro.git
cd cerebro

# 2. Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Start the server
uvicorn app.main:app --host 0.0.0.0 --port 4000
```

---

## 📡 API Endpoints (Quick Ref)

- `POST /api/events`: Endpoints for agents to report findings.
- `POST /api/select-project`: Set the active work directory.
- `GET /api/projects`: List available projects in the workspace.
- `SOCKET /ws`: Real-time event stream.

---

## 📜 License

© 2026 Sergio - SKRYMIR Intelligence Command. All rights reserved.
