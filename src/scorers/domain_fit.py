"""Domain fit scorer — checks if repo matches domain-specific keywords."""

from __future__ import annotations

from src.core.models import Repo
from src.core.interfaces import BaseScorer


class DomainFitScorer(BaseScorer):
    """Scores repos based on domain keyword matching in description/README."""

    name = "domain_fit"
    default_weight = 0.10

    def __init__(self, weight=None, **params):
        super().__init__(weight, **params)
        self.keywords: list[str] = params.get("keywords", [])

    def set_keywords(self, keywords: list[str]) -> None:
        """Set domain keywords at runtime."""
        self.keywords = [k.lower() for k in keywords]

    def compute(self, repo: Repo) -> float:
        if not self.keywords:
            return 50.0  # Neutral when no keywords set

        text = " ".join(filter(None, [
            repo.description or "",
            repo.semantics.purpose or "",
            repo.semantics.summary or "",
            " ".join(repo.metrics.topics or []),
        ])).lower()

        matches = sum(1 for kw in self.keywords if kw in text)
        ratio = matches / len(self.keywords)

        return min(ratio * 100, 100.0)
