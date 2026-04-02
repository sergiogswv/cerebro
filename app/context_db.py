"""
Context DB — Cerebro
Base de datos de contexto para seguimiento de:
- Historial de archivos (eventos, modificaciones, problemas)
- Criticidad de archivos por patrón
- Patrones repetitivos de problemas

Usa SQLite para persistencia ligera.
"""

import logging
import sqlite3
import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from pathlib import Path

from app.vector_store import VectorStore
from app.hybrid_query_engine import HybridQueryEngine

logger = logging.getLogger("cerebro.context_db")


# Schema de la base de datos
DB_SCHEMA = """
-- Historial de eventos por archivo
CREATE TABLE IF NOT EXISTS file_events (
    id TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    severity TEXT NOT NULL,
    payload TEXT,
    timestamp TEXT NOT NULL,
    decision_actions TEXT,
    resolved BOOLEAN DEFAULT FALSE,
    resolution_notes TEXT
);

-- Configuración de criticidad por archivo/patrón
CREATE TABLE IF NOT EXISTS file_criticality (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL UNIQUE,
    criticality TEXT NOT NULL DEFAULT 'medium',
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Estadísticas agregadas por archivo
CREATE TABLE IF NOT EXISTS file_stats (
    file_path TEXT PRIMARY KEY,
    total_events INTEGER DEFAULT 0,
    critical_events INTEGER DEFAULT 0,
    error_events INTEGER DEFAULT 0,
    warning_events INTEGER DEFAULT 0,
    last_event_at TEXT,
    repeat_offender BOOLEAN DEFAULT FALSE,
    churn_score REAL DEFAULT 0.0,
    risk_score REAL DEFAULT 0.0,
    updated_at TEXT NOT NULL
);

-- Patrones detectados (ej: archivo con múltiples violaciones)
CREATE TABLE IF NOT EXISTS detected_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type TEXT NOT NULL,
    file_path TEXT,
    description TEXT,
    severity TEXT,
    occurrences INTEGER DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    metadata TEXT
);

-- Feedback del usuario sobre decisiones de Cerebro
CREATE TABLE IF NOT EXISTS user_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    decision_actions TEXT,
    feedback_type TEXT NOT NULL,  -- 'thumbs_up' o 'thumbs_down'
    reason TEXT,  -- Por qué el usuario dio este feedback
    suggested_action TEXT,  -- Qué debería haber hecho Cerebro
    timestamp TEXT NOT NULL,
    processed BOOLEAN DEFAULT FALSE
);

-- Resultados de decisiones (tracking automático)
CREATE TABLE IF NOT EXISTS decision_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    file_path TEXT,
    decision_actions TEXT,
    outcome_type TEXT,  -- 'correct', 'false_positive', 'false_negative'
    outcome_details TEXT,
    auto_detected BOOLEAN DEFAULT FALSE,  -- Si fue detectado automáticamente o por feedback
    timestamp TEXT NOT NULL
);

-- Reglas aprendidas (ajustadas por feedback)
CREATE TABLE IF NOT EXISTS learned_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    original_rule TEXT,
    adjusted_rule TEXT NOT NULL,
    reason TEXT,
    feedback_count INTEGER DEFAULT 1,
    success_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    active BOOLEAN DEFAULT TRUE
);

-- Índices para consultas rápidas
CREATE INDEX IF NOT EXISTS idx_file_events_path ON file_events(file_path);
CREATE INDEX IF NOT EXISTS idx_file_events_timestamp ON file_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_file_events_severity ON file_events(severity);
CREATE INDEX IF NOT EXISTS idx_detected_patterns_type ON detected_patterns(pattern_type);
CREATE INDEX IF NOT EXISTS idx_user_feedback_event ON user_feedback(event_id);
CREATE INDEX IF NOT EXISTS idx_decision_outcomes_event ON decision_outcomes(event_id);
"""


