from pydantic import BaseModel, Field
from typing import Dict, List, Literal, Optional
from datetime import datetime
from enum import Enum


class PipelineState(str, Enum):
    IDLE = "idle"
    ANALYZING_SENTINEL = "analyzing_sentinel"
    ANALYZING_WARDEN = "analyzing_warden"
    ANALYZING_ARCHITECT = "analyzing_architect"
    SYNTHESIZING = "synthesizing"
    AWAITING_REVIEW = "awaiting_review"
    FIXING = "fixing"
    POST_FIX_ANALYSIS = "post_fix_analysis"
    COMPLETED = "completed"
    PAUSED = "paused"
    ERROR = "error"


class FindingSeverity(str, Enum):
    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class FindingCategory(str, Enum):
    SECURITY = "security"
    ARCHITECTURE = "architecture"
    CODE_QUALITY = "code_quality"
    PERFORMANCE = "performance"


class AgentFinding(BaseModel):
    id: str
    agent: str
    file_path: str
    line_number: Optional[int] = None
    severity: FindingSeverity
    category: FindingCategory
    message: str
    description: Optional[str] = None
    suggestion: Optional[str] = None
    auto_fixable: bool = False
    fix_instruction: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UnifiedFinding(BaseModel):
    id: str
    file_paths: List[str]
    severity: FindingSeverity
    category: FindingCategory
    message: str
    description: str
    sources: List[str]  # Which agents found this
    occurrences: int
    auto_fixable: bool
    fix_instruction: Optional[str] = None
    requires_manual_review: bool = False


class AgentFindings(BaseModel):
    agent: str
    findings: List[AgentFinding]
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    error: Optional[str] = None


class UnifiedReport(BaseModel):
    target_file: str
    findings: List[UnifiedFinding]
    total_count: int
    by_severity: Dict[FindingSeverity, int]
    by_category: Dict[FindingCategory, int]
    auto_fixable_count: int
    requires_manual_review_count: int
    generated_at: datetime = Field(default_factory=datetime.utcnow)


class ServiceConfig(BaseModel):
    agent: str
    mode: Literal["core", "adk"]
    enabled: bool = True
    priority: int = 1
    startup_delay_seconds: int = 0


class AutoInitConfig(BaseModel):
    enabled: bool = True
    services: List[ServiceConfig] = []


class ExecutionPolicy(BaseModel):
    type: Literal["sequential", "parallel_override"] = "sequential"
    timeout_seconds: int = 300
    skip_if_busy: bool = False
    parallel_override_allowed: bool = True


class SynthesisConfig(BaseModel):
    auto_merge_duplicates: bool = True
    severity_threshold: FindingSeverity = FindingSeverity.WARNING
    require_manual_review_for: List[FindingSeverity] = [FindingSeverity.CRITICAL]
    group_by: List[str] = ["severity", "agent"]


class PostFixConfig(BaseModel):
    auto_verify: bool = True
    max_verification_rounds: int = 2
    debounce_seconds: int = 3
    fail_on_new_issues: bool = False


class PipelineConfig(BaseModel):
    version: str = "1.0"
    auto_init: AutoInitConfig = Field(default_factory=AutoInitConfig)
    execution: ExecutionPolicy = Field(default_factory=ExecutionPolicy)
    synthesis: SynthesisConfig = Field(default_factory=SynthesisConfig)
    post_fix: PostFixConfig = Field(default_factory=PostFixConfig)


class PipelineStatus(BaseModel):
    id: str
    state: PipelineState
    target_file: Optional[str] = None
    current_agent: Optional[str] = None
    completed_agents: List[str] = []
    queued_agents: List[str] = []
    findings: Dict[str, AgentFindings] = {}
    unified_report: Optional[UnifiedReport] = None
    round_count: int = 1
    started_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    error: Optional[str] = None
