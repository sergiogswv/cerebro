"""Strict event schemas for agent-dashboard communication.

This module defines the contract between Cerebro agents and the dashboard.
Any event emitted must conform to one of the defined schemas.
"""

from pydantic import BaseModel, Field, field_validator
from typing import Dict, List, Literal, Optional, Any, Union
from enum import Enum
from datetime import datetime
import logging

logger = logging.getLogger("cerebro.schema")


class EventValidationError(Exception):
    """Raised when an event fails schema validation."""
    pass


class EventType(str, Enum):
    """All valid event types in the system."""

    # Pipeline events
    PIPELINE_STARTED = "pipeline_started"
    PIPELINE_AGENT_STARTED = "pipeline_agent_started"
    PIPELINE_AGENT_COMPLETED = "pipeline_agent_completed"
    PIPELINE_SYNTHESIZING = "pipeline_synthesizing"
    PIPELINE_AWAITING_REVIEW = "pipeline_awaiting_review"
    PIPELINE_FIXING = "pipeline_fixing"
    PIPELINE_COMPLETED = "pipeline_completed"
    PIPELINE_PAUSED = "pipeline_paused"
    PIPELINE_ERROR = "pipeline_error"

    # Wizard events
    WIZARD_INIT_STARTED = "wizard_init_started"
    WIZARD_AI_QUERY_START = "wizard_ai_query_start"
    WIZARD_AI_QUERY_COMPLETE = "wizard_ai_query_complete"
    WIZARD_INIT_COMPLETED = "wizard_init_completed"
    WIZARD_STEP = "wizard_step"

    # Analysis events
    ANALYSIS_COMPLETED = "analysis_completed"
    ANALYSIS_ERROR = "analysis_error"

    # Command events
    COMMAND_COMPLETED = "command_completed"
    COMMAND_ERROR = "command_error"

    # Agent readiness
    AGENT_READY = "agent_ready"
    AGENT_ADK_READY = "agent_adk_ready"

    # Interaction
    INTERACTION_REQUIRED = "interaction_required"


class BaseEvent(BaseModel):
    """Base fields for all events."""
    source: str = Field(..., description="Agent or system that emitted the event")
    type: str = Field(..., description="Event type")
    timestamp: Optional[str] = Field(default=None, description="ISO timestamp")
    id: Optional[str] = Field(default=None, description="Unique event ID")

    @field_validator('type')
    @classmethod
    def validate_type(cls, v):
        """Ensure type is a known event type."""
        try:
            EventType(v)
        except ValueError:
            logger.warning(f"Unknown event type: {v}")
        return v


class PipelinePayload(BaseModel):
    """Payload for pipeline events."""
    pipeline_id: Optional[str] = None
    state: Optional[str] = None
    target_file: Optional[str] = None
    agent: Optional[str] = None
    findings_count: Optional[int] = None
    total_count: Optional[int] = None
    auto_fixable_count: Optional[int] = None
    message: Optional[str] = None


class WizardPayload(BaseModel):
    """Payload for wizard events."""
    wizard_step: Optional[int] = None
    total_steps: Optional[int] = Field(default=3, ge=1, le=10)
    message: Optional[str] = None
    framework_detected: Optional[str] = None
    error_info: Optional[str] = None
    api_url: Optional[str] = None


class AnalysisPayload(BaseModel):
    """Payload for analysis events."""
    target: Optional[str] = None
    summary: Optional[str] = None
    finding: Optional[str] = None
    analysis: Optional[str] = None
    action: Optional[str] = None
    raw_status: Optional[str] = None


class CommandPayload(BaseModel):
    """Payload for command events."""
    action: Optional[str] = None
    target: Optional[str] = None
    result: Optional[Any] = None
    status: Optional[str] = None


class InteractionPayload(BaseModel):
    """Payload for interaction_required events."""
    prompt_id: str = Field(..., description="Unique ID for this interaction prompt")
    message: str = Field(..., description="Message to display to user")
    options: Optional[List[str]] = None
    wizard_step: Optional[int] = None
    total_steps: Optional[int] = Field(default=3, ge=1, le=10)

    @field_validator('options')
    @classmethod
    def validate_options(cls, v):
        """Ensure options is a list of strings if provided."""
        if v is not None and not all(isinstance(x, str) for x in v):
            raise ValueError("All options must be strings")
        return v


class AgentReadyPayload(BaseModel):
    """Payload for agent ready events."""
    ready: bool = True
    mode: Optional[str] = None  # 'core' or 'adk'
    version: Optional[str] = None


