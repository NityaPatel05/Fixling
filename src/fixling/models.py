"""Domain models (spec Part D). LLM-facing schemas are kept flat and small."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------- LLM-facing schemas


class IssueSpec(BaseModel):
    summary: str
    kind: Literal["bug", "feature", "test", "other"]
    expected_behavior: str
    actual_behavior: str
    repro_hints: list[str]
    mentioned_symbols: list[str]
    acceptance_criteria: list[str]
    ambiguity: int  # 0-10, 10 = cannot act
    size_estimate: Literal["small", "medium", "large"]

    @field_validator("ambiguity")
    @classmethod
    def _clamp(cls, v: int) -> int:
        return max(0, min(10, v))


class FileRanking(BaseModel):
    files: list[str]  # max 3, must be from offered list
    reason: str


class TargetPick(BaseModel):
    targets: list[str]  # qualnames, max 3, from offered list
    reason: str


class ReproTest(BaseModel):
    test_code: str  # full file content
    explanation: str


class Edit(BaseModel):
    path: str
    search: str
    replace: str


class RepairProposal(BaseModel):
    edits: list[Edit]  # max 4
    rationale: str


class JudgeVerdict(BaseModel):
    fix_is_general: bool
    special_cases_inputs: bool
    modifies_tests_to_pass: bool
    concerns: list[str]
    confidence: int  # 0-10

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, v: int) -> int:
        return max(0, min(10, v))


# ---------------------------------------------------------------- internal models


class Chunk(BaseModel):
    path: str
    qualname: str
    kind: Literal["function", "method", "class", "module"]
    start: int
    end: int
    signature: str
    docstring: str | None = None
    source: str


class EditTarget(BaseModel):
    path: str
    qualname: str
    start: int
    end: int
    source: str


class TestRun(BaseModel):
    __test__ = False  # not a pytest class

    cmd: str
    exit_code: int
    duration_ms: int
    collected: int = 0
    passed: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    failure_details: dict[str, str] = Field(default_factory=dict)
    junit_ok: bool = False
    output: str = ""  # head/tail-truncated combined stdout+stderr


class GateFinding(BaseModel):
    rule: str
    severity: Literal["block", "flag"]
    path: str
    line: int | None = None
    detail: str


class Candidate(BaseModel):
    id: str
    round: int
    temperature: float
    proposal: RepairProposal | None = None
    patch: str | None = None  # unified diff vs base
    patch_hash: str | None = None
    apply_error: str | None = None
    repro_run: TestRun | None = None
    impacted_run: TestRun | None = None
    findings: list[GateFinding] = Field(default_factory=list)
    score: float = 0.0
    agreement: int = 0  # number of identical duplicates folded into this candidate
    # Compact, checkpoint-friendly result fields (the patch text itself lives in runs/<id>/, not in graph state).
    patch_path: str | None = None
    diff_lines: int = 0
    files: list[str] = Field(default_factory=list)
    llm_error: str | None = None
    repro_passed: bool | None = None  # None = no reproduction test exists / could not be run
    regressions: list[str] = Field(default_factory=list)  # baseline-passing impacted tests that now fail
    junit_ok: bool = True
    failure_text: str = ""  # truncated failing output, fed to the refine prompt
    event_seq: int | None = None


class VerifyResult(BaseModel):
    f2p_ok: bool
    p2p_ok: bool
    regressions: list[str]
    new_skips: list[str]
    shuffled_ok: bool | None
    full_run: TestRun
    findings: list[GateFinding]


OutcomeStatus = Literal["verified_fix", "unverified_fix", "repro_only", "needs_clarification", "no_result"]
ConfidenceLevel = Literal["high", "medium", "low", "none"]


class Outcome(BaseModel):
    status: OutcomeStatus
    reason: str
    confidence: ConfidenceLevel
    confidence_reasons: list[str] = Field(default_factory=list)


EventType = Literal[
    "run_started",
    "issue_received",
    "spec",
    "triage",
    "env",
    "retrieval",
    "llm_call",
    "exec",
    "test_run",
    "repro",
    "candidate",
    "gate_finding",
    "selection",
    "verify",
    "strength",
    "judge",
    "budget",
    "stuck",
    "outcome",
    "git",
    "pr_opened",
    "comment",
    "error",
    "interrupt",
    "resume",
    "sanitize",
    "refine",
]
EventSource = Literal["system", "llm", "sandbox", "github", "user"]


class Event(BaseModel):
    run_id: str
    seq: int
    ts: datetime
    type: EventType
    source: EventSource
    node: str | None = None
    cause: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    artifact: str | None = None


class ExecResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
