"""
Decision Engine — Cerebro
Evalúa eventos de agentes y decide acciones basándose en:
- Severidad del evento
- Historial del archivo (ContextDB)
- Reglas configurables

Decisiones posibles:
- notify: Notificar al usuario
- chain: Encadenar a otro agente
- block: Bloquear acción (ej: commit)
- ignore: Solo loggear
"""

import logging
import httpx
from typing import Dict, List, Any, Optional
from enum import Enum
from dataclasses import dataclass, field

logger = logging.getLogger("cerebro.decision_engine")


class DecisionAction(Enum):
    """Acciones que puede tomar el Decision Engine"""
    NOTIFY = "notify"
    CHAIN = "chain"
    BLOCK = "block"
    IGNORE = "ignore"
    ESCALATE = "escalate"
    AUTOFIX = "autofix"  # NUEVO: Modo Proactivo/Autómata


class SeverityLevel(Enum):
    """Niveles de severidad para evaluación"""
    INFO = 0
    WARNING = 1
    ERROR = 2
    CRITICAL = 3


@dataclass
class Decision:
    """Resultado de la evaluación de un evento"""
    actions: List[DecisionAction] = field(default_factory=list)
    target_agents: List[str] = field(default_factory=list)
    notification_level: Optional[str] = None
    reason: str = ""
    confidence: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "actions": [a.value for a in self.actions],
            "target_agents": self.target_agents,
            "notification_level": self.notification_level,
            "reason": self.reason,
            "confidence": self.confidence,
            "metadata": self.metadata
        }


