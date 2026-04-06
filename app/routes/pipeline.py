import logging
from fastapi import APIRouter, HTTPException, Query
from typing import List, Optional

from app.pipeline.config_manager import PipelineConfigManager
from app.pipeline.config_validator import ConfigValidator, ConfigConsistencyChecker
from app.pipeline.models import PipelineConfig, PipelineStatus, ServiceConfig
from app.models import ApiResponse

router = APIRouter(tags=["pipeline"])
logger = logging.getLogger("cerebro.pipeline")


@router.get("/config/validate", response_model=ApiResponse)
async def validate_pipeline_config(project_path: str = Query(..., description="Path to project being analyzed")):
    """
    Validate pipeline config against agent configs.

    Checks for mode mismatches, timeout conflicts, and unavailable services.
    """
    try:
        manager = PipelineConfigManager.get_instance()
        pipeline_config = manager.get_config()

        checker = ConfigConsistencyChecker(pipeline_config)
        report = checker.check_now(project_path)

        return ApiResponse(
            ok=report.valid,
            data={
                "valid": report.valid,
                "conflicts": [c.model_dump() for c in report.conflicts],
                "warnings": [w.model_dump() for w in report.warnings],
            },
            message="Config validation complete" if report.valid else f"Found {len(report.conflicts)} conflict(s)"
        )
    except Exception as e:
        logger = logging.getLogger("cerebro.pipeline")
        logger.error(f"Config validation error: {e}")
        return ApiResponse(ok=False, error=str(e))


@router.get("/config", response_model=ApiResponse)
async def get_pipeline_config():
    """Get current pipeline configuration."""
    manager = PipelineConfigManager.get_instance()
    config = manager.get_config()
    return ApiResponse(ok=True, data=config.model_dump())


@router.post("/config", response_model=ApiResponse)
async def update_pipeline_config(config: PipelineConfig):
    """Update pipeline configuration."""
    manager = PipelineConfigManager.get_instance()
    manager.update_config(config)
    return ApiResponse(ok=True, message="Configuration updated")


@router.post("/config/services/reorder", response_model=ApiResponse)
async def reorder_services(ordered_agents: List[str]):
    """
    Reorder services by priority.

    ordered_agents: List of agent names in desired priority order (first = highest priority)
    """
    manager = PipelineConfigManager.get_instance()
    config = manager.get_config()

    # Reassign priorities based on order
    for i, agent in enumerate(ordered_agents):
        for service in config.auto_init.services:
            if service.agent == agent:
                service.priority = i + 1

    manager.update_config(config)
    return ApiResponse(ok=True, message="Service order updated")


@router.get("/status", response_model=ApiResponse)
async def get_pipeline_status():
    """Get current pipeline execution status."""
    from app.orchestrator import orchestrator

    status = await orchestrator.get_pipeline_status()
    return ApiResponse(ok=True, data=status)


@router.post("/action", response_model=ApiResponse)
async def pipeline_action(
    action: str,
    finding_ids: Optional[List[str]] = None,
    agent: Optional[str] = None
):
    """
    Perform action on current pipeline.

    Actions:
    - "approve": Approve selected fixes (requires finding_ids)
    - "skip": Skip current agent and continue
    - "retry": Retry current agent after timeout/error
    - "abort": Cancel entire pipeline
    - "reset_circuit": Reset circuit breaker for agent
    """
    from app.orchestrator import orchestrator

    if action == "reset_circuit":
        if not agent:
            return ApiResponse(ok=False, error="agent parameter required for reset_circuit")
        orchestrator.analysis_pipeline.reset_circuit(agent)
        return ApiResponse(ok=True, message=f"Circuit breaker reset for {agent}")

    result = await orchestrator.pipeline_action(action, finding_ids=finding_ids)

    if "error" in result:
        return ApiResponse(ok=False, error=result["error"])

    return ApiResponse(ok=True, data=result, message=f"Action {action} executed")
