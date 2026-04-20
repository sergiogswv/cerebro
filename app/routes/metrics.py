from fastapi import APIRouter
from app.models import ApiResponse
from app.orchestrator import orchestrator
import logging

router = APIRouter(tags=["metrics"])
logger = logging.getLogger("cerebro.routes.metrics")

@router.get("/metrics/effectiveness", response_model=ApiResponse, summary="Resumen de efectividad y aciertos de decisiones autónomas")
async def get_effectiveness_metrics():
    try:
        if not hasattr(orchestrator, 'context_db') or not orchestrator.context_db:
            return ApiResponse(ok=False, message="Context DB no inicializada", data={})

        db = orchestrator.context_db
        with db._get_connection() as conn:
            # 1. Total de resoluciones
            total_query = "SELECT outcome_type, COUNT(*) as count FROM decision_outcomes GROUP BY outcome_type"
            total_rows = conn.execute(total_query).fetchall()
            
            outcomes = {"correct": 0, "false_positive": 0, "false_negative": 0}
            for r in total_rows:
                if r["outcome_type"] in outcomes:
                    outcomes[r["outcome_type"]] = r["count"]

            # 2. Desglose de feedback manual (thumbs_up vs thumbs_down)
            feedback_query = "SELECT feedback_type, COUNT(*) as count FROM user_feedback GROUP BY feedback_type"
            feedback_rows = conn.execute(feedback_query).fetchall()
            
            feedback = {"thumbs_up": 0, "thumbs_down": 0}
            for r in feedback_rows:
                if r["feedback_type"] in feedback:
                    feedback[r["feedback_type"]] = r["count"]

            # 3. Tasa de efectividad pura
            total_decisions = sum(outcomes.values())
            effectiveness_rate = (outcomes["correct"] / total_decisions * 100) if total_decisions > 0 else 0

            return ApiResponse(ok=True, message="Métricas de efectividad recuperadas", data={
                "outcomes": outcomes,
                "feedback": feedback,
                "effectiveness_rate": round(effectiveness_rate, 2),
                "total_decisions": total_decisions
            })
    except Exception as e:
        logger.exception("Error recuperando métricas de efectividad")
        return ApiResponse(ok=False, message=f"Error: {e}", data={})
