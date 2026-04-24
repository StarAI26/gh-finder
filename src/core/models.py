"""Data models — all contracts for the search pipeline.

Pure stdlib: dataclasses + manual validation. No pydantic dependency.
Every component reads/writes through these models, never raw dicts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


# ─── Query Layer ───────────────────────────────────────────────────


class QueryStrategy(str, Enum):
    KEYWORD = "keyword"
    SEMANTIC = "semantic"
    MULTI_STRATEGY = "multi_strategy"


@dataclass
class GitHubQuery:
    """A single structured GitHub search query."""

    query_string: str
    strategy: QueryStrategy = QueryStrategy.KEYWORD
    min_stars: int = 2
    max_results: int = 30
    language: Optional[str] = None
    created_after: Optional[str] = None  # YYYY-MM-DD
    sort: str = "stars"
    order: str = "desc"

    def to_api_params(self) -> dict:
        """Convert to GitHub Search API query parameters."""
        params = {
            "q": self.query_string,
            "per_page": min(self.max_results, 100),
            "sort": self.sort,
            "order": self.order,
        }
        return params


@dataclass
class SearchContext:
    """The full search context — raw input + parsed intent + queries."""

    raw_query: str
    domain: str = "general"
    strategy: QueryStrategy = QueryStrategy.MULTI_STRATEGY
    hints: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)
    top_n: int = 5
    generated_queries: list[GitHubQuery] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "raw_query": self.raw_query,
            "domain": self.domain,
            "strategy": self.strategy.value,
            "hints": self.hints,
            "excludes": self.excludes,
            "top_n": self.top_n,
            "generated_queries": [
                {"query_string": q.query_string, "strategy": q.strategy.value}
                for q in self.generated_queries
            ],
        }


# ─── Repository Data ───────────────────────────────────────────────


@dataclass
class RepoMetrics:
    """Quantitative metrics extracted from GitHub API."""

    stars: int = 0
    forks: int = 0
    open_issues: int = 0
    watchers: int = 0
    size_kb: int = 0
    is_archived: bool = False
    is_fork: bool = False
    license_key: Optional[str] = None
    topics: list[str] = field(default_factory=list)


@dataclass
class RepoActivity:
    """Temporal activity signals."""

    pushed_at: Optional[str] = None  # ISO 8601 string
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    has_recent_commits: bool = False
    days_since_last_push: Optional[int] = None


@dataclass
class SemanticExtract:
    """Structured extraction from README / description."""

    purpose: str = ""
    result: str = ""
    audience: str = ""
    tech_stack: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class Repo:
    """Canonical repository representation throughout the pipeline."""

    full_name: str
    html_url: str
    description: Optional[str] = None

    metrics: RepoMetrics = field(default_factory=RepoMetrics)
    activity: RepoActivity = field(default_factory=RepoActivity)
    semantics: SemanticExtract = field(default_factory=SemanticExtract)

    # Scoring
    score_breakdown: dict[str, float] = field(default_factory=dict)
    composite_score: float = 0.0
    rank: int = 0

    # Trust / marketing noise detection
    soft_post_suspicion: bool = False  # High stars but zero code mentions

    # Metadata
    fetch_timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "full_name": self.full_name,
            "html_url": self.html_url,
            "description": self.description,
            "metrics": {
                "stars": self.metrics.stars,
                "forks": self.metrics.forks,
                "open_issues": self.metrics.open_issues,
                "watchers": self.metrics.watchers,
                "size_kb": self.metrics.size_kb,
                "is_archived": self.metrics.is_archived,
                "is_fork": self.metrics.is_fork,
                "license_key": self.metrics.license_key,
                "topics": self.metrics.topics,
            },
            "activity": {
                "pushed_at": self.activity.pushed_at,
                "created_at": self.activity.created_at,
                "updated_at": self.activity.updated_at,
                "has_recent_commits": self.activity.has_recent_commits,
                "days_since_last_push": self.activity.days_since_last_push,
            },
            "semantics": {
                "purpose": self.semantics.purpose,
                "result": self.semantics.result,
                "audience": self.semantics.audience,
                "tech_stack": self.semantics.tech_stack,
                "summary": self.semantics.summary,
            },
            "score_breakdown": self.score_breakdown,
            "composite_score": self.composite_score,
            "rank": self.rank,
            "soft_post_suspicion": self.soft_post_suspicion,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)


# ─── Scoring ───────────────────────────────────────────────────────


@dataclass
class ScorerConfig:
    """Configuration for a single scoring dimension."""

    name: str
    weight: float = 0.0
    enabled: bool = True
    params: dict = field(default_factory=dict)


@dataclass
class ScoringConfig:
    """Full scoring configuration."""

    dimensions: dict[str, ScorerConfig] = field(default_factory=dict)
    total_weight: float = 1.0

    def normalize_weights(self) -> None:
        """Normalize all weights to sum to 1.0."""
        enabled = {k: v for k, v in self.dimensions.items() if v.enabled}
        total = sum(v.weight for v in enabled.values())
        if total > 0:
            for dim in enabled.values():
                dim.weight /= total
            self.total_weight = 1.0

    @classmethod
    def from_dict(cls, data: dict) -> "ScoringConfig":
        dims = {}
        for name, cfg in data.get("dimensions", {}).items():
            dims[name] = ScorerConfig(
                name=name,
                weight=cfg.get("weight", 0.0),
                enabled=cfg.get("enabled", True),
                params=cfg.get("params", {}),
            )
        return cls(dimensions=dims)


# ─── Pipeline Results ──────────────────────────────────────────────


class PipelineStage(str, Enum):
    QUERY_BUILD = "query_build"
    FETCH = "fetch"
    SCORE = "score"
    INSPECT = "inspect"
    REFINE = "refine"
    REFETCH = "refetch"
    RANK = "rank"


@dataclass
class StageMetrics:
    """Timing and status for a pipeline stage."""

    stage: PipelineStage
    started_at: float
    completed_at: Optional[float] = None
    duration_ms: float = 0.0
    status: str = "pending"  # pending, running, completed, failed, skipped
    items_processed: int = 0
    error: Optional[str] = None


@dataclass
class SearchResult:
    """Final pipeline output."""

    context: SearchContext
    repos: list[Repo] = field(default_factory=list)
    stage_metrics: list[StageMetrics] = field(default_factory=list)
    total_repos_found: int = 0
    total_repos_scored: int = 0
    query_strings_executed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # Pool expansion: full candidate set vs. final top results
    pool_repos: list[Repo] = field(default_factory=list)  # All candidates before final cut
    pool_size: int = 0  # Size of the expanded candidate pool

    @property
    def top_repos(self) -> list[Repo]:
        """Return repos sorted by composite_score, respecting top_n."""
        sorted_repos = sorted(self.repos, key=lambda r: r.composite_score, reverse=True)
        return sorted_repos[: self.context.top_n]

    def to_markdown(self) -> str:
        """Render results as human-readable markdown."""
        lines = [f"## Search Results for `{self.context.raw_query}`\n"]
        lines.append(f"- Domain: {self.context.domain}")
        lines.append(f"- Strategy: {self.context.strategy.value}")
        lines.append(f"- Found: {self.total_repos_found} repos, "
                     f"Scored: {self.total_repos_scored}")
        lines.append("")

        for i, repo in enumerate(self.top_repos, 1):
            health = self._health_label(repo)
            lines.append(f"### {i}. {repo.full_name}")
            lines.append(f"- **Score**: {repo.composite_score:.1f}/100 | Health: {health}")
            lines.append(f"- **URL**: {repo.html_url}")
            if repo.description:
                lines.append(f"- **Description**: {repo.description}")
            if repo.semantics.purpose:
                lines.append(f"- **Purpose**: {repo.semantics.purpose}")
            if repo.semantics.tech_stack:
                lines.append(f"- **Tech**: {', '.join(repo.semantics.tech_stack[:3])}")
            if repo.score_breakdown:
                reasons = ", ".join(
                    f"{k}: {v:.0f}" for k, v in repo.score_breakdown.items() if v > 0
                )
                lines.append(f"- **Breakdown**: {reasons}")
            lines.append("")

        if self.errors:
            lines.append("### Errors\n")
            for err in self.errors:
                lines.append(f"- {err}")

        return "\n".join(lines)

    @staticmethod
    def _health_label(repo: Repo) -> str:
        if repo.metrics.is_archived:
            return "archived"
        if repo.composite_score >= 70:
            return "strong"
        if repo.composite_score >= 45:
            return "watch"
        if repo.composite_score >= 25:
            return "cooling"
        return "risky"
