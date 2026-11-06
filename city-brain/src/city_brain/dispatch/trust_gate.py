from __future__ import annotations

from dataclasses import dataclass

from city_brain.config import settings
from city_brain.schemas.guardian import GuardianVerdict, Verdict


@dataclass
class TrustGateResult:
    allowed: bool
    reason: str


class TrustGate:
    def __init__(self, threshold: float | None = None) -> None:
        self.threshold = threshold if threshold is not None else settings.trust_threshold

    def check(self, verdict: GuardianVerdict | None) -> TrustGateResult:
        if verdict is None:
            return TrustGateResult(False, "guardian_unreachable")
        if verdict.verdict != Verdict.TRUSTED:
            return TrustGateResult(False, f"verdict_{verdict.verdict}")
        if verdict.trust_score < self.threshold:
            return TrustGateResult(False, "below_threshold")
        return TrustGateResult(True, "trusted")
