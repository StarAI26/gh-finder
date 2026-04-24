"""Topics scorer — rewards repos with relevant topic tags."""

from __future__ import annotations

from src.core.models import Repo
from src.core.interfaces import BaseScorer


class TopicsScorer(BaseScorer):
    """Scores based on topic tag matches."""

    name = "topics"
    default_weight = 0.10

    def __init__(self, weight=None, **params):
        super().__init__(weight, **params)
        self.match_boost = params.get("topic_match_boost", 3)

    def compute(self, repo: Repo) -> float:
        if not repo.metrics.topics:
            return 0.0
        # More topics = more engaged community (capped)
        topic_count = min(len(repo.metrics.topics), 10)
        return min(topic_count * self.match_boost, 30.0)
