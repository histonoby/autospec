"""Structured Outputs models (§8.6)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Requirement(BaseModel):
    req_id: str
    title: str
    description: str
    acceptance_criteria: list[str]
    priority: Literal["must", "should", "may"]


class RequirementList(BaseModel):
    requirements: list[Requirement]
    assumptions: list[str]
    open_questions: list[str]


class FileSummary(BaseModel):
    path: str
    purpose: str
    key_symbols: list[str]
    dependencies: list[str]


class RelevantFile(BaseModel):
    path: str
    reason: str
    score: int


class RelevanceSelection(BaseModel):
    files: list[RelevantFile]


class Finding(BaseModel):
    title: str
    detail: str
    files: list[str]
    severity: Literal["info", "low", "medium", "high"]


class AnalysisReport(BaseModel):
    summary: str
    answer: str
    relevant_files: list[RelevantFile]
    findings: list[Finding]
    open_questions: list[str]


class PlannedChange(BaseModel):
    path: str
    action: Literal["create", "modify", "delete"]
    description: str


class ChangePlan(BaseModel):
    goal: str
    changes: list[PlannedChange]
    new_tests: list[str]
    risks: list[str]


class RequirementAuditItem(BaseModel):
    req_id: str
    description: str
    status: Literal["PASS", "FAIL", "PARTIAL"]
    implemented_in: list[str]
    verified_by: list[str]
    note: str


class AuditLLMOutput(BaseModel):
    requirements: list[RequirementAuditItem]
    potential_risks: list[str]
    summary: str


class TestVerdict(BaseModel):
    __test__ = False  # keep pytest from collecting this model
    test_name: str
    suspect: bool
    req_id: str
    reason: str


class AuditedRequirement(RequirementAuditItem):
    priority: str = "must"
    missing_impl: list[str] = Field(default_factory=list)
    missing_tests: list[str] = Field(default_factory=list)
    unverified: bool = False


class AuditReport(BaseModel):
    """AuditLLMOutput + values computed mechanically (never by the LLM)."""

    overall_status: Literal["PASS", "WARN"]
    score: float
    gate_passed: bool
    requirements: list[AuditedRequirement]
    potential_risks: list[str]
    summary: str
    coverage: dict = Field(default_factory=dict)
    gate_digest: str = ""
    reasons: list[str] = Field(default_factory=list)
