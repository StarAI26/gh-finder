"""Niche boost scorer — protects small but high-quality projects in specialized domains.

Without this scorer, niche projects (low stars, high actual usage) get drowned
out by viral marketing projects. This scorer applies a bonus to repos whose
star count is respectable for their domain but would score low on absolute stars.

Bonus = up to 10 points, based on domain-aware star normalization.
"""

from __future__ import annotations

from src.core.models import Repo
from src.core.interfaces import BaseScorer


class NicheBoostScorer(BaseScorer):
    """Domain-aware boost for niche but high-quality projects.

    Scoring logic:
      - Load domain-specific min_stars and max_stars from domain_rules
      - Normalize repo stars against the domain's expected range
      - If stars are in the "sweet spot" (above min but below 30% of max),
        apply a bonus (the project is well-regarded in its niche)
      - If stars are very high (above max), no bonus needed (already scores well)
      - If stars are below min, small bonus for being active in a niche

    Components (total = 100 points, but used as additive bonus):
      - Domain fit bonus (0-60): based on star position in domain range
      - Activity bonus (0-40): recent push + issues being managed
    """

    name = "niche_boost"
    default_weight = 0.10

    # Default domain ranges (stars)
    DEFAULT_MIN_STARS = 50
    DEFAULT_MAX_STARS = 10000
    # Sweet spot: below 30% of max means "respected in niche but not mainstream"
    SWEET_SPOT_RATIO = 0.30
    MAX_BONUS = 10.0  # Maximum bonus points added to final score

    def __init__(self, weight=None, **params):
        super().__init__(weight, **params)
        self.min_stars = params.get("min_stars", self.DEFAULT_MIN_STARS)
        self.max_stars = params.get("max_stars", self.DEFAULT_MAX_STARS)
        # Domain rules loaded at runtime via set_domain_config
        self._domain_config: dict = {}

    def set_domain_config(self, config: dict) -> None:
        """Set domain-specific star thresholds."""
        self._domain_config = config

    def compute(self, repo: Repo) -> float:
        stars = repo.metrics.stars
        domain = self._domain_config.get("domain", "general")
        domain_rules = self._domain_config.get("rules", {})

        # Get domain-specific thresholds
        domain_rule = domain_rules.get(domain, {})
        min_stars = domain_rule.get("min_stars", self.min_stars)
        max_stars = domain_rule.get("max_stars", self.max_stars)

        # Calculate position in domain star range
        if max_stars <= min_stars:
            return 50.0  # Neutral if range is invalid

        position = (stars - min_stars) / (max_stars - min_stars)
        position = max(0.0, min(1.0, position))  # Clamp to [0, 1]

        # Bonus calculation:
        # - Below min_stars: small bonus (2-5) for being active in niche
        # - Between min_stars and sweet_spot: high bonus (5-10) — "respected niche project"
        # - Above sweet_spot: decreasing bonus (10→0) — mainstream, doesn't need boost
        sweet_spot = self.SWEET_SPOT_RATIO

        if stars < min_stars:
            # Very niche — small bonus if active
            bonus = 2.0
            if repo.activity.has_recent_commits:
                bonus += 3.0
            return bonus

        if position <= sweet_spot:
            # Sweet spot: niche but respected
            # Linear scale from 5 to 10 as position goes from 0 to sweet_spot
            ratio = position / sweet_spot
            bonus = 5.0 + ratio * 5.0
            # Activity bonus
            if repo.activity.has_recent_commits:
                bonus += 0.0  # Already high, activity confirmed by stars
            return bonus

        # Above sweet spot: decreasing bonus
        # At sweet_spot → 10, at 1.0 → 0
        above_ratio = (position - sweet_spot) / (1.0 - sweet_spot)
        bonus = 10.0 * (1.0 - above_ratio)
        return max(0.0, bonus)
