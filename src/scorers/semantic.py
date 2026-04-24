"""Semantic scorer — rewards repos with clear purpose/tech_stack alignment."""

from __future__ import annotations

from src.core.models import Repo
from src.core.interfaces import BaseScorer


class SemanticScorer(BaseScorer):
    """Scores based on semantic extraction completeness and relevance."""

    name = "semantic"
    default_weight = 0.25

    def __init__(self, weight=None, **params):
        super().__init__(weight, **params)
        self.purpose_boost = params.get("purpose_match_boost", 15)
        self.tech_boost = params.get("tech_stack_match_boost", 5)

    def compute(self, repo: Repo) -> float:
        score = 0.0
        sem = repo.semantics

        # Purpose clarity (0-40)
        if sem.purpose:
            score += 25
            if len(sem.purpose) > 20:
                score += 15  # Detailed purpose

        # Result clarity (0-25)
        if sem.result:
            score += 25

        # Audience clarity (0-15)
        if sem.audience:
            score += 15

        # Tech stack (0-20)
        if sem.tech_stack:
            score += min(len(sem.tech_stack) * self.tech_boost, 20)

        return min(score, 100.0)
