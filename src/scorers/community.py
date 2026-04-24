"""Community scorer — log-scaled popularity metrics with fork/star ratio check.

Extracted from the old health scorer. Measures community size and engagement
without letting mega-repos dominate mid-tier repos.

Fork/star ratio check: a very low ratio (< 0.03) suggests "watch-only"
popularity (potential marketing noise) and applies a penalty.
"""

from __future__ import annotations

import math

from src.core.models import Repo
from src.core.interfaces import BaseScorer


class CommunityScorer(BaseScorer):
    """Community size from stars, forks, watchers.

    Log-scaled to prevent mega-repos from dominating.
    Total = 100 points:
      - stars: log-scaled (0-50)
      - forks: log-scaled (0-30)
      - watchers: log-scaled (0-20)

    Fork/star ratio penalty:
      - ratio >= 0.1: no penalty (normal engagement)
      - ratio 0.03-0.1: -10% penalty (low engagement)
      - ratio < 0.03: -25% penalty (suspicious — may be marketing noise)
    """

    name = "community"
    default_weight = 0.10

    def __init__(self, weight=None, **params):
        super().__init__(weight, **params)
        self.max_stars = params.get("max_stars_for_scale", 500000)
        self.max_forks = params.get("max_forks_for_scale", 100000)
        self.max_watchers = params.get("max_watchers_for_scale", 50000)

    def compute(self, repo: Repo) -> float:
        stars_score = self._log_scale(repo.metrics.stars, self.max_stars) * 50
        forks_score = self._log_scale(repo.metrics.forks, self.max_forks) * 30
        watchers_score = self._log_scale(repo.metrics.watchers, self.max_watchers) * 20

        raw_score = stars_score + forks_score + watchers_score

        # Apply fork/star ratio penalty
        ratio_penalty = self._fork_star_ratio_penalty(repo)
        return min(raw_score * ratio_penalty, 100.0)

    @staticmethod
    def _fork_star_ratio_penalty(repo: Repo) -> float:
        """Apply penalty for suspiciously low fork/star ratios.

        Normal fork/star ratio: 0.1-0.3
        Low ratio (< 0.03): suggests stars from passive watching, not active use
        """
        stars = repo.metrics.stars
        forks = repo.metrics.forks
        if stars < 100:
            return 1.0  # Too few stars for ratio to be meaningful

        ratio = forks / stars if stars > 0 else 0

        if ratio >= 0.1:
            return 1.0  # Normal engagement
        if ratio >= 0.03:
            return 0.9  # Mild penalty (-10%)
        return 0.75  # Strong penalty (-25%)

    @staticmethod
    def _log_scale(value: int, max_val: float) -> float:
        if max_val <= 0:
            return 0.0
        return math.log1p(value) / math.log1p(max_val)
