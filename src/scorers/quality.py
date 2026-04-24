"""Quality scorer — license, issue hygiene, archived status.

Extracted from the old health scorer. Measures project health signals
that indicate quality and safety, not popularity.
"""

from __future__ import annotations

from src.core.models import Repo
from src.core.interfaces import BaseScorer


class QualityScorer(BaseScorer):
    """Project quality from license, issue hygiene, and archived status.

    Components (total = 100 points):
      - License quality (0-40): MIT/Apache→40, BSD→36, GPL→32, MPL→28, other→12, no→8
      - Issue hygiene (0-40): ratio-based penalty
      - Archived (0-20): not archived→20, archived→0
    """

    name = "quality"
    default_weight = 0.25

    LICENSE_SCORES = {
        "MIT": 1.0, "mit": 1.0,
        "Apache-2.0": 1.0, "apache-2.0": 1.0,
        "BSD-2-Clause": 0.9, "bsd-2-clause": 0.9,
        "BSD-3-Clause": 0.9, "bsd-3-clause": 0.9,
        "GPL-3.0": 0.8, "gpl-3.0": 0.8,
        "MPL-2.0": 0.7, "mpl-2.0": 0.7,
        "Unlicense": 0.6,
        "NOASSERTION": 0.3,
    }

    def __init__(self, weight=None, **params):
        super().__init__(weight, **params)
        self.issue_ratio_threshold = params.get("issue_ratio_threshold", 0.5)
        self.min_issues_for_penalty = params.get("min_issues_for_penalty", 50)

    def compute(self, repo: Repo) -> float:
        score = 0.0
        score += self._score_license(repo)       # 0-40
        score += self._score_issue_hygiene(repo) # 0-40
        score += self._score_archived(repo)      # 0-20
        return min(score, 100.0)

    def _score_license(self, repo: Repo) -> float:
        key = repo.metrics.license_key
        if not key:
            return 8.0  # No license = low but not zero
        weight = self.LICENSE_SCORES.get(key, 0.3)
        return weight * 40

    def _score_issue_hygiene(self, repo: Repo) -> float:
        issues = repo.metrics.open_issues
        stars = repo.metrics.stars
        if stars == 0:
            return 40  # Neutral for zero-star repos
        ratio = issues / stars
        if issues < self.min_issues_for_penalty:
            return 40
        if ratio <= 0.1:
            return 40
        if ratio <= self.issue_ratio_threshold:
            return 25
        if ratio <= 1.0:
            return 12
        return 0

    def _score_archived(self, repo: Repo) -> float:
        if repo.metrics.is_archived:
            return 0
        return 20