# Reglas de decisión por defecto (configurables vía YAML/DB)
DEFAULT_DECISION_RULES = {
    # Severidad → Acciones por defecto
    "severity_rules": {
        "info": {
            "actions": [DecisionAction.IGNORE],
            "notification_level": None,
        },
        "warning": {
            "actions": [DecisionAction.NOTIFY],
            "notification_level": "warning",
        },
        "error": {
            "actions": [DecisionAction.NOTIFY, DecisionAction.CHAIN],
            "notification_level": "error",
            "chain_to": ["architect"],  # Análisis de impacto
        },
        "critical": {
            "actions": [DecisionAction.NOTIFY, DecisionAction.BLOCK, DecisionAction.CHAIN],
            "notification_level": "critical",
            "chain_to": ["architect", "warden"],  # Análisis + seguridad
        },
    },
    # Reglas específicas por tipo de evento
    "event_rules": {
        "file_change": {
            "base_severity": "info",
            "description": "Cambio de archivo detectado",
        },
        "secret_detected": {
            "base_severity": "critical",
            "description": "Secreto expuesto detectado",
        },
        "architecture_violation": {
            "base_severity": "error",
            "description": "Violación de arquitectura",
            "extra_actions": [DecisionAction.BLOCK],
        },
        "security_finding": {
            "base_severity": "error",
            "description": "Hallazgo de seguridad",
        },
        "code_quality_issue": {
            "base_severity": "warning",
            "description": "Problema de calidad de código",
        },
        "test_missing": {
            "base_severity": "warning",
            "description": "Falta test para archivo modificado",
        },
        # Sentinel events with findings - trigger pipeline analysis
        "sentinel_check_completed": {
            "base_severity": "warning",
            "description": "Sentinel detectó issues de calidad",
            "extra_actions": [DecisionAction.CHAIN],
            "chain_to": ["architect"],
        },
        "sentinel_analyze_completed": {
            "base_severity": "warning",
            "description": "Sentinel análisis completado",
            "extra_actions": [DecisionAction.CHAIN],
            "chain_to": ["architect"],
        },
        "sentinel_analyze_error": {
            "base_severity": "error",
            "description": "Error en análisis de Sentinel",
            "extra_actions": [DecisionAction.CHAIN],
            "chain_to": ["architect"],
        },
        "sentinel_check_error": {
            "base_severity": "error",
            "description": "Error en check de Sentinel",
            "extra_actions": [DecisionAction.CHAIN],
            "chain_to": ["architect"],
        },
        # Core Rust analysis events (from monitor.rs)
        "analysis_completed": {
            "base_severity": "warning",
            "description": "Análisis IA de Sentinel completado",
            "extra_actions": [DecisionAction.CHAIN, DecisionAction.NOTIFY],
            "chain_to": ["architect"],
        },
        "analysis_failed": {
            "base_severity": "error",
            "description": "Falló análisis IA de Sentinel",
            "extra_actions": [DecisionAction.NOTIFY],
        },
        "sentinel_audit_completed": {
            "base_severity": "error",
            "description": "Sentinel audit completado con hallazgos",
            "extra_actions": [DecisionAction.CHAIN],
            "chain_to": ["architect", "warden"],
        },
        "sentinel_analysis_completed": {
            "base_severity": "warning",
            "description": "Análisis de Sentinel completado",
            "extra_actions": [DecisionAction.CHAIN],
            "chain_to": ["architect"],
        },
        "sentinel_file_change": {
            "base_severity": "info",
            "description": "Cambio de archivo detectado por Sentinel",
            "extra_actions": [DecisionAction.CHAIN],
            "chain_to": ["sentinel"],  # Auto-start analysis
        },
        # Architect ADK events
        "architect_lint_completed": {
            "base_severity": "error",
            "description": "Análisis de arquitectura completado con hallazgos",
            "extra_actions": [DecisionAction.BLOCK],
        },
        "architect_analyze_completed": {
            "base_severity": "error",
            "description": "Análisis profundo de arquitectura completado",
            "extra_actions": [DecisionAction.BLOCK],
        },
        "architect_deep_analysis_completed": {
            "base_severity": "error",
            "description": "Análisis profundo de arquitectura completado",
            "extra_actions": [DecisionAction.BLOCK],
        },
        "architect_circular_check_completed": {
            "base_severity": "critical",
            "description": "Dependencias circulares detectadas",
            "extra_actions": [DecisionAction.BLOCK],
        },
    },
    # Reglas por patrón de archivo (criticalidad)
    "file_patterns": {
        "**/auth*": {"criticality": "high", "extra_actions": [DecisionAction.CHAIN]},
        "**/security*": {"criticality": "high", "extra_actions": [DecisionAction.CHAIN]},
        "**/config*": {"criticality": "medium", "extra_actions": []},
        "**/*.env*": {"criticality": "critical", "extra_actions": [DecisionAction.BLOCK]},
        "**/database*": {"criticality": "high", "extra_actions": [DecisionAction.CHAIN]},
        "**/migration*": {"criticality": "high", "extra_actions": [DecisionAction.CHAIN]},
        "**/api*": {"criticality": "medium", "extra_actions": []},
        "**/controller*": {"criticality": "medium", "extra_actions": []},
        "**/service*": {"criticality": "medium", "extra_actions": []},
    },
    # Agentes disponibles para encadenamiento
    "available_agents": ["sentinel", "architect", "warden"],
    # Reglas para autofix proactivo
    "autofix_rules": {
        "confidence_threshold": 0.8,
        "night_mode_reduced_threshold": 0.7,
        "safe_issue_types": ["dead_code", "unused_import", "formatting", "simple_refactor"],
        "require_validation": True,
    },
}