# Event type-specific models
class PipelineEvent(BaseEvent):
    type: Literal[
        "pipeline_started",
        "pipeline_agent_started",
        "pipeline_agent_completed",
        "pipeline_synthesizing",
        "pipeline_awaiting_review",
        "pipeline_fixing",
        "pipeline_completed",
        "pipeline_paused",
        "pipeline_error",
    ]
    payload: PipelinePayload


class WizardEvent(BaseEvent):
    type: Literal[
        "wizard_init_started",
        "wizard_ai_query_start",
        "wizard_ai_query_complete",
        "wizard_init_completed",
        "wizard_step",
    ]
    payload: WizardPayload


class AnalysisEvent(BaseEvent):
    type: Literal[
        "analysis_completed",
        "analysis_error",
    ]
    payload: AnalysisPayload


class CommandEvent(BaseEvent):
    type: Literal[
        "command_completed",
        "command_error",
    ]
    payload: CommandPayload


class InteractionEvent(BaseEvent):
    type: Literal["interaction_required"]
    payload: InteractionPayload


class AgentReadyEvent(BaseEvent):
    type: Literal["agent_ready", "agent_adk_ready"]
    payload: AgentReadyPayload


# Union type for all events
AgentEvent = Union[
    PipelineEvent,
    WizardEvent,
    AnalysisEvent,
    CommandEvent,
    InteractionEvent,
    AgentReadyEvent,
    BaseEvent,
]


def validate_event(event_data: dict) -> AgentEvent:
    """
    Validate and parse an event dictionary.

    Args:
        event_data: Raw event data from Socket.IO

    Returns:
        Validated AgentEvent subclass

    Raises:
        EventValidationError: If the event fails validation
    """
    if not isinstance(event_data, dict):
        raise EventValidationError(f"Event must be a dict, got {type(event_data)}")

    event_type = event_data.get("type")
    if not event_type:
        raise EventValidationError("Event missing required field: type")

    try:
        # Route to appropriate validator based on type
        if event_type.startswith("pipeline_"):
            return PipelineEvent(**event_data)
        elif event_type.startswith("wizard_"):
            return WizardEvent(**event_data)
        elif event_type in ["analysis_completed", "analysis_error"]:
            return AnalysisEvent(**event_data)
        elif event_type in ["command_completed", "command_error"]:
            return CommandEvent(**event_data)
        elif event_type == "interaction_required":
            return InteractionEvent(**event_data)
        elif event_type in ["agent_ready", "agent_adk_ready"]:
            return AgentReadyEvent(**event_data)
        else:
            # Unknown type, still validate as base event
            logger.debug(f"Validating unknown event type as base: {event_type}")
            return BaseEvent(**event_data)
    except Exception as e:
        raise EventValidationError(f"Failed to validate event: {e}") from e


def create_event(
    source: str,
    event_type: EventType,
    payload: dict,
    **kwargs
) -> dict:
    """
    Create a properly formatted event dictionary.

    Args:
        source: Agent or system name
        event_type: Type of event
        payload: Event payload (will be validated)
        **kwargs: Additional fields (timestamp, id, etc.)

    Returns:
        Event dictionary ready for Socket.IO emission
    """
    event_data = {
        "source": source,
        "type": event_type.value,
        "payload": payload,
        **kwargs
    }

    # Add timestamp if not provided
    if "timestamp" not in event_data:
        event_data["timestamp"] = datetime.utcnow().isoformat() + "Z"

    # Validate
    validated = validate_event(event_data)
    return validated.model_dump()


# Event type to payload model mapping for reference
EVENT_SCHEMAS = {
    EventType.PIPELINE_STARTED: PipelinePayload,
    EventType.PIPELINE_AGENT_STARTED: PipelinePayload,
    EventType.PIPELINE_AGENT_COMPLETED: PipelinePayload,
    EventType.WIZARD_INIT_STARTED: WizardPayload,
    EventType.WIZARD_AI_QUERY_START: WizardPayload,
    EventType.WIZARD_AI_QUERY_COMPLETE: WizardPayload,
    EventType.WIZARD_INIT_COMPLETED: WizardPayload,
    EventType.INTERACTION_REQUIRED: InteractionPayload,
    EventType.ANALYSIS_COMPLETED: AnalysisPayload,
    EventType.COMMAND_COMPLETED: CommandPayload,
    EventType.AGENT_READY: AgentReadyPayload,
}
