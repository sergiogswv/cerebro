"""
proactive_scheduler.py — Programador proactivo de análisis para Cerebro.

Modo Proactivo/Autómata: ejecuta análisis de código sin esperar cambios de archivos.
Modos disponibles:
  - debt_analysis:       analiza archivos con deuda técnica según umbral y lookback
  - hot_files:           analiza archivos modificados frecuentemente
  - new_implementation:  analiza archivos nuevos según patrones glob

Estados internos: idle → scanning → analyzing → idle (o paused en cualquier momento)
"""

import asyncio
import logging
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List

import httpx

logger = logging.getLogger("cerebro.proactive_scheduler")


# ─────────────────────────────────────────────────────────────────────────────
# Configuración por defecto (puede sobreescribirse desde la DB / API)
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_PROACTIVE_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "master_switch": True,
    "modes": {
        "debt_analysis": {
            "enabled": True,
            "interval_minutes": 120,      # Cada 2 horas (simplificado desde cron)
            "max_files_per_run": 10,
            "min_debt_score": 50,
            "lookback_days": 30,
        },
        "hot_files": {
            "enabled": True,
            "interval_minutes": 15,
            "max_files_per_run": 5,
            "threshold_changes": 3,
        },
        "new_implementation": {
            "enabled": False,
            "interval_minutes": 360,     # Cada 6 horas
            "scan_patterns": ["src/**/*.ts", "lib/**/*.py"],
        },
    },
    "autofix": {
        "enabled": True,
        "threshold": 0.8,
        "safe_issue_types": ["dead_code", "unused_import", "formatting"],
        "night_mode": {
            "enabled": True,
            "start_hour": 22,
            "end_hour": 6,
            "auto_merge_if_validated": False,
        },
        "validation": {
            "run_tests": True,
            "require_build": True,
        },
    },
    "notifications": {
        "telegram": True,
        "dashboard": True,
        "summary_after_batch": True,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Estado interno del scheduler
# ─────────────────────────────────────────────────────────────────────────────

class SchedulerState:
    IDLE = "idle"
    SCANNING = "scanning"
    ANALYZING = "analyzing"
    PAUSED = "paused"


class ProactiveScheduler:
    """
    Scheduler proactivo que lanza análisis automáticos según intervalos configurados.
    Integra con el orchestrator de Cerebro para enviar comandos a los agentes.
    """

    def __init__(self, db_path: str, cerebro_internal_url: str = "http://localhost:4000"):
        self.db_path = db_path
        self.cerebro_url = cerebro_internal_url
        self.state = SchedulerState.IDLE
        self.config: Dict[str, Any] = {}
        self._tasks: List[asyncio.Task] = []
        self._running = False
        self._current_batch_id: Optional[str] = None
        self._init_db()
        logger.info("📅 ProactiveScheduler inicializado")

    # ─────────────────────────────────────────────────────────────────────────
    # Base de datos
    # ─────────────────────────────────────────────────────────────────────────

    def _init_db(self):
        """Crea las tablas necesarias si no existen."""
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS proactive_analysis_state (
                    id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    last_analyzed_at TIMESTAMP,
                    debt_score INTEGER,
                    times_autofixed INTEGER DEFAULT 0,
                    last_autofix_result TEXT,
                    status TEXT,
                    batch_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_proactive_project ON proactive_analysis_state(project);
                CREATE INDEX IF NOT EXISTS idx_proactive_mode ON proactive_analysis_state(mode);
                CREATE INDEX IF NOT EXISTS idx_proactive_last_analyzed ON proactive_analysis_state(last_analyzed_at);
                CREATE INDEX IF NOT EXISTS idx_proactive_batch ON proactive_analysis_state(batch_id);

                CREATE TABLE IF NOT EXISTS proactive_config (
                    project TEXT PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    updated_at TIMESTAMP
                );
            """)
        logger.info("✅ Tablas proactivo inicializadas")

    def get_config(self, project: str) -> Dict[str, Any]:
        """Obtiene la config del proyecto desde DB, o la default."""
        import json
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT config_json FROM proactive_config WHERE project = ?",
                (project,)
            ).fetchone()
        if row:
            return json.loads(row[0])
        return DEFAULT_PROACTIVE_CONFIG.copy()

    def save_config(self, project: str, config: Dict[str, Any]):
        """Persiste la configuración en la DB."""
        import json
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO proactive_config (project, config_json, updated_at)
                   VALUES (?, ?, ?)""",
                (project, json.dumps(config), now)
            )
        logger.info(f"💾 Config proactivo guardada para proyecto '{project}'")

    def _mark_file_analyzed(self, project: str, mode: str, file_path: str, batch_id: str):
        """Registra que un archivo fue analizado en este batch."""
        now = datetime.now(timezone.utc).isoformat()
        record_id = str(uuid.uuid4())
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO proactive_analysis_state
                   (id, project, mode, file_path, last_analyzed_at, status, batch_id)
                   VALUES (?, ?, ?, ?, ?, 'analyzed', ?)""",
                (record_id, project, mode, file_path, now, batch_id)
            )

    def _was_recently_analyzed(self, project: str, file_path: str, hours: int = 2) -> bool:
        """Evita re-analizar archivos recientes."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """SELECT last_analyzed_at FROM proactive_analysis_state
                   WHERE project = ? AND file_path = ? AND last_analyzed_at > ?
                   ORDER BY last_analyzed_at DESC LIMIT 1""",
                (project, file_path, cutoff)
            ).fetchone()
        return row is not None

    # ─────────────────────────────────────────────────────────────────────────
    # Modo nocturno
    # ─────────────────────────────────────────────────────────────────────────

    def is_night_mode_active(self, config: Optional[Dict] = None) -> bool:
        """True si la hora actual está dentro de la ventana nocturna configurada."""
        cfg = config or self.config
        night_cfg = cfg.get("autofix", {}).get("night_mode", {})
        if not night_cfg.get("enabled", False):
            return False

        now_hour = datetime.now().hour
        start = night_cfg.get("start_hour", 22)
        end = night_cfg.get("end_hour", 6)

        if start > end:  # Cruza medianoche (ej: 22 → 6)
            return now_hour >= start or now_hour < end
        return start <= now_hour < end

    # ─────────────────────────────────────────────────────────────────────────
    # Arranque / pausa / reanudación
    # ─────────────────────────────────────────────────────────────────────────

    async def start(self, project: str):
        """Arranca todos los loops de programación para el proyecto dado."""
        if self._running:
            logger.warning("⏭️  ProactiveScheduler ya está corriendo")
            return

        self.config = self.get_config(project)
        self._project = project
        self._running = True
        self.state = SchedulerState.IDLE
        logger.info(f"🚀 ProactiveScheduler iniciado para '{project}'")

        modes_cfg = self.config.get("modes", {})

        if modes_cfg.get("hot_files", {}).get("enabled"):
            interval = modes_cfg["hot_files"].get("interval_minutes", 15) * 60
            t = asyncio.create_task(self._loop("hot_files", interval))
            self._tasks.append(t)

        if modes_cfg.get("debt_analysis", {}).get("enabled"):
            interval = modes_cfg["debt_analysis"].get("interval_minutes", 120) * 60
            t = asyncio.create_task(self._loop("debt_analysis", interval))
            self._tasks.append(t)

        if modes_cfg.get("new_implementation", {}).get("enabled"):
            interval = modes_cfg["new_implementation"].get("interval_minutes", 360) * 60
            t = asyncio.create_task(self._loop("new_implementation", interval))
            self._tasks.append(t)

    async def stop(self):
        """Detiene todos los loops."""
        self._running = False
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        self.state = SchedulerState.IDLE
        logger.info("🛑 ProactiveScheduler detenido")

    def pause(self):
        """Pausa temporalmente el scheduler (los loops siguen pero no lanzan análisis)."""
        self.state = SchedulerState.PAUSED
        logger.info("⏸️  ProactiveScheduler pausado")

    def resume(self):
        """Reanuda el scheduler después de una pausa."""
        if self.state == SchedulerState.PAUSED:
            self.state = SchedulerState.IDLE
            logger.info("▶️  ProactiveScheduler reanudado")

    # ─────────────────────────────────────────────────────────────────────────
    # Loops internos
    # ─────────────────────────────────────────────────────────────────────────

    async def _loop(self, mode: str, interval_seconds: float):
        """Loop de un modo específico."""
        logger.info(f"⏱️  Loop '{mode}' iniciado (intervalo={interval_seconds}s)")
        # Primera ejecución: esperar un poco antes de lanzar
        await asyncio.sleep(10)
        while self._running:
            if self.state != SchedulerState.PAUSED:
                try:
                    await self._run_mode(mode)
                except Exception as exc:
                    logger.error(f"❌ Error en loop {mode}: {exc}")
            await asyncio.sleep(interval_seconds)

    async def _run_mode(self, mode: str):
        """Ejecuta un ciclo de análisis para el modo dado."""
        if not self.config.get("master_switch", True):
            return

        batch_id = str(uuid.uuid4())[:8]
        self._current_batch_id = batch_id
        files = await self._discover_files(mode)

        if not files:
            logger.info(f"📭 '{mode}': no hay archivos para analizar")
            return

        self.state = SchedulerState.ANALYZING
        logger.info(f"🔍 '{mode}': analizando {len(files)} archivos (batch={batch_id})")
        await self._emit_event("proactive_analysis_started", {
            "mode": mode,
            "files_count": len(files),
            "batch_id": batch_id,
        })

        for file_path in files:
            if self.state == SchedulerState.PAUSED:
                break
            try:
                await self._trigger_agent_analysis(file_path, mode, batch_id)
                self._mark_file_analyzed(self._project, mode, file_path, batch_id)
                await asyncio.sleep(2)  # Throttle entre archivos
            except Exception as exc:
                logger.error(f"❌ Error analizando {file_path}: {exc}")

        self.state = SchedulerState.IDLE
        await self._emit_event("proactive_batch_completed", {
            "mode": mode,
            "files_analyzed": len(files),
            "batch_id": batch_id,
            "night_mode": self.is_night_mode_active(),
        })
        logger.info(f"✅ Batch '{mode}' completado (batch_id={batch_id})")

    # ─────────────────────────────────────────────────────────────────────────
    # Descubrimiento de archivos
    # ─────────────────────────────────────────────────────────────────────────

    async def _discover_files(self, mode: str) -> List[str]:
        """Descubre archivos a analizar según el modo."""
        from app.config import get_settings
        settings = get_settings()
        project_path = Path(settings.workspace_root) / self._project
        modes_cfg = self.config.get("modes", {}).get(mode, {})

        files: List[str] = []
        max_files = modes_cfg.get("max_files_per_run", 5)

        self.state = SchedulerState.SCANNING

        if mode == "hot_files":
            files = await self._find_hot_files(project_path, modes_cfg)
        elif mode == "debt_analysis":
            files = await self._find_debt_files(project_path, modes_cfg)
        elif mode == "new_implementation":
            files = await self._find_new_files(project_path, modes_cfg)

        # Filtrar archivos recientemente analizados
        cutoff_hours = 2 if mode == "hot_files" else 4
        files = [f for f in files if not self._was_recently_analyzed(
            self._project, f, hours=cutoff_hours
        )]

        return files[:max_files]

    async def _find_hot_files(self, project_path: Path, cfg: Dict) -> List[str]:
        """Devuelve archivos más modificados del proyecto vía git log."""
        threshold = cfg.get("threshold_changes", 3)
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(project_path),
                "log", "--name-only", "--pretty=format:", "-n", "100",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            raw = stdout.decode(errors="replace").strip()
            count: Dict[str, int] = {}
            for line in raw.splitlines():
                line = line.strip()
                if line:
                    count[line] = count.get(line, 0) + 1
            return [
                str(project_path / f) for f, c in sorted(count.items(), key=lambda x: -x[1])
                if c >= threshold and (project_path / f).is_file()
            ]
        except Exception as exc:
            logger.warning(f"⚠️  git log falló: {exc}")
            return []

    async def _find_debt_files(self, project_path: Path, cfg: Dict) -> List[str]:
        """Devuelve archivos con deuda técnica (busca en proactive_analysis_state)."""
        min_score = cfg.get("min_debt_score", 50)
        lookback = cfg.get("lookback_days", 30)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """SELECT file_path FROM proactive_analysis_state
                   WHERE project = ? AND debt_score >= ? AND created_at >= ?
                   ORDER BY debt_score DESC LIMIT 20""",
                (self._project, min_score, cutoff)
            ).fetchall()
        return [r[0] for r in rows if Path(r[0]).is_file()]

    async def _find_new_files(self, project_path: Path, cfg: Dict) -> List[str]:
        """Devuelve archivos nuevos que coincidan con los patrones glob configurados."""
        import glob as glob_mod
        patterns = cfg.get("scan_patterns", ["src/**/*.ts", "lib/**/*.py"])
        files = []
        for pat in patterns:
            matches = glob_mod.glob(str(project_path / pat), recursive=True)
            files.extend(matches)
        return files

    # ─────────────────────────────────────────────────────────────────────────
    # Trigger de análisis
    # ─────────────────────────────────────────────────────────────────────────

    async def _trigger_agent_analysis(self, file_path: str, mode: str, batch_id: str):
        """Manda un comando de análisis a Sentinel vía Cerebro."""
        logger.info(f"🎯 Triggering análisis proactivo: {file_path} (modo={mode})")
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                await client.post(
                    f"{self.cerebro_url}/command/sentinel_adk",
                    json={
                        "action": "check",
                        "target": file_path,
                        "options": {
                            "proactive": True,
                            "mode": mode,
                            "batch_id": batch_id,
                        },
                        "request_id": f"proactive-{batch_id}-{uuid.uuid4().hex[:6]}"
                    }
                )
        except Exception as exc:
            logger.warning(f"⚠️  No se pudo triggear análisis de {file_path}: {exc}")

    # ─────────────────────────────────────────────────────────────────────────
    # Trigger manual
    # ─────────────────────────────────────────────────────────────────────────

    async def trigger_now(self, mode: str = "hot_files"):
        """Dispara un análisis inmediato sin esperar al intervalo."""
        if not self._running:
            logger.warning("⚠️  Scheduler no está corriendo, no se puede triggear")
            return
        logger.info(f"🔔 Trigger manual: modo='{mode}'")
        asyncio.create_task(self._run_mode(mode))

    # ─────────────────────────────────────────────────────────────────────────
    # Notificaciones vía WebSocket de Cerebro
    # ─────────────────────────────────────────────────────────────────────────

    async def _emit_event(self, event_type: str, payload: Dict[str, Any]):
        """Emite evento al bus interno de Cerebro (WebSocket + decisión engine)."""
        try:
            from app.sockets import emit_agent_event
            await emit_agent_event({
                "source": "proactive_scheduler",
                "type": event_type,
                "severity": "info",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payload": payload,
            })
        except Exception as exc:
            logger.debug(f"No se pudo emitir evento {event_type}: {exc}")

    # ─────────────────────────────────────────────────────────────────────────
    # Estado para dashboard
    # ─────────────────────────────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "running": self._running,
            "night_mode_active": self.is_night_mode_active(),
            "current_batch_id": self._current_batch_id,
            "project": getattr(self, "_project", None),
        }


# Singleton global
_scheduler: Optional[ProactiveScheduler] = None


def get_proactive_scheduler(db_path: str = None) -> ProactiveScheduler:
    global _scheduler
    if _scheduler is None:
        if db_path is None:
            from app.config import get_settings
            s = get_settings()
            db_path = getattr(s, "context_db_path", "cerebro_context.db")
        _scheduler = ProactiveScheduler(db_path=db_path)
    return _scheduler
