"""Graded verification — the named checks behind the trust signal.

The per-check breakdown is the real contract: the headline grade is derived from the
checks, never the reverse. A caller thresholds on the check it cares about.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

CheckStatus = Literal["pass", "fail", "skip"]
Grade = Literal["verified", "partial", "unverified"]


class VerificationCheck(BaseModel):
    name: str
    status: CheckStatus
    reason: str | None = None


class VerificationReport(BaseModel):
    grade: Grade
    checks: list[VerificationCheck] = Field(default_factory=list)
    # Set iff grade == "partial": the check that capped it (name and reason),
    # or the abstention that kept it short of verified. `partial` is never a
    # bare word a caller has to reverse-engineer from the checks.
    partial_reason: str | None = None

    def check(self, name: str) -> VerificationCheck | None:
        return next((c for c in self.checks if c.name == name), None)
