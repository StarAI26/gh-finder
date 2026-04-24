"""Trust scorer — verifies real usage via GitHub Code Search API.

Measures whether a repo is actually referenced in other codebases,
not just popular. This is the "Truth Layer" that distinguishes
"boring but used" from "viral but unused".

Trust Score = normalize(mentions) * 70 + normalize(stars) * 30

Search targets: requirements.txt, package.json, go.mod, Cargo.toml, README
"""

from __future__ import annotations

import json
import math
import time
import urllib.request
import urllib.error
import urllib.parse
import os
from typing import Optional

from src.core.models import Repo
from src.core.interfaces import BaseScorer


# Dependency file patterns to search for repo mentions
MENTION_FILE_PATTERNS = [
    "filename:requirements.txt",
    "filename:pyproject.toml",
    "filename:package.json",
    "filename:go.mod",
    "filename:Cargo.toml",
    "filename:pom.xml",
    "path:README",
]

# Normalization caps
MAX_MENTIONS_FOR_SCALE = 1000  # 1000+ mentions = full score
MAX_STARS_FOR_SCALE = 100000   # 100k+ stars = full star score


class TrustScorer(BaseScorer):
    """Project trust via actual codebase mentions.

    Components (total = 100 points):
      - Mention count (0-70): log-scaled code search results
      - Star popularity (0-30): log-scaled stars as secondary signal

    If rate limit hit or API error: returns 50 (neutral fallback).
    """

    name = "trust"
    default_weight = 0.15

    def __init__(self, weight=None, **params):
        super().__init__(weight, **params)
        self.max_mentions = params.get("max_mentions_for_scale", MAX_MENTIONS_FOR_SCALE)
        self.max_stars = params.get("max_stars_for_scale", MAX_STARS_FOR_SCALE)
        self.timeout = params.get("timeout", 10)
        self._token = os.environ.get("GITHUB_TOKEN")
        # Cache within scorer instance to avoid redundant API calls
        self._mention_cache: dict[str, int] = {}

    def compute(self, repo: Repo) -> float:
        mentions = self._count_mentions(repo)

        # Mark soft_post_suspicion: high stars but zero mentions
        if repo.metrics.stars > 1000 and mentions == 0:
            repo.soft_post_suspicion = True

        return self._calculate_score(mentions, repo.metrics.stars)

    def _count_mentions(self, repo: Repo) -> int:
        """Count how many other repos reference this one."""
        full_name = repo.full_name  # e.g. "owner/repo"
        repo_name = repo.full_name.split("/")[-1] if "/" in repo.full_name else repo.full_name

        # Check instance cache first
        cache_key = f"{full_name}:{repo_name}"
        if cache_key in self._mention_cache:
            return self._mention_cache[cache_key]

        total = 0
        search_queries = []

        # Build search queries for each file pattern
        for pattern in MENTION_FILE_PATTERNS:
            # Search for full "owner/repo" reference (most precise)
            search_queries.append(f'"{full_name}" {pattern}')
            # Search for repo name alone (catches informal references)
            if repo_name != full_name:
                search_queries.append(f'"{repo_name}" {pattern}')

        for query in search_queries:
            try:
                count = self._search_code(query)
                total += count
            except Exception:
                # API error — continue with partial count
                pass

            # Rate limit pacing (0.2s between Code Search requests)
            time.sleep(0.2)

        self._mention_cache[cache_key] = total
        return total

    def _search_code(self, query: str) -> int:
        """Execute a GitHub Code Search query, return total_count."""
        url = f"https://api.github.com/search/code?q={urllib.parse.quote(query)}&per_page=1"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "github-reference-finder/3.0",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("total_count", 0)
        except urllib.error.HTTPError as e:
            if e.code == 403:
                # Rate limit exceeded
                raise
            if e.code == 422:
                # Invalid query (e.g. special chars)
                return 0
            return 0
        except Exception:
            return 0

    def _calculate_score(self, mentions: int, stars: int) -> float:
        """Calculate trust score from mentions and stars."""
        # Mention score: log-scaled (0-70)
        if self.max_mentions > 0:
            mention_score = (
                self._log_scale(mentions, self.max_mentions) * 70
            )
        else:
            mention_score = 0.0

        # Star score: log-scaled (0-30) — secondary signal
        if self.max_stars > 0:
            star_score = (
                self._log_scale(stars, self.max_stars) * 30
            )
        else:
            star_score = 0.0

        return min(mention_score + star_score, 100.0)

    @staticmethod
    def _log_scale(value: int, max_val: float) -> float:
        """Log-scale a value to [0, 1] range."""
        if max_val <= 0:
            return 0.0
        return math.log1p(value) / math.log1p(max_val)

    def compute_fallback(self, repo: Repo) -> float:
        """Return neutral score when API is unavailable.

        Use this during rate limit exhaustion to avoid blocking the pipeline.
        """
        return 50.0  # Neutral midpoint
