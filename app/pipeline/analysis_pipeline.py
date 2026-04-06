import uuid
import logging
from datetime import datetime
from typing import Dict, List, Optional, Callable

from app.pipeline.models import (
    PipelineState,
    PipelineStatus,
    AgentFindings,
    PipelineConfig,
    FindingSeverity,
)
from app.pipeline.finding_synthesizer import FindingSynthesizer
from app.pipeline.circuit_breaker import PipelineCircuitManager, CircuitBreakerConfig

logger = logging.getLogger("cerebro.pipeline")


class AnalysisPipeline:
    """
    State machine for sequential analysis pipeline.

    Manages the lifecycle: IDLE → ANALYZING_* → SYNTHESIZING → ... → COMPLETED
    Includes circuit breaker for timeout handling and retry logic.
    """

    def __init__(self, config: PipelineConfig):
        self.config = config
        self.synthesizer = FindingSynthesizer()
        self._status: Optional[PipelineStatus] = None
        self._handlers: Dict[PipelineState, List[Callable]] = {}

        # Circuit breaker for agent timeouts
        cb_config = CircuitBreakerConfig(
            timeout_seconds=config.execution.timeout_seconds,
            max_retries=2,
            failure_threshold=2,
            recovery_timeout_seconds=60,
        )
        self._circuit_manager = PipelineCircuitManager(cb_config)
        self._circuit_manager.set_callbacks(
            on_timeout=self._on_agent_timeout,
            on_error=self._on_agent_error
        )

        # Pending agent completions (for async handling)
        self._pending_agent: Optional[str] = None
        self._agent_start_time: Optional[datetime] = None

    @property
    def status(self) -> Optional[PipelineStatus]:
        return self._status

    def start(
        self,
        target_file: str,
        agents_queue: List[str],
    ) -> PipelineStatus:
        """Start a new analysis pipeline."""
        if self._status and self._status.state not in [
            PipelineState.COMPLETED,
            PipelineState.ERROR,
            PipelineState.IDLE,
        ]:
            raise RuntimeError(f"Pipeline already running: {self._status.state}")

        self._status = PipelineStatus(
            id=str(uuid.uuid4()),
            state=PipelineState.IDLE,
            target_file=target_file,
            current_agent=None,
            completed_agents=[],
            queued_agents=agents_queue.copy(),
            findings={},
            round_count=1,
            started_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )

        logger.info(f"Pipeline {self._status.id} started for {target_file}")

        # Transition to first agent
        if agents_queue:
            self._transition_to_next_agent()

        return self._status

    def on_agent_completed(
        self,
        agent: str,
        findings: AgentFindings,
    ) -> Optional[PipelineStatus]:
        """
        Called when an agent completes analysis.

        Returns updated status or None if pipeline not running.
        """
        if not self._status:
            return None

        if self._status.current_agent != agent:
            logger.warning(f"Agent {agent} completed but current is {self._status.current_agent}")
            return self._status

        # Store findings
        self._status.findings[agent] = findings
        self._status.completed_agents.append(agent)
        self._status.updated_at = datetime.utcnow()

        logger.info(f"Agent {agent} completed with {len(findings.findings)} findings")

        # Check if more agents in queue
        if self._status.queued_agents:
            self._transition_to_next_agent()
        else:
            # All agents completed, move to synthesis
            self._transition_to(PipelineState.SYNTHESIZING)
            self._run_synthesis()

        return self._status

    def on_agent_error(self, agent: str, error: str) -> PipelineStatus:
        """Called when an agent fails."""
        if not self._status:
            raise RuntimeError("Pipeline not running")

        self._status.error = f"Agent {agent} failed: {error}"
        self._transition_to(PipelineState.ERROR)

        logger.error(f"Pipeline {self._status.id} error: {self._status.error}")

        return self._status

    def _transition_to_next_agent(self) -> None:
        """Transition to the next agent in queue."""
        if not self._status or not self._status.queued_agents:
            return

        next_agent = self._status.queued_agents.pop(0)
        self._status.current_agent = next_agent

        # Map agent to state
        state_map = {
            "sentinel": PipelineState.ANALYZING_SENTINEL,
            "warden": PipelineState.ANALYZING_WARDEN,
            "architect": PipelineState.ANALYZING_ARCHITECT,
        }

        new_state = state_map.get(next_agent, PipelineState.ERROR)
        self._transition_to(new_state)

    def _transition_to(self, new_state: PipelineState) -> None:
        """Transition to a new state."""
        if not self._status:
            return

        old_state = self._status.state
        self._status.state = new_state
        self._status.updated_at = datetime.utcnow()

        logger.info(f"Pipeline {self._status.id}: {old_state} → {new_state}")

        # Notify handlers
        for handler in self._handlers.get(new_state, []):
            try:
                handler(self._status)
            except Exception as e:
                logger.error(f"State handler error: {e}")

    def _run_synthesis(self) -> None:
        """Run synthesis and determine next step."""
        if not self._status:
            return

        # Generate unified report
        report = self.synthesizer.consolidate(
            self._status.target_file,
            self._status.findings,
        )

        self._status.unified_report = report

        logger.info(
            f"Synthesis complete: {report.total_count} findings, "
            f"{report.auto_fixable_count} auto-fixable, "
            f"{report.requires_manual_review_count} need review"
        )

        # Decide next step
        if report.total_count == 0:
            # No issues found
            self._transition_to(PipelineState.COMPLETED)
        elif report.requires_manual_review_count > 0:
            # Need manual review for critical items
            self._transition_to(PipelineState.AWAITING_REVIEW)
        else:
            # Can proceed to auto-fix
            self._transition_to(PipelineState.FIXING)

    def get_next_agent(self) -> Optional[str]:
        """Get the next agent to run, or None if complete."""
        if not self._status:
            return None
        return self._status.current_agent

    def should_pause_for_review(self) -> bool:
        """Check if pipeline should pause for manual review."""
        if not self._status or not self._status.unified_report:
            return False

        critical_in_review = any(
            f.severity == FindingSeverity.CRITICAL
            for f in self._status.unified_report.findings
            if f.requires_manual_review
        )

        return critical_in_review

    def approve_fixes(self, selected_finding_ids: List[str]) -> PipelineStatus:
        """User has approved specific fixes to apply."""
        if not self._status:
            raise RuntimeError("Pipeline not running")

        if self._status.state != PipelineState.AWAITING_REVIEW:
            raise RuntimeError(f"Cannot approve fixes in state {self._status.state}")

        # Filter to only selected findings
        if self._status.unified_report:
            self._status.unified_report.findings = [
                f for f in self._status.unified_report.findings
                if f.id in selected_finding_ids
            ]

        self._transition_to(PipelineState.FIXING)
        return self._status

    # Circuit breaker handlers
    def _on_agent_timeout(self, agent: str):
        """Called when an agent times out."""
        logger.warning(f"⏱️ Agent {agent} timed out")
        if self._status:
            self._status.error = f"Agent {agent} timed out after {self.config.execution.timeout_seconds}s"
            self._transition_to(PipelineState.PAUSED)

    def _on_agent_error(self, agent: str, error: str):
        """Called when an agent errors."""
        logger.error(f"❌ Agent {agent} error: {error}")
        if self._status:
            self.on_agent_error(agent, error)

    async def execute_agent(self, agent: str, operation: Callable, *args, **kwargs):
        """
        Execute an agent operation with circuit breaker protection.

        This is the recommended way to run agent operations from the orchestrator.
        """
        self._pending_agent = agent
        self._agent_start_time = datetime.utcnow()

        try:
            result = await self._circuit_manager.execute(agent, operation, *args, **kwargs)
            return result
        finally:
            self._pending_agent = None
            self._agent_start_time = None

    def check_timeout(self) -> bool:
        """
        Check if current agent has exceeded timeout.
        Called periodically by orchestrator.

        Returns True if timeout detected and pipeline was paused.
        """
        if not self._status or not self._agent_start_time:
            return False

        elapsed = (datetime.utcnow() - self._agent_start_time).total_seconds()
        if elapsed > self.config.execution.timeout_seconds:
            logger.warning(f"⏱️ Timeout detected for {self._pending_agent}: {elapsed}s")
            self._on_agent_timeout(self._pending_agent)
            return True
        return False

    def get_circuit_status(self) -> dict:
        """Get circuit breaker status for monitoring."""
        return self._circuit_manager.get_all_status()

    def reset_circuit(self, agent: str = None):
        """Reset circuit breaker for an agent or all agents."""
        self._circuit_manager.reset(agent)
        logger.info(f"Circuit breaker reset for {agent or 'all agents'}")

    def retry_current_agent(self) -> Optional[PipelineStatus]:
        """
        Retry the current agent after a timeout/pause.

        Returns updated status or None if no agent to retry.
        """
        if not self._status:
            return None

        if self._status.state != PipelineState.PAUSED:
            logger.warning(f"Cannot retry in state {self._status.state}")
            return self._status

        # Clear error and reset circuit for current agent
        self._status.error = None
        if self._status.current_agent:
            self.reset_circuit(self._status.current_agent)

        # Resume from where we left off
        agent = self._status.current_agent
        logger.info(f"Retrying agent {agent}")
        self._transition_to(self._state_for_agent(agent))

        return self._status

    def skip_current_agent(self) -> Optional[PipelineStatus]:
        """
        Skip the current agent and continue with next.

        Returns updated status.
        """
        if not self._status:
            return None

        agent = self._status.current_agent
        logger.warning(f"Skipping agent {agent}")

        # Mark as completed (empty findings)
        self._status.completed_agents.append(agent)
        self._status.error = None

        # Continue to next
        if self._status.queued_agents:
            self._transition_to_next_agent()
        else:
            self._transition_to(PipelineState.SYNTHESIZING)
            self._run_synthesis()

        return self._status

    def abort(self, reason: str = "aborted") -> Optional[PipelineStatus]:
        """Abort the pipeline."""
        if not self._status:
            return None

        self._status.error = reason
        self._transition_to(PipelineState.ERROR)
        return self._status

    def _state_for_agent(self, agent: str) -> PipelineState:
        """Get the pipeline state for an agent."""
        state_map = {
            "sentinel": PipelineState.ANALYZING_SENTINEL,
            "warden": PipelineState.ANALYZING_WARDEN,
            "architect": PipelineState.ANALYZING_ARCHITECT,
        }
        return state_map.get(agent, PipelineState.ERROR)
