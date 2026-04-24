"""Momentum scorer — detects recent activity trends.

Scores based on push recency tiers.
"""

from __future__ import annotations

from src.core.models import Repo
from src.core.interfaces import BaseScorer


class MomentumScorer(BaseScorer):
    """Momentum detection via push recency.

    Tiers:
      - Active (< 30 days): 100
      - Recent (< 90 days): 70
      - Dormant (< 180 days): 40
      - Stale (> 180 days): 10
      - No data: 0
    """

    name = "momentum"
    default_weight = 0.15

    def __init__(self, weight=None, **params):
        super().__init__(weight, **params)
        self.recent_days = params.get("recent_push_days", 30)
        self.active_days = params.get("active_push_days", 90)
        self.stale_days = params.get("stale_days", 180)

    def compute(self, repo: Repo) -> float:
        days = repo.activity.days_since_last_push
        if days is None:
            return 0.0
        if days <= self.recent_days:
            return 100.0
        if days <= self.active_days:
            return 70.0
        if days <= self.stale_days:
            return 40.0
        return 10.0
