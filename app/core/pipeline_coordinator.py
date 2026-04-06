"""Pipeline Coordinator - Manages sequential analysis pipeline execution."""

import asyncio
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime

from app.pipeline.analysis_pipeline import AnalysisPipeline
from app.pipeline.config_manager import PipelineConfigManager
from app.pipeline.models import AgentFindings, AgentFinding
from app.sockets import emit_pipeline_event

logger = logging.getLogger("cerebro.pipeline")


class PipelineCoordinator:
    """
    Manages the sequential analysis pipeline lifecycle.

    Responsibilities:
    - Start/stop pipeline executions
    - Coordinate agent execution order
    - Handle timeouts and retries
    - Aggregate findings from all agents
    """

    def __init__(self):
        pipeline_config = PipelineConfigManager.get_instance().get_config()
        self._pipeline = AnalysisPipeline(pipeline_config)
        self._timeout_check_task: Optional[asyncio.Task] = None
        self._active_project: Optional[str] = None

    @property
    def pipeline(self) -> AnalysisPipeline:
        return self._pipeline

    @property
    def is_running(self) -> bool:
        """Check if a pipeline is currently running."""
        return (
            self._pipeline.status is not None and
            self._pipeline.status.state not in ["completed", "error", "idle", "paused"]
        )

    def set_active_project(self, project: str):
        """Set the active project for pipeline execution."""
        self._active_project = project

    async def start_analysis(
        self,
        file_path: str,
        agents: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Start a sequential analysis pipeline for a file.

        Args:
            file_path: Path to the file to analyze
            agents: Optional list of agents to run (defaults from config)

        Returns:
            Pipeline status dict
        """
        if not self._active_project:
            return {"error": "No active project selected"}

        # Get agents from config if not specified
        if not agents:
            config = self._pipeline.config
            agents = [
                s.agent for s in config.auto_init.services
                if s.enabled
            ]
            agents.sort(key=lambda a: next(
                (s.priority for s in config.auto_init.services if s.agent == a), 999
            ))

        if not agents:
            return {"error": "No agents enabled in pipeline config"}

        try:
            status = self._pipeline.start(file_path, agents)

            await emit_pipeline_event("started", {
                "pipeline_id": status.id,
                "target_file": file_path,
                "agents_queue": agents,
            })

            # Start timeout checker
            await self._start_timeout_checker()

            return {
                "status": "ok",
                "pipeline_id": status.id,
                "state": status.state.value
            }
        except RuntimeError as e:
            logger.warning(f"Pipeline already running: {e}")
            return {
                "error": str(e),
                "current_state": self._pipeline.status.state.value if self._pipeline.status else None
            }

    async def on_agent_complete(
        self,
        agent: str,
        findings: List[Dict],
        duration: Optional[float] = None
    ) -> Dict[str, Any]:
        """Handle agent analysis completion."""
        agent_findings = AgentFindings(
            agent=agent,
            findings=[
                AgentFinding(
                    id=f.get("id", str(uuid.uuid4())),
                    agent=agent,
                    file_path=f.get("file", f.get("file_path", "")),
                    severity=f.get("severity", "info"),
                    category=f.get("category", "code_quality"),
                    message=f.get("message", str(f)),
                )
                for f in findings
            ],
            completed_at=datetime.utcnow(),
            duration_seconds=duration,
        )

        status = self._pipeline.on_agent_completed(agent, agent_findings)

        await emit_pipeline_event("agent_completed", {
            "pipeline_id": status.id,
            "agent": agent,
            "findings_count": len(findings),
            "state": status.state.value,
        })

        return {
            "status": "ok",
            "pipeline_state": status.state.value,
            "completed_agents": status.completed_agents,
        }

    async def execute_action(self, action: str, **kwargs) -> Dict[str, Any]:
        """
        Execute a pipeline action.

        Actions:
        - retry: Retry current agent after timeout
        - skip: Skip current agent and continue
        - abort: Cancel pipeline
        - approve: Approve fixes (requires finding_ids)
        """
        status = self._pipeline.status

        if not status:
            return {"error": "No pipeline running"}

        if action == "retry":
            new_status = self._pipeline.retry_current_agent()
            return {"status": "ok", "state": new_status.state.value if new_status else None}

        elif action == "skip":
            new_status = self._pipeline.skip_current_agent()
            return {"status": "ok", "state": new_status.state.value if new_status else None}

        elif action == "abort":
            new_status = self._pipeline.abort(kwargs.get("reason", "user_aborted"))
            await self._stop_timeout_checker()
            return {"status": "ok", "state": new_status.state.value if new_status else None}

        elif action == "approve":
            finding_ids = kwargs.get("finding_ids", [])
            new_status = self._pipeline.approve_fixes(finding_ids)
            return {"status": "ok", "state": new_status.state.value if new_status else None}

        else:
            return {"error": f"Unknown action: {action}"}

    def get_status(self) -> Dict[str, Any]:
        """Get current pipeline status."""
        status = self._pipeline.status
        if not status:
            return {"state": "idle"}

        return {
            "pipeline_id": status.id,
            "state": status.state.value,
            "target_file": status.target_file,
            "current_agent": status.current_agent,
            "completed_agents": status.completed_agents,
            "queued_agents": status.queued_agents,
            "round_count": status.round_count,
            "error": status.error,
            "unified_report": status.unified_report.model_dump() if status.unified_report else None,
        }

    def get_circuit_status(self) -> Dict[str, Any]:
        """Get circuit breaker status."""
        return self._pipeline.get_circuit_status()

    async def _start_timeout_checker(self):
        """Start periodic timeout checking."""
        if self._timeout_check_task and not self._timeout_check_task.done():
            return

        async def check_loop():
            while True:
                try:
                    await asyncio.sleep(5)
                    if self._pipeline.check_timeout():
                        logger.warning("Pipeline timeout detected")
                        await emit_pipeline_event("paused", {
                            "reason": "timeout",
                            "agent": self._pipeline.status.current_agent if self._pipeline.status else None,
                        })
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error in timeout check: {e}")

        self._timeout_check_task = asyncio.create_task(check_loop())
        logger.info("Pipeline timeout checker started")

    async def _stop_timeout_checker(self):
        """Stop the timeout checker."""
        if self._timeout_check_task:
            self._timeout_check_task.cancel()
            try:
                await self._timeout_check_task
            except asyncio.CancelledError:
                pass
            self._timeout_check_task = None
            logger.info("Pipeline timeout checker stopped")
