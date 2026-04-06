import logging
import logging.config
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.config import get_settings
from app.routes import router

settings = get_settings()

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cerebro")


# ─── App ───────────────────────────────────────────────────────��──────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.orchestrator import orchestrator
    import asyncio

    logger.info("🧠 Cerebro arriba — modo: %s | puerto: %s", settings.orchestrator_mode, settings.port)

    async def delayed_bootstrap():
        await asyncio.sleep(5)
        await orchestrator.bootstrap()

    async def telemetry_task():
        import psutil
        from app.sockets import sio
        import time
        start_time = time.time()
        while True:
            try:
                cpu = psutil.cpu_percent(interval=None)
                ram = psutil.virtual_memory().percent
                uptime_seconds = int(time.time() - start_time)
                m, s = divmod(uptime_seconds, 60)
                h, m = divmod(m, 60)
                uptime_str = f"{h:02d}:{m:02d}:{s:02d}"
                await sio.emit('system_stats', {
                    "cpu": f"{cpu}%",
                    "ram": f"{ram}%",
                    "uptime": uptime_str
                })
            except Exception as e:
                logger.error(f"Error en telemetría: {e}")
            await asyncio.sleep(3)

    asyncio.create_task(delayed_bootstrap())
    asyncio.create_task(telemetry_task())

    # ── Arrancar ProactiveScheduler ───────────────────────────────────────────
    async def delayed_proactive_start():
        await asyncio.sleep(15)  # Esperar a que los agentes estén listos
        try:
            from app.proactive_scheduler import get_proactive_scheduler
            from app.orchestrator import orchestrator
            project = orchestrator.active_project
            if project:
                scheduler = get_proactive_scheduler()
                await scheduler.start(project)
                logger.info(f"📅 ProactiveScheduler iniciado para '{project}'")
            else:
                logger.info("📅 ProactiveScheduler en espera (sin proyecto activo)")
        except Exception as exc:
            logger.warning(f"⚠️  ProactiveScheduler no pudo iniciar: {exc}")

    asyncio.create_task(delayed_proactive_start())

    yield
    # ── Apagar scheduler al cerrar ────────────────────────────────────────────
    try:
        from app.proactive_scheduler import get_proactive_scheduler
        await get_proactive_scheduler().stop()
    except Exception:
        pass
    logger.info("🧠 Cerebro apagado")


# Import socketio
import socketio
from app.sockets import sio

# Crear FastAPI app
fastapi_app = FastAPI(
    title="Cerebro — Orquestador",
    description="Orquestador central del sistema multi-agente de desarrollo.",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS para FastAPI
fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

fastapi_app.include_router(router)

@fastapi_app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception(f"❌ Unhandled exception in {request.method} {request.url.path}: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error: {str(exc)}"}
    )

@fastapi_app.get("/", tags=["root"])
async def root():
    return {
        "service": "cerebro",
        "version": "0.1.0",
        "mode": settings.orchestrator_mode,
        "docs": "/docs",
    }


# ASGI Application que combina Socket.IO y FastAPI con CORS
class CombinedASGIApp:
    """ASGI app that routes between Socket.IO and FastAPI with CORS support."""

    def __init__(self, sio_server, fastapi_app):
        self.sio = sio_server
        self.fastapi = fastapi_app
        self.socketio_app = socketio.ASGIApp(sio_server, socketio_path='/ws/socket.io')

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            method = scope.get("method", "GET")

            # Handle CORS preflight
            if method == "OPTIONS":
                await self._handle_cors_preflight(scope, receive, send)
                return

            # Route to Socket.IO for /ws/socket.io paths
            if path.startswith("/ws/socket.io"):
                await self.socketio_app(scope, receive, send)
            else:
                # Route to FastAPI for everything else
                await self._handle_fastapi_with_cors(scope, receive, send)
        elif scope["type"] == "websocket":
            # WebSocket goes to Socket.IO
            await self.socketio_app(scope, receive, send)
        else:
            # Other types (lifespan, etc.)
            await self.fastapi(scope, receive, send)

    async def _handle_cors_preflight(self, scope, receive, send):
        """Handle CORS preflight request."""
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                [b"access-control-allow-origin", b"http://localhost:5173"],
                [b"access-control-allow-methods", b"GET, POST, PUT, DELETE, OPTIONS, PATCH"],
                [b"access-control-allow-headers", b"*"],
                [b"access-control-allow-credentials", b"true"],
                [b"content-length", b"0"],
            ],
        })
        await send({"type": "http.response.body", "body": b""})

    async def _handle_fastapi_with_cors(self, scope, receive, send):
        """Call FastAPI and add CORS headers to response."""
        origin = "http://localhost:5173"

        async def send_with_cors(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                # Check if CORS headers already exist
                has_cors = any(h[0].lower() == b"access-control-allow-origin" for h in headers)
                if not has_cors:
                    headers.append([b"access-control-allow-origin", origin.encode()])
                    headers.append([b"access-control-allow-credentials", b"true"])
                message["headers"] = headers
            await send(message)

        await self.fastapi(scope, receive, send_with_cors)


# Crear aplicación combinada
app = CombinedASGIApp(sio, fastapi_app)
