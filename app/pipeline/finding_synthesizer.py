import hashlib
import logging
from typing import Dict, List
from datetime import datetime

from app.pipeline.models import (
    AgentFinding,
    AgentFindings,
    UnifiedFinding,
    UnifiedReport,
    FindingSeverity,
    FindingCategory,
)

logger = logging.getLogger("cerebro.pipeline")


class FindingSynthesizer:
    """Consolidates findings from multiple agents into unified report."""

    SEVERITY_ORDER = {
        FindingSeverity.CRITICAL: 0,
        FindingSeverity.ERROR: 1,
        FindingSeverity.WARNING: 2,
        FindingSeverity.INFO: 3,
    }

    def consolidate(
        self,
        target_file: str,
        findings_by_agent: Dict[str, AgentFindings],
    ) -> UnifiedReport:
        """
        Consolidate findings from all agents.

        Steps:
        1. Collect all findings
        2. Deduplicate similar findings
        3. Sort by severity
        4. Generate unified report
        """
        all_findings: List[AgentFinding] = []
        for agent_findings in findings_by_agent.values():
            all_findings.extend(agent_findings.findings)

        # Deduplicate
        unified = self._deduplicate(all_findings)

        # Sort by severity
        unified.sort(key=lambda f: self.SEVERITY_ORDER.get(f.severity, 99))

        # Calculate statistics
        by_severity = {}
        by_category = {}
        auto_fixable = 0
        manual_review = 0

        for finding in unified:
            by_severity[finding.severity] = by_severity.get(finding.severity, 0) + 1
            by_category[finding.category] = by_category.get(finding.category, 0) + 1
            if finding.auto_fixable:
                auto_fixable += 1
            if finding.requires_manual_review:
                manual_review += 1

        return UnifiedReport(
            target_file=target_file,
            findings=unified,
            total_count=len(unified),
            by_severity=by_severity,
            by_category=by_category,
            auto_fixable_count=auto_fixable,
            requires_manual_review_count=manual_review,
            generated_at=datetime.utcnow(),
        )

    def _deduplicate(self, findings: List[AgentFinding]) -> List[UnifiedFinding]:
        """
        Group similar findings together.

        Two findings are duplicates if:
        - Same file path
        - Same category
        - Similar message (normalized)
        """
        groups: Dict[str, List[AgentFinding]] = {}

        for finding in findings:
            key = self._generate_finding_key(finding)
            if key not in groups:
                groups[key] = []
            groups[key].append(finding)

        unified = []
        for group in groups.values():
            unified.append(self._merge_findings(group))

        return unified

    def _generate_finding_key(self, finding: AgentFinding) -> str:
        """Generate deduplication key for a finding."""
        # Normalize file path (remove workspace prefix)
        file_key = finding.file_path.split("/")[-1]  # Just filename

        # Normalize message (lowercase, remove extra spaces)
        message_key = " ".join(finding.message.lower().split()[:5])  # First 5 words

        key = f"{file_key}:{finding.category.value}:{message_key}"
        return hashlib.md5(key.encode()).hexdigest()

    def _merge_findings(self, findings: List[AgentFinding]) -> UnifiedFinding:
        """Merge a group of similar findings into one unified finding."""
        if not findings:
            raise ValueError("Cannot merge empty findings list")

        # Use most severe
        most_severe = min(
            findings,
            key=lambda f: self.SEVERITY_ORDER.get(f.severity, 99)
        )

        # Collect all sources
        sources = list(set(f.agent for f in findings))

        # Collect all file paths
        file_paths = list(set(f.file_path for f in findings))

        # Check if auto-fixable (all must agree)
        auto_fixable = all(f.auto_fixable for f in findings)

        # Use the most detailed fix instruction
        fix_instruction = None
        for f in findings:
            if f.fix_instruction:
                fix_instruction = f.fix_instruction
                break

        # Determine if manual review is required
        requires_manual_review = any(
            f.severity == FindingSeverity.CRITICAL for f in findings
        ) or len(findings) > 2  # Multiple agents disagree

        # Merge descriptions
        descriptions = [f.description or f.message for f in findings if f.description or f.message]
        description = descriptions[0] if descriptions else findings[0].message

        return UnifiedFinding(
            id=findings[0].id,  # Use first finding's ID
            file_paths=file_paths,
            severity=most_severe.severity,
            category=most_severe.category,
            message=findings[0].message,  # Original message
            description=description,
            sources=sources,
            occurrences=len(findings),
            auto_fixable=auto_fixable,
            fix_instruction=fix_instruction,
            requires_manual_review=requires_manual_review,
        )