class DecisionEngine:
    """
    Motor de decisiones de Cerebro.
    Evalúa eventos y determina acciones basándose en reglas configurables.
    Usa IA on-demand (Architect) para eventos ambiguos.
    """

    def __init__(self, rules: Optional[Dict] = None, architect_url: Optional[str] = None):
        self.rules = rules or DEFAULT_DECISION_RULES
        self._context_db = None  # Se inyecta después
        self.architect_url = architect_url or "http://localhost:4002"
        # Umbrales para consultar IA
        self.ai_consult_threshold = 0.5  # Si confidence < 0.5, consultar IA
        self.ai_enabled = True

    def set_context_db(self, context_db):
        """Inyecta referencia a ContextDB para consultas de historial"""
        self._context_db = context_db

    async def evaluate(
        self,
        event: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None
    ) -> Decision:
        """
        Evalúa un evento y retorna una decisión.

        Args:
            event: Diccionario con datos del evento (source, type, severity, payload)
            context: Contexto adicional (file_path, previous_events, etc.)

        Returns:
            Decision: Objeto con acciones a tomar
        """
        context = context or {}
        decision = Decision()

        # Extraer datos del evento
        source = event.get("source", "unknown")
        event_type = event.get("type", "unknown")
        severity = event.get("severity", "info").lower()
        payload = event.get("payload", {})
        file_path = payload.get("file") or context.get("file_path")

        logger.debug(f"🧠 Evaluando evento: source={source}, type={event_type}, severity={severity}")

        # 1. Determinar severidad efectiva (puede ser ajustada por contexto)
        effective_severity = self._calculate_effective_severity(
            severity, event_type, file_path, context
        )

        # 2. Obtener acciones base por severidad
        base_actions = self._get_actions_for_severity(effective_severity)
        decision.actions.extend(base_actions["actions"])
        decision.notification_level = base_actions.get("notification_level")

        # 3. Aplicar reglas específicas por tipo de evento
        event_rule = self.rules.get("event_rules", {}).get(event_type, {})
        if event_rule.get("extra_actions"):
            for action in event_rule["extra_actions"]:
                if action not in decision.actions:
                    decision.actions.append(action)

        # 4. Aplicar reglas por patrón de archivo (si aplica)
        if file_path and self._context_db:
            file_criticality = self._context_db.get_file_criticality(file_path)
            if file_criticality == "high":
                if DecisionAction.CHAIN not in decision.actions:
                    decision.actions.append(DecisionAction.CHAIN)
                decision.confidence = min(1.0, decision.confidence + 0.1)
            elif file_criticality == "critical":
                if DecisionAction.BLOCK not in decision.actions:
                    decision.actions.append(DecisionAction.BLOCK)
                if DecisionAction.ESCALATE not in decision.actions:
                    decision.actions.append(DecisionAction.ESCALATE)
                decision.confidence = min(1.0, decision.confidence + 0.2)

        # 5. Determinar agentes para encadenamiento
        if DecisionAction.CHAIN in decision.actions:
            decision.target_agents = self._get_chain_targets(
                severity, event_type, source
            )

        # 6. Determinar si se debe escalar (BLOCK + CRITICAL)
        if DecisionAction.BLOCK in decision.actions:
            decision.reason = f"Acción bloqueada por severidad {effective_severity}"
            if DecisionAction.ESCALATE in decision.actions:
                decision.reason += " y requiere revisión manual"

        # 7. Construir razón legible
        if not decision.reason:
            decision.reason = self._build_reason(
                severity, effective_severity, event_type, file_path
            )

        # 8. Ajustar confianza basada en historial
        if self._context_db and file_path:
            history = self._context_db.get_file_history(file_path)
            if history and history.get("repeat_offender"):
                decision.confidence = min(1.0, decision.confidence + 0.15)
                decision.metadata["repeat_offender"] = True

        # 9. Consultar IA si confianza es baja o evento es ambiguo
        if self._should_consult_ai(decision, event):
            ai_result = await self.consult_ai(event, context)
            if ai_result:
                self._apply_ai_to_decision(decision, ai_result)
                decision.metadata["ai_consulted"] = True

        # 10. Aplicar aprendizaje de Autofix previo (Learning Loop)
        if self._context_db and file_path:
            patterns = self._context_db.get_related_patterns(file_path)
            for p in patterns:
                if p["pattern_type"] == "autofix_success":
                    # Si ya funcionó antes, subir confianza y marcar para trigger rápido
                    decision.confidence = min(1.0, decision.confidence + 0.2)
                    decision.metadata["previously_fixed_successfully"] = True
                    decision.reason += f"; (Aprendizaje) Archivo reparado con éxito el {p['last_seen'][:10]}"
                    
                elif p["pattern_type"] == "autofix_exhausted":
                    # Si ya falló repetidamente, ESCALAR de inmediato y NO reintentar
                    if DecisionAction.ESCALATE not in decision.actions:
                        decision.actions.append(DecisionAction.ESCALATE)
                    if DecisionAction.BLOCK not in decision.actions:
                        decision.actions.append(DecisionAction.BLOCK)
                    
                    decision.confidence = 1.0
                    decision.reason = "⚠️ (Aprendizaje) Autofix falló repetidamente en este archivo. Bloqueo manual requerido."
                    decision.metadata["suppress_autofix"] = True
                    decision.metadata["failure_history"] = p["last_seen"]

        # 11. Evaluar si corresponde AUTOFIX (Cerebro Proactivo)
        # Solo para eventos de agentes: no evaluamos autofixes de eventos de Executor o Scheduler
        agent_sources = {"sentinel", "architect", "warden"}
        if source in agent_sources and event_type.endswith("_completed"):
            try:
                from app.proactive_scheduler import get_proactive_scheduler
                scheduler = get_proactive_scheduler()
                night_mode = scheduler.is_night_mode_active()
                if self.should_autofix(event, night_mode):
                    if DecisionAction.AUTOFIX not in decision.actions:
                        decision.actions.append(DecisionAction.AUTOFIX)
                        decision.reason += "; Autofix automático elegible"
                        logger.info(f"⚡ AUTOFIX añadido a la decisión (night_mode={night_mode})")
            except Exception as _ae:
                logger.debug(f"No se pudo evaluar autofix: {_ae}")

        logger.info(f"✅ Decisión: actions={[a.value for a in decision.actions]}, "
                    f"targets={decision.target_agents}, confidence={decision.confidence:.2f}")

        return decision

    def _calculate_effective_severity(
        self,
        severity: str,
        event_type: str,
        file_path: Optional[str],
        context: Dict
    ) -> str:
        """
        Calcula la severidad efectiva considerando contexto.
        Puede elevar la severidad si el archivo es crítico o tiene historial problemático.
        """
        severity_map = {
            "info": SeverityLevel.INFO,
            "warning": SeverityLevel.WARNING,
            "error": SeverityLevel.ERROR,
            "critical": SeverityLevel.CRITICAL,
        }

        effective = severity_map.get(severity.lower(), SeverityLevel.INFO)

        # Elevar si es tipo de evento conocido como problemático
        event_rule = self.rules.get("event_rules", {}).get(event_type, {})
        event_severity = event_rule.get("base_severity")
        if event_severity:
            event_level = severity_map.get(event_severity, SeverityLevel.INFO)
            if event_level.value > effective.value:
                effective = event_level
                logger.debug(f"  ↳ Severidad elevada a {event_severity} por tipo de evento")

        # Elevar si archivo es crítico
        if file_path and self._context_db:
            criticality = self._context_db.get_file_criticality(file_path)
            if criticality == "critical" and effective.value < SeverityLevel.CRITICAL.value:
                effective = SeverityLevel.CRITICAL
                logger.debug(f"  ↳ Severidad elevada a critical por archivo crítico")
            elif criticality == "high" and effective.value < SeverityLevel.ERROR.value:
                effective = SeverityLevel.ERROR
                logger.debug(f"  ↳ Severidad elevada a error por archivo de alta criticidad")

        return effective.name.lower()

    def _get_actions_for_severity(self, severity: str) -> Dict:
        """Obtiene acciones base para una severidad dada"""
        severity_rules = self.rules.get("severity_rules", {})
        rule = severity_rules.get(severity, severity_rules.get("info"))

        return {
            "actions": rule.get("actions", [DecisionAction.IGNORE]),
            "notification_level": rule.get("notification_level"),
        }

    def _get_chain_targets(
        self,
        severity: str,
        event_type: str,
        source: str
    ) -> List[str]:
        """Determina qué agentes deben ser encadenados"""
        targets = []
        available = self.rules.get("available_agents", [])

        # Reglas específicas por tipo de evento
        event_rule = self.rules.get("event_rules", {}).get(event_type, {})
        if event_rule.get("chain_to"):
            for agent in event_rule["chain_to"]:
                if agent in available and agent != source:
                    targets.append(agent)

        # Reglas por severidad
        severity_rule = self.rules.get("severity_rules", {}).get(severity, {})
        if severity_rule.get("chain_to"):
            for agent in severity_rule["chain_to"]:
                if agent in available and agent != source and agent not in targets:
                    targets.append(agent)

        # Default: architect para análisis en errores
        if not targets and severity in ["error", "critical"]:
            if "architect" in available and "architect" != source:
                targets.append("architect")

        return targets

    def _build_reason(
        self,
        severity: str,
        effective_severity: str,
        event_type: str,
        file_path: Optional[str]
    ) -> str:
        """Construye una razón legible para la decisión"""
        parts = []

        if effective_severity != severity:
            parts.append(f"Severidad ajustada de {severity} a {effective_severity}")

        event_desc = self.rules.get("event_rules", {}).get(event_type, {}).get("description")
        if event_desc:
            parts.append(event_desc)

        if file_path:
            parts.append(f"archivo: {file_path}")

        return "; ".join(parts) if parts else f"Decisión basada en severidad {severity}"

    def add_rule(self, rule_type: str, rule_key: str, rule_value: Any):
        """Agrega o actualiza una regla en tiempo de ejecución"""
        if rule_type not in self.rules:
            self.rules[rule_type] = {}
        self.rules[rule_type][rule_key] = rule_value
        logger.info(f"📝 Regla agregada: {rule_type}.{rule_key}")

    def get_decision_matrix(self) -> Dict:
        """Retorna la matriz de decisiones actual para inspección"""
        return {
            "severity_rules": {
                k: {
                    "actions": [a.value for a in v.get("actions", [])],
                    "notification_level": v.get("notification_level"),
                    "chain_to": v.get("chain_to", []),
                }
                for k, v in self.rules.get("severity_rules", {}).items()
            },
            "event_rules": self.rules.get("event_rules", {}),
            "file_patterns": list(self.rules.get("file_patterns", {}).keys()),
        }

    # ─────────────────────────────────────────────────────────────────────────
    # IA ON-DEMAND
    # ─────────────────────────────────────────────────────────────────────────

    async def consult_ai(
        self,
        event: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Consulta a Architect (IA) para eventos ambiguos o de baja confianza.

        Args:
            event: Evento a evaluar
            context: Contexto adicional

        Returns:
            Dict con análisis de IA o None si no hay respuesta
        """
        if not self.ai_enabled:
            logger.debug("🤖 IA deshabilitada, saltando consulta")
            return None

        context = context or {}
        file_path = event.get("payload", {}).get("file") or context.get("file_path")

        if not file_path:
            logger.debug("🤖 Sin file_path, saltando consulta IA")
            return None

        logger.info(f"🤖 Consultando IA para evento ambiguo: {event.get('type')} en {file_path}")

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Llamar a Architect para análisis del archivo
                resp = await client.post(
                    f"{self.architect_url}/ai/analyze-file",
                    json={
                        "file_path": file_path,
                        "event_type": event.get("type"),
                        "event_severity": event.get("severity"),
                        "context": context,
                    }
                )

                if resp.status_code == 200:
                    result = resp.json()
                    if result.get("ok"):
                        logger.info(f"✅ IA respondió: {result.get('analysis', {})[:100]}")
                        return {
                            "analysis": result.get("analysis"),
                            "risk_level": result.get("risk_level"),
                            "recommendation": result.get("recommendation"),
                            "confidence": result.get("confidence", 0.8),
                        }
                    else:
                        logger.warning(f"⚠️ IA respondió sin éxito: {result.get('error')}")
                else:
                    logger.warning(f"⚠️ HTTP {resp.status_code} de Architect")

        except httpx.TimeoutException:
            logger.warning("⏰ Timeout consultando IA")
        except Exception as e:
            logger.warning(f"⚠️ Error consultando IA: {e}")

        return None

    def _should_consult_ai(self, decision: Decision, event: Dict[str, Any]) -> bool:
        """
        Determina si se debe consultar IA basado en confianza y tipo de evento.
        """
        # Siempre consultar si es evento crítico sin historial claro
        if event.get("severity") == "critical" and decision.confidence < 0.7:
            return True

        # Consultar si confianza es baja
        if decision.confidence < self.ai_consult_threshold:
            return True

        # Consultar si es archivo sin historial y severidad media-alta
        if event.get("severity") in ["error", "critical"] and not decision.metadata.get("repeat_offender"):
            return True

        return False

    def _apply_ai_to_decision(self, decision: Decision, ai_result: Dict[str, Any]) -> Decision:
        """
        Aplica el resultado de IA a una decisión existente.
        Puede elevar severidad, agregar acciones, o ajustar confianza.
        """
        risk_level = ai_result.get("risk_level", "medium")
        recommendation = ai_result.get("recommendation", "")
        ai_confidence = ai_result.get("confidence", 0.8)

        # Ajustar confianza basada en IA
        decision.confidence = (decision.confidence + ai_confidence) / 2

        # Elevar severidad si IA indica alto riesgo
        if risk_level == "high":
            if DecisionAction.CHAIN not in decision.actions:
                decision.actions.append(DecisionAction.CHAIN)
            if DecisionAction.ESCALATE not in decision.actions:
                decision.actions.append(DecisionAction.ESCALATE)
            decision.reason += f"; IA recomienda escalar (risk={risk_level})"

        elif risk_level == "medium":
            if DecisionAction.CHAIN not in decision.actions:
                decision.actions.append(DecisionAction.CHAIN)
            decision.reason += f"; IA recomienda análisis (risk={risk_level})"

        # Agregar recomendación a metadata
        if recommendation:
            decision.metadata["ai_recommendation"] = recommendation

        # Agregar agentes específicos si IA los sugiere
        if "target_agents" in ai_result:
            for agent in ai_result["target_agents"]:
                if agent not in decision.target_agents:
                    decision.target_agents.append(agent)

        logger.info(f"🤖 Decisión ajustada por IA: confidence={decision.confidence:.2f}, "
                    f"actions={[a.value for a in decision.actions]}")

        return decision

    # ─────────────────────────────────────────────────────────────────────────
    # AUTOFIX — Modo Proactivo/Autómata
    # ─────────────────────────────────────────────────────────────────────────

    def should_autofix(self, event: Dict[str, Any], night_mode_active: bool = False) -> bool:
        """
        Determina si un evento merece autofix automático.

        Args:
            event: Evento del agente con payload estandarizado
            night_mode_active: True si el sistema está en ventana horaria nocturna

        Returns:
            True si corresponde disparar un AUTOFIX
        """
        autofix_rules = self.rules.get("autofix_rules", {})
        threshold = autofix_rules.get("confidence_threshold", 0.8)
        night_threshold = autofix_rules.get("night_mode_reduced_threshold", 0.7)
        safe_types = autofix_rules.get("safe_issue_types", [])

        payload = event.get("payload", {})
        confidence = payload.get("confidence", event.get("confidence", 0.0))
        issue_type = payload.get("issue_type", "")

        # Nunca autofix si está configurado para suprimir (aprendizaje previo)
        if payload.get("suppress_autofix") or event.get("suppress_autofix"):
            logger.debug("🚫 Autofix suprimido por aprendizaje previo")
            return False

        # Modo nocturno: threshold reducido para tipos seguros
        if night_mode_active and confidence >= night_threshold:
            logger.debug(f"🌙 Autofix por modo nocturno (confidence={confidence:.2f} >= {night_threshold})")
            return True

        # Confianza alta + tipo seguro: autofix inmediato
        if confidence >= threshold and issue_type in safe_types:
            logger.debug(f"✅ Autofix: confidence={confidence:.2f}, issue_type={issue_type}")
            return True

        return False
