import logging
import logging.config
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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


# ─── App ──────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.orchestrator import orchestrator
    import asyncio
    
    logger.info("🧠 Cerebro arriba — modo: %s | puerto: %s", settings.orchestrator_mode, settings.port)
    
    # Lanzar el bootstrap en segundo plano para no bloquear el inicio del servidor
    # Esperamos unos segundos para que el Notificador (si se levanta en conjunto) esté listo
    async def delayed_bootstrap():
        await asyncio.sleep(5)
        await orchestrator.bootstrap()
        
    asyncio.create_task(delayed_bootstrap())
    
    yield
    logger.info("🧠 Cerebro apagado")


app = FastAPI(
    title="Cerebro — Orquestador",
    description="Orquestador central del sistema multi-agente de desarrollo.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

import socketio
from app.sockets import sio



@app.get("/", tags=["root"])
async def root():
    return {
        "service": "cerebro",
        "version": "0.1.0",
        "mode": settings.orchestrator_mode,
        "docs": "/docs",
    }

app = socketio.ASGIApp(sio, other_asgi_app=app, socketio_path='/ws/socket.io')
