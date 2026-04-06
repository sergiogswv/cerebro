"""Circuit breaker pattern for pipeline agent execution.

Provides timeout handling, retry logic, and graceful failure recovery.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Callable
from dataclasses import dataclass, field

logger = logging.getLogger("cerebro.pipeline")


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if recovered


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker."""
    timeout_seconds: int = 300
    max_retries: int = 2
    failure_threshold: int = 3
    recovery_timeout_seconds: int = 60


@dataclass
class AgentAttempt:
    """Tracks a single agent execution attempt."""
    agent: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    retry_count: int = 0


class CircuitBreaker:
    """
    Circuit breaker for agent execution.

    Monitors agent health and prevents cascading failures by
    failing fast when an agent is consistently erroring.
    """

    def __init__(self, config: CircuitBreakerConfig = None):
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._failures: dict[str, int] = {}  # agent -> failure count
        self._last_failure_time: Optional[datetime] = None
        self._current_attempt: Optional[AgentAttempt] = None
        self._on_state_change: Optional[Callable] = None

    @property
    def state(self) -> CircuitState:
        return self._state

    def set_on_state_change(self, callback: Callable[[CircuitState, CircuitState], None]):
        """Set callback for state changes (old_state, new_state)."""
        self._on_state_change = callback

    async def call(self, agent: str, operation: Callable, *args, **kwargs):
        """
        Execute an operation with circuit breaker protection.

        Args:
            agent: Name of the agent being called
            operation: Async callable to execute

        Raises:
            CircuitOpenError: If circuit is open
            TimeoutError: If operation exceeds timeout
            Exception: Original exception from operation
        """
        # Check if we can attempt
        if self._state == CircuitState.OPEN:
            if self._should_attempt_reset():
                self._transition_to(CircuitState.HALF_OPEN)
            else:
                raise CircuitOpenError(f"Circuit open for {agent}, try again later")

        # Track attempt
        self._current_attempt = AgentAttempt(
            agent=agent,
            started_at=datetime.utcnow(),
            retry_count=self._failures.get(agent, 0)
        )

        try:
            # Execute with timeout
            result = await asyncio.wait_for(
                operation(*args, **kwargs),
                timeout=self.config.timeout_seconds
            )

            # Success - reset failures
            self._on_success(agent)
            return result

        except asyncio.TimeoutError:
            self._on_failure(agent, "timeout")
            raise TimeoutError(f"Agent {agent} timed out after {self.config.timeout_seconds}s")

        except Exception as e:
            self._on_failure(agent, str(e))
            raise

    def _on_success(self, agent: str):
        """Handle successful execution."""
        if self._current_attempt:
            self._current_attempt.completed_at = datetime.utcnow()

        if self._state == CircuitState.HALF_OPEN:
            self._transition_to(CircuitState.CLOSED)

        # Clear failures for this agent
        if agent in self._failures:
            del self._failures[agent]

        logger.debug(f"Circuit breaker: {agent} succeeded, failures cleared")

    def _on_failure(self, agent: str, error: str):
        """Handle failed execution."""
        if self._current_attempt:
            self._current_attempt.completed_at = datetime.utcnow()
            self._current_attempt.error = error

        # Increment failure count
        self._failures[agent] = self._failures.get(agent, 0) + 1
        self._last_failure_time = datetime.utcnow()

        # Check if we should open the circuit
        if self._failures[agent] >= self.config.failure_threshold:
            if self._state != CircuitState.OPEN:
                self._transition_to(CircuitState.OPEN)
                logger.warning(
                    f"Circuit breaker OPEN for {agent} "
                    f"({self._failures[agent]} failures)"
                )

    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to try recovery."""
        if not self._last_failure_time:
            return True

        elapsed = (datetime.utcnow() - self._last_failure_time).total_seconds()
        return elapsed >= self.config.recovery_timeout_seconds

    def _transition_to(self, new_state: CircuitState):
        """Transition to a new state."""
        old_state = self._state
        self._state = new_state

        if self._on_state_change:
            try:
                self._on_state_change(old_state, new_state)
            except Exception:
                pass

        logger.info(f"Circuit breaker: {old_state.value} → {new_state.value}")

    def get_status(self) -> dict:
        """Get current circuit breaker status."""
        return {
            "state": self._state.value,
            "failures": self._failures.copy(),
            "current_attempt": {
                "agent": self._current_attempt.agent,
                "started": self._current_attempt.started_at.isoformat(),
                "retries": self._current_attempt.retry_count,
            } if self._current_attempt else None,
        }


class CircuitOpenError(Exception):
    """Raised when circuit breaker is open."""
    pass


class PipelineCircuitManager:
    """
    Manages circuit breakers for all pipeline agents.

    Provides per-agent circuit isolation so one failing agent
    doesn't affect others.
    """

    def __init__(self, config: CircuitBreakerConfig = None):
        self.config = config or CircuitBreakerConfig()
        self._circuits: dict[str, CircuitBreaker] = {}
        self._on_agent_timeout: Optional[Callable[[str], None]] = None
        self._on_agent_error: Optional[Callable[[str, str], None]] = None

    def set_callbacks(
        self,
        on_timeout: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str, str], None]] = None
    ):
        """Set callbacks for agent failures."""
        self._on_agent_timeout = on_timeout
        self._on_agent_error = on_error

    def get_circuit(self, agent: str) -> CircuitBreaker:
        """Get or create circuit breaker for an agent."""
        if agent not in self._circuits:
            circuit = CircuitBreaker(self.config)
            self._circuits[agent] = circuit
        return self._circuits[agent]

    async def execute(
        self,
        agent: str,
        operation: Callable,
        *args,
        **kwargs
    ):
        """
        Execute an operation with circuit breaker protection.

        Returns:
            Result from operation

        Raises:
            CircuitOpenError: If circuit is open
            TimeoutError: On timeout
            Exception: On operation error
        """
        circuit = self.get_circuit(agent)

        try:
            return await circuit.call(agent, operation, *args, **kwargs)
        except TimeoutError:
            if self._on_agent_timeout:
                self._on_agent_timeout(agent)
            raise
        except Exception:
            if self._on_agent_error:
                self._on_agent_error(agent, "execution failed")
            raise

    def reset(self, agent: str = None):
        """Reset circuit breaker(s)."""
        if agent:
            if agent in self._circuits:
                self._circuits[agent] = CircuitBreaker(self.config)
        else:
            self._circuits.clear()

    def get_all_status(self) -> dict:
        """Get status of all circuit breakers."""
        return {
            agent: circuit.get_status()
            for agent, circuit in self._circuits.items()
        }
