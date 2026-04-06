"""
routes/ — Router modular de Cerebro.

Cada módulo agrupa endpoints por dominio.
Este __init__ los ensambla en un único APIRouter
que main.py registra en la app FastAPI.

Estructura:
  routes/core.py       → /events, /health, /status, /bootstrap, /projects, /select-project
  routes/architect.py  → /architect/*
  routes/sentinel.py   → /sentinel/*
  routes/warden.py     → /warden/*
  routes/learning.py   → /feedback, /learn, /learned-rules, /changes/*
  routes/proactive.py  → /proactive/* (Modo Proactivo/Autómata)
"""

from fastapi import APIRouter

from .core      import router as core_router
from .architect import router as architect_router
from .sentinel  import router as sentinel_router
from .warden    import router as warden_router
from .learning  import router as learning_router
from .config    import router as config_router
from .pipeline  import router as pipeline_router
from .proactive import router as proactive_router
from .interactive import router as interactive_router

# Router principal — registra todos los sub-routers bajo el mismo prefijo /api
router = APIRouter(prefix="/api", tags=["api"])
router.include_router(core_router)
router.include_router(architect_router, prefix="/architect")
router.include_router(sentinel_router)  # El prefijo /sentinel ya está en las rutas del router
router.include_router(warden_router, prefix="/warden")
router.include_router(learning_router, prefix="/cerebro")
router.include_router(config_router)
router.include_router(pipeline_router, prefix="/pipeline")
router.include_router(proactive_router)  # /api/proactive/*
router.include_router(interactive_router, prefix="/interactive")