class ContextDB:
    """
    Base de datos de contexto para Cerebro.
    Almacena historial de archivos, criticidad y patrones.
    """

    def __init__(self, db_path: Optional[str] = None, vector_enabled: bool = True):
        """
        Inicializa la base de datos.

        Args:
            db_path: Ruta al archivo SQLite. Por defecto: ~/.cerebro/context.db
            vector_enabled: Si True, intenta inicializar VectorStore
        """
        if db_path is None:
            cerebro_dir = Path.home() / ".cerebro"
            cerebro_dir.mkdir(parents=True, exist_ok=True)
            db_path = str(cerebro_dir / "context.db")

        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

        # Configurar criticidad por defecto
        self._init_default_criticality()

        # Inicializar VectorStore (opcional)
        self._vector_store: Optional[VectorStore] = None
        self._hybrid_engine: Optional[HybridQueryEngine] = None

        if vector_enabled:
            try:
                self._vector_store = VectorStore()
                self._hybrid_engine = HybridQueryEngine(self, self._vector_store)
                logger.info("VectorStore integrado en ContextDB")
            except Exception as e:
                logger.warning(f"No se pudo inicializar VectorStore: {e}")

        logger.info(f"🗄️ ContextDB inicializada: {db_path}")

    def _get_connection(self) -> sqlite3.Connection:
        """Obtiene conexión a la base de datos"""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init_db(self):
        """Inicializa el schema de la base de datos"""
        conn = self._get_connection()
        conn.executescript(DB_SCHEMA)
        conn.commit()
        logger.debug("📦 Schema de ContextDB verificado")

    def _init_default_criticality(self):
        """Inicializa patrones de criticidad por defecto"""
        default_patterns = [
            ("**/auth*", "high", "Archivos de autenticación - críticos para seguridad"),
            ("**/security*", "high", "Archivos de seguridad"),
            ("**/*.env*", "critical", "Variables de entorno - pueden contener secretos"),
            ("**/config*", "medium", "Archivos de configuración"),
            ("**/database*", "high", "Acceso a base de datos"),
            ("**/migration*", "high", "Migraciones de base de datos"),
            ("**/controller*", "medium", "Controladores API"),
            ("**/service*", "medium", "Servicios de negocio"),
            ("**/model*", "medium", "Modelos de datos"),
            ("**/middleware*", "high", "Middlewares - afectan todo el flujo"),
            ("**/router*", "medium", "Ruteo de la aplicación"),
            ("**/utils*", "low", "Utilitarios generales"),
            ("**/test*", "low", "Archivos de test"),
            ("**/*.spec*", "low", "Especificaciones de test"),
        ]

        conn = self._get_connection()
        now = datetime.utcnow().isoformat()

        for pattern, criticality, description in default_patterns:
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO file_criticality (pattern, criticality, description, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (pattern, criticality, description, now, now)
                )
            except sqlite3.IntegrityError:
                pass  # Ya existe

        conn.commit()
        logger.debug("📦 Patrones de criticidad por defecto inicializados")

    # ─────────────────────────────────────────────────────────────────────────
    # MÉTODOS DE CONSULTA DE CONTEXTO
    # ─────────────────────────────────────────────────────────────────────────

    def get_file_context(self, file_path: str) -> Dict[str, Any]:
        """
        Obtiene todo el contexto disponible para un archivo.

        Args:
            file_path: Ruta del archivo

        Returns:
            Dict con criticality, history, stats, patterns
        """
        return {
            "criticality": self.get_file_criticality(file_path),
            "history": self.get_file_history(file_path),
            "stats": self.get_file_stats(file_path),
            "patterns": self.get_related_patterns(file_path),
        }

    def get_file_criticality(self, file_path: str) -> str:
        """
        Obtiene la criticidad de un archivo basada en patrones configurados.

        Args:
            file_path: Ruta del archivo

        Returns:
            str: 'low', 'medium', 'high', o 'critical'
        """
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT criticality FROM file_criticality WHERE ? LIKE pattern ORDER BY LENGTH(pattern) DESC LIMIT 1",
            (file_path,)
        )
        row = cursor.fetchone()

        if row:
            return row["criticality"]

        # Verificar patrones con glob manual
        import fnmatch
        cursor = conn.execute("SELECT pattern, criticality FROM file_criticality")
        for row in cursor:
            if fnmatch.fnmatch(file_path, row["pattern"]):
                return row["criticality"]

        return "medium"  # Default

    def get_file_history(self, file_path: str, limit: int = 10) -> Dict[str, Any]:
        """
        Obtiene el historial de eventos de un archivo.

        Args:
            file_path: Ruta del archivo
            limit: Máximo de eventos a retornar

        Returns:
            Dict con eventos y flags (repeat_offender, etc.)
        """
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT id, event_type, source, severity, payload, timestamp, decision_actions
            FROM file_events
            WHERE file_path = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (file_path, limit)
        )

        events = []
        for row in cursor:
            events.append({
                "id": row["id"],
                "event_type": row["event_type"],
                "source": row["source"],
                "severity": row["severity"],
                "payload": json.loads(row["payload"]) if row["payload"] else None,
                "timestamp": row["timestamp"],
                "decision_actions": json.loads(row["decision_actions"]) if row["decision_actions"] else None,
            })

        # Verificar si es repeat offender (múltiples eventos de error/critical)
        stats = self.get_file_stats(file_path)
        repeat_offender = stats.get("error_events", 0) + stats.get("critical_events", 0) >= 3

        return {
            "events": events,
            "repeat_offender": repeat_offender,
            "total_events": len(events),
        }

    def get_file_stats(self, file_path: str) -> Dict[str, Any]:
        """
        Obtiene estadísticas agregadas de un archivo.

        Args:
            file_path: Ruta del archivo

        Returns:
            Dict con estadísticas
        """
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT * FROM file_stats WHERE file_path = ?",
            (file_path,)
        )
        row = cursor.fetchone()

        if not row:
            return {
                "total_events": 0,
                "critical_events": 0,
                "error_events": 0,
                "warning_events": 0,
                "repeat_offender": False,
                "churn_score": 0.0,
                "risk_score": 0.0,
            }

        return {
            "total_events": row["total_events"],
            "critical_events": row["critical_events"],
            "error_events": row["error_events"],
            "warning_events": row["warning_events"],
            "last_event_at": row["last_event_at"],
            "repeat_offender": bool(row["repeat_offender"]),
            "churn_score": row["churn_score"],
            "risk_score": row["risk_score"],
        }

    def get_related_patterns(self, file_path: str) -> List[Dict[str, Any]]:
        """
        Obtiene patrones detectados relacionados con un archivo.

        Args:
            file_path: Ruta del archivo

        Returns:
            Lista de patrones detectados
        """
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT * FROM detected_patterns
            WHERE file_path = ? OR file_path IS NULL
            ORDER BY last_seen DESC
            """,
            (file_path,)
        )

        patterns = []
        for row in cursor:
            patterns.append({
                "id": row["id"],
                "pattern_type": row["pattern_type"],
                "file_path": row["file_path"],
                "description": row["description"],
                "severity": row["severity"],
                "occurrences": row["occurrences"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "metadata": json.loads(row["metadata"]) if row["metadata"] else None,
            })

        return patterns

    def get_recent_patterns(
        self,
        source_filter: Optional[str] = None,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Obtiene los patrones detectados más recientes.

        Args:
            source_filter: Filtrar por tipo de patrón (opcional)
            limit: Número máximo de patrones a retornar

        Returns:
            Lista de patrones detectados ordenados por last_seen DESC
        """
        conn = self._get_connection()

        if source_filter:
            cursor = conn.execute(
                """
                SELECT * FROM detected_patterns
                WHERE pattern_type = ?
                ORDER BY last_seen DESC
                LIMIT ?
                """,
                (source_filter, limit)
            )
        else:
            cursor = conn.execute(
                """
                SELECT * FROM detected_patterns
                ORDER BY last_seen DESC
                LIMIT ?
                """,
                (limit,)
            )

        patterns = []
        for row in cursor:
            patterns.append({
                "id": row["id"],
                "pattern_type": row["pattern_type"],
                "file_path": row["file_path"],
                "description": row["description"],
                "severity": row["severity"],
                "occurrences": row["occurrences"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "metadata": json.loads(row["metadata"]) if row["metadata"] else None,
            })

        return patterns

    # ─────────────────────────────────────────────────────────────────────────
    # MÉTODOS DE REGISTRO DE EVENTOS
    # ─────────────────────────────────────────────���───────────────────────────

    def record_event(
        self,
        file_path: str,
        event_type: str,
        source: str,
        severity: str,
        payload: Optional[Dict] = None,
        decision_actions: Optional[List[str]] = None,
    ) -> str:
        """
        Registra un evento en el historial de un archivo.

        Args:
            file_path: Ruta del archivo
            event_type: Tipo de evento (file_change, secret_detected, etc.)
            source: Agente que generó el evento
            severity: Severidad del evento
            payload: Datos adicionales del evento
            decision_actions: Acciones decididas por DecisionEngine

        Returns:
            str: ID del evento registrado
        """
        import uuid
        event_id = str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat()

        conn = self._get_connection()
        conn.execute(
            """
            INSERT INTO file_events (id, file_path, event_type, source, severity, payload, timestamp, decision_actions)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                file_path,
                event_type,
                source,
                severity,
                json.dumps(payload) if payload else None,
                timestamp,
                json.dumps(decision_actions) if decision_actions else None,
            )
        )

        # Actualizar estadísticas
        self._update_file_stats(conn, file_path, severity)

        conn.commit()
        logger.debug(f"📝 Evento registrado: {event_id} ({file_path}, {event_type}, {severity})")

        # Indexar en VectorStore (async, no bloqueante)
        if self._vector_store and self._vector_store.is_available():
            try:
                # Construir descripción para embedding
                description = self._build_event_description(
                    event_type, source, severity, file_path, payload
                )

                metadata = {
                    "source": source,
                    "severity": severity,
                    "file_path": file_path,
                    "timestamp": timestamp,
                    "project": payload.get("project") if payload else None,
                    "event_type": event_type
                }

                # Indexar (no bloqueante, si falla no afecta el evento)
                self._vector_store.add_event(event_id, description, metadata)
            except Exception as e:
                logger.warning(f"No se pudo indexar evento en VectorStore: {e}")

        return event_id

    def _update_file_stats(self, conn: sqlite3.Connection, file_path: str, severity: str):
        """Actualiza estadísticas de un archivo después de un evento"""
        timestamp = datetime.utcnow().isoformat()

        # Obtener stats actuales
        cursor = conn.execute(
            "SELECT * FROM file_stats WHERE file_path = ?",
            (file_path,)
        )
        row = cursor.fetchone()

        if row:
            # Actualizar existentes
            critical_events = row["critical_events"] + (1 if severity == "critical" else 0)
            error_events = row["error_events"] + (1 if severity == "error" else 0)
            warning_events = row["warning_events"] + (1 if severity == "warning" else 0)
            total_events = row["total_events"] + 1

            # Calcular churn score (eventos recientes)
            seven_days_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()
            cursor = conn.execute(
                "SELECT COUNT(*) as cnt FROM file_events WHERE file_path = ? AND timestamp > ?",
                (file_path, seven_days_ago)
            )
            recent_events = cursor.fetchone()["cnt"]
            churn_score = min(1.0, recent_events / 10.0)  # Normalizado 0-1

            # Calcular risk score
            risk_score = (
                (critical_events * 0.4) +
                (error_events * 0.3) +
                (warning_events * 0.2) +
                (churn_score * 0.1)
            )

            repeat_offender = (critical_events + error_events) >= 3

            conn.execute(
                """
                UPDATE file_stats SET
                    total_events = ?,
                    critical_events = ?,
                    error_events = ?,
                    warning_events = ?,
                    last_event_at = ?,
                    repeat_offender = ?,
                    churn_score = ?,
                    risk_score = ?,
                    updated_at = ?
                WHERE file_path = ?
                """,
                (
                    total_events,
                    critical_events,
                    error_events,
                    warning_events,
                    timestamp,
                    repeat_offender,
                    churn_score,
                    risk_score,
                    timestamp,
                    file_path,
                )
            )
        else:
            # Crear nuevas stats
            critical_events = 1 if severity == "critical" else 0
            error_events = 1 if severity == "error" else 0
            warning_events = 1 if severity == "warning" else 0

            conn.execute(
                """
                INSERT INTO file_stats (file_path, total_events, critical_events, error_events, warning_events, last_event_at, repeat_offender, churn_score, risk_score, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_path,
                    1,
                    critical_events,
                    error_events,
                    warning_events,
                    timestamp,
                    False,
                    0.1,
                    (critical_events * 0.4) + (error_events * 0.3) + (warning_events * 0.2),
                    timestamp,
                )
            )

    def record_pattern(
        self,
        pattern_type: str,
        description: str,
        severity: str,
        file_path: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ):
        """
        Registra un patrón detectado (ej: múltiples violaciones en un archivo).

        Args:
            pattern_type: Tipo de patrón (repeat_offender, high_churn, etc.)
            description: Descripción del patrón
            severity: Severidad asociada
            file_path: Archivo relacionado (opcional)
            metadata: Datos adicionales
        """
        timestamp = datetime.utcnow().isoformat()

        conn = self._get_connection()

        # Verificar si ya existe patrón similar
        cursor = conn.execute(
            "SELECT * FROM detected_patterns WHERE pattern_type = ? AND file_path = ?",
            (pattern_type, file_path)
        )
        row = cursor.fetchone()

        if row:
            # Actualizar existente
            conn.execute(
                """
                UPDATE detected_patterns SET
                    occurrences = occurrences + 1,
                    last_seen = ?,
                    metadata = ?
                WHERE id = ?
                """,
                (timestamp, json.dumps(metadata) if metadata else None, row["id"])
            )
        else:
            # Crear nuevo
            conn.execute(
                """
                INSERT INTO detected_patterns (pattern_type, file_path, description, severity, first_seen, last_seen, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (pattern_type, file_path, description, severity, timestamp, timestamp, json.dumps(metadata) if metadata else None)
            )

        conn.commit()
        logger.debug(f"📊 Patrón registrado: {pattern_type} ({file_path or 'global'})")

    # ─────────────────────────────────────────────────────────────────────────
    # MÉTODOS DE CONFIGURACIÓN
    # ─────────────────────────────────────────────────────────────────────────

    def set_file_criticality(self, pattern: str, criticality: str, description: Optional[str] = None):
        """
        Configura la criticidad de un patrón de archivo.

        Args:
            pattern: Patrón glob (ej: "**/auth*")
            criticality: 'low', 'medium', 'high', 'critical'
            description: Descripción opcional
        """
        conn = self._get_connection()
        now = datetime.utcnow().isoformat()

        conn.execute(
            """
            INSERT OR REPLACE INTO file_criticality (pattern, criticality, description, created_at, updated_at)
            VALUES (?, ?, ?, COALESCE((SELECT created_at FROM file_criticality WHERE pattern = ?), ?), ?)
            """,
            (pattern, criticality, description, pattern, now, now)
        )

        conn.commit()
        logger.info(f"⚙️ Criticidad configurada: {pattern} → {criticality}")

    def get_criticality_config(self) -> List[Dict[str, Any]]:
        """
        Obtiene toda la configuración de criticidad.

        Returns:
            Lista de patrones con su criticidad
        """
        conn = self._get_connection()
        cursor = conn.execute("SELECT * FROM file_criticality ORDER BY criticality DESC, pattern")

        configs = []
        for row in cursor:
            configs.append({
                "pattern": row["pattern"],
                "criticality": row["criticality"],
                "description": row["description"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            })

        return configs

    # ──────────────────────────��──────────────────────────────────────────────
    # MÉTODOS DE FEEDBACK Y APRENDIZAJE
    # ─────────────────────────────────────────────────────────────────────────

    def record_feedback(
        self,
        event_id: str,
        feedback_type: str,
        decision_actions: Optional[List[str]] = None,
        reason: Optional[str] = None,
        suggested_action: Optional[str] = None,
    ) -> int:
        """
        Registra feedback del usuario sobre una decisión de Cerebro.

        Args:
            event_id: ID del evento feedbackado
            feedback_type: 'thumbs_up' o 'thumbs_down'
            decision_actions: Acciones que tomó Cerebro
            reason: Razón del feedback
            suggested_action: Qué debería haber hecho Cerebro

        Returns:
            int: ID del feedback registrado
        """
        timestamp = datetime.utcnow().isoformat()
        conn = self._get_connection()

        cursor = conn.execute(
            """
            INSERT INTO user_feedback (event_id, decision_actions, feedback_type, reason, suggested_action, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                json.dumps(decision_actions) if decision_actions else None,
                feedback_type,
                reason,
                suggested_action,
                timestamp,
            )
        )

        feedback_id = cursor.lastrowid
        conn.commit()

        # Si es thumbs_down, registrar como posible false_positive
        if feedback_type == "thumbs_down":
            self.record_decision_outcome(
                event_id=event_id,
                outcome_type="false_positive",
                outcome_details=reason,
                auto_detected=False,
            )

        logger.info(f"💾 Feedback registrado: {feedback_type} para evento {event_id}")
        return feedback_id

    def record_decision_outcome(
        self,
        event_id: str,
        outcome_type: str,
        file_path: Optional[str] = None,
        decision_actions: Optional[List[str]] = None,
        outcome_details: Optional[str] = None,
        auto_detected: bool = False,
    ) -> int:
        """
        Registra el resultado de una decisión (si fue correcta o no).

        Args:
            event_id: ID del evento
            outcome_type: 'correct', 'false_positive', 'false_negative'
            file_path: Archivo involucrado
            decision_actions: Acciones tomadas
            outcome_details: Detalles del resultado
            auto_detected: Si fue detectado automáticamente o por feedback

        Returns:
            int: ID del outcome registrado
        """
        timestamp = datetime.utcnow().isoformat()
        conn = self._get_connection()

        cursor = conn.execute(
            """
            INSERT INTO decision_outcomes (event_id, file_path, decision_actions, outcome_type, outcome_details, auto_detected, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                file_path,
                json.dumps(decision_actions) if decision_actions else None,
                outcome_type,
                outcome_details,
                auto_detected,
                timestamp,
            )
        )

        outcome_id = cursor.lastrowid
        conn.commit()

        logger.info(f"📊 Outcome registrado: {outcome_type} para evento {event_id}")
        return outcome_id

    def get_feedback_stats(self, event_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Obtiene estadísticas de feedback.

        Args:
            event_id: ID de evento específico (opcional)

        Returns:
            Dict con estadísticas de feedback
        """
        conn = self._get_connection()

        if event_id:
            cursor = conn.execute(
                "SELECT feedback_type, COUNT(*) as count FROM user_feedback WHERE event_id = ? GROUP BY feedback_type",
                (event_id,)
            )
        else:
            cursor = conn.execute(
                "SELECT feedback_type, COUNT(*) as count FROM user_feedback GROUP BY feedback_type"
            )

        stats = {"thumbs_up": 0, "thumbs_down": 0}
        for row in cursor:
            stats[row["feedback_type"]] = row["count"]

        # Calcular accuracy
        total = stats["thumbs_up"] + stats["thumbs_down"]
        accuracy = stats["thumbs_up"] / total if total > 0 else 1.0

        stats["accuracy"] = accuracy
        stats["total"] = total

        return stats

    def get_learned_rules(self, active_only: bool = True) -> List[Dict[str, Any]]:
        """
        Obtiene reglas aprendidas basadas en feedback.

        Args:
            active_only: Solo retornar reglas activas

        Returns:
            Lista de reglas aprendidas
        """
        conn = self._get_connection()

        query = "SELECT * FROM learned_rules"
        if active_only:
            query += " WHERE active = TRUE"
        query += " ORDER BY feedback_count DESC"

        cursor = conn.execute(query)

        rules = []
        for row in cursor:
            rules.append({
                "id": row["id"],
                "original_rule": row["original_rule"],
                "adjusted_rule": row["adjusted_rule"],
                "reason": row["reason"],
                "feedback_count": row["feedback_count"],
                "success_count": row["success_count"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            })

        return rules

    def suggest_rule_adjustment(
        self,
        original_rule: str,
        adjusted_rule: str,
        reason: str,
    ) -> int:
        """
        Sugiere un ajuste de regla basado en feedback acumulado.

        Args:
            original_rule: Regla original
            adjusted_rule: Nueva regla sugerida
            reason: Razón del ajuste

        Returns:
            int: ID de la regla aprendida
        """
        timestamp = datetime.utcnow().isoformat()
        conn = self._get_connection()

        # Verificar si ya existe regla similar
        cursor = conn.execute(
            "SELECT * FROM learned_rules WHERE original_rule = ? AND adjusted_rule = ?",
            (original_rule, adjusted_rule)
        )
        row = cursor.fetchone()

        if row:
            # Actualizar existente
            conn.execute(
                """
                UPDATE learned_rules SET
                    feedback_count = feedback_count + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (timestamp, row["id"])
            )
            rule_id = row["id"]
        else:
            # Crear nueva
            cursor = conn.execute(
                """
                INSERT INTO learned_rules (original_rule, adjusted_rule, reason, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (original_rule, adjusted_rule, reason, timestamp, timestamp)
            )
            rule_id = cursor.lastrowid

        conn.commit()
        return rule_id

    # ─────────────────────────────────────────────────────────────────────────
    # MÉTODOS DE LIMPIEZA
    # ─────────────────────────────────────────────────────────────────────────

    def cleanup_old_events(self, days: int = 30):
        """
        Elimina eventos antiguos para mantener la DB ligera.

        Args:
            days: Días de antigüedad para eliminar
        """
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

        conn = self._get_connection()
        cursor = conn.execute("SELECT COUNT(*) as cnt FROM file_events WHERE timestamp < ?", (cutoff,))
        count = cursor.fetchone()["cnt"]

        conn.execute("DELETE FROM file_events WHERE timestamp < ?", (cutoff,))
        conn.commit()

        logger.info(f"🧹 {count} eventos antiguos eliminados (> {days} días)")

    def close(self):
        """Cierra la conexión a la base de datos"""
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.debug("🔒 ContextDB cerrada")

    def analyze_learning(self, limit: int = 100) -> Dict[str, Any]:
        """
        Analiza últimos eventos y feedback para sugerir ajustes de reglas.

        Args:
            limit: Cantidad de eventos a analizar

        Returns:
            Dict con sugerencias de ajustes
        """
        conn = self._get_connection()

        # Obtener últimos eventos con feedback negativo
        cursor = conn.execute(
            """
            SELECT
                fe.event_type,
                fe.severity,
                fe.decision_actions,
                uf.reason,
                uf.suggested_action,
                fe.file_path
            FROM file_events fe
            JOIN user_feedback uf ON fe.id = uf.event_id
            WHERE uf.feedback_type = 'thumbs_down'
            ORDER BY fe.timestamp DESC
            LIMIT ?
            """,
            (limit,)
        )

        negative_feedback = []
        for row in cursor:
            negative_feedback.append({
                "event_type": row["event_type"],
                "severity": row["severity"],
                "decision_actions": json.loads(row["decision_actions"]) if row["decision_actions"] else None,
                "reason": row["reason"],
                "suggested_action": row["suggested_action"],
                "file_path": row["file_path"],
            })

        # Analizar patrones de feedback negativo
        suggestions = []

        # Agrupar por tipo de evento y severidad
        patterns = {}
        for fb in negative_feedback:
            key = f"{fb['event_type']}:{fb['severity']}"
            if key not in patterns:
                patterns[key] = {"count": 0, "suggestions": [], "files": []}
            patterns[key]["count"] += 1
            if fb["suggested_action"]:
                patterns[key]["suggestions"].append(fb["suggested_action"])
            if fb["file_path"]:
                patterns[key]["files"].append(fb["file_path"])

        # Generar sugerencias para patrones con 2+ feedbacks negativos
        for pattern, data in patterns.items():
            if data["count"] >= 2:
                event_type, severity = pattern.split(":")

                # Sugerir ajuste de severidad
                if severity == "error" and data["count"] >= 3:
                    suggestions.append({
                        "type": "severity_adjustment",
                        "description": f"Reducir severidad de {event_type} de error a warning",
                        "reason": f"{data['count']} feedbacks negativos",
                        "confidence": min(1.0, data["count"] / 5.0),
                    })

                # Sugerir cambio de acción
                if data["suggestions"]:
                    most_common_suggestion = max(set(data["suggestions"]), key=data["suggestions"].count)
                    suggestions.append({
                        "type": "action_adjustment",
                        "description": f"Para {event_type}, {most_common_suggestion}",
                        "reason": f"Sugerido {len(data['suggestions'])} veces",
                        "confidence": min(1.0, len(data["suggestions"]) / 5.0),
                    })

        # Obtener outcomes para análisis adicional
        cursor = conn.execute(
            """
            SELECT outcome_type, COUNT(*) as count
            FROM decision_outcomes
            WHERE auto_detected = TRUE
            GROUP BY outcome_type
            """
        )

        auto_detected_outcomes = {row["outcome_type"]: row["count"] for row in cursor}

        return {
            "negative_feedback_count": len(negative_feedback),
            "patterns": patterns,
            "suggestions": suggestions,
            "auto_detected_outcomes": auto_detected_outcomes,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # MÉTODOS DE BÚSQUEDA SEMÁNTICA
    # ────��────────────────────────────────────────────────────────────────────

    def _build_event_description(
        self,
        event_type: str,
        source: str,
        severity: str,
        file_path: str,
        payload: Optional[Dict]
    ) -> str:
        """Construye descripción textual del evento para embeddings."""
        parts = [
            f"Event Type: {event_type}",
            f"Source: {source}",
            f"Severity: {severity}",
            f"File: {file_path}"
        ]

        if payload:
            if payload.get("description"):
                parts.append(f"Description: {payload['description']}")
            if payload.get("message"):
                parts.append(f"Message: {payload['message']}")
            if payload.get("finding"):
                parts.append(f"Finding: {payload['finding']}")
            if payload.get("pattern_type"):
                parts.append(f"Pattern: {payload['pattern_type']}")

        return "\n".join(parts)

    def semantic_search(
        self,
        query: str,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Búsqueda semántica de eventos.

        Args:
            query: Texto de búsqueda natural (ej: "authentication bypass")
            filters: Filtros opcionales {source, severity, project}
            limit: Cantidad máxima de resultados

        Returns:
            Lista de eventos similares con metadata enriquecida

        Note:
            Requiere VectorStore inicializado. Si no está disponible,
            retorna lista vacía.
        """
        if not self._hybrid_engine:
            logger.warning("HybridQueryEngine no disponible, semantic_search no funciona")
            return []

        return self._hybrid_engine.semantic_search(query, filters, limit)

    def find_similar_findings(
        self,
        event_id: str,
        limit: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Encuentra hallazgos similares a un evento existente.

        Args:
            event_id: ID del evento de referencia
            limit: Cantidad de resultados similares

        Returns:
            Lista de eventos similares con scores de similitud
        """
        if not self._hybrid_engine:
            return []

        return self._hybrid_engine.find_similar_findings(event_id, limit)

    def get_file_clusters(
        self,
        project: Optional[str] = None,
        min_cluster_size: int = 3
    ) -> List[Dict[str, Any]]:
        """
        Clustering de archivos por similitud semántica.

        Agrupa archivos que tienen eventos similares conceptualmente.

        Args:
            project: Filtrar por proyecto específico
            min_cluster_size: Tamaño mínimo de cluster a retornar

        Returns:
            Lista de clusters con archivos y tópico representativo

        Example:
            [
                {
                    "cluster_id": 0,
                    "files": ["/auth/login.py", "/auth/oauth.py"],
                    "file_count": 2,
                    "topic": "auth-oauth"
                }
            ]
        """
        if not self._hybrid_engine:
            return []

        return self._hybrid_engine.get_file_clusters(project, min_cluster_size)

    def is_vector_available(self) -> bool:
        """Retorna True si VectorStore está disponible y funcionando."""
        return self._vector_store is not None and self._vector_store.is_available()


# Instancia global (lazy initialization)
_context_db: Optional[ContextDB] = None


def get_context_db() -> ContextDB:
    """Obtiene la instancia global de ContextDB"""
    global _context_db
    if _context_db is None:
        _context_db = ContextDB()
    return _context_db
