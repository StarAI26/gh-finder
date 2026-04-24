"""Abstract base classes — the contracts every component must implement.

Pure stdlib. Sync (not async) since we use urllib.request.
New implementations subclass and override. Pipeline depends only
on these interfaces, never concrete types.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from .models import GitHubQuery, Repo, SearchContext, SearchResult


# ─── Data Acquisition ──────────────────────────────────────────────


class BaseQueryBuilder(ABC):
    """Converts user intent into structured GitHub search queries."""

    @abstractmethod
    def build(self, context: SearchContext) -> list[GitHubQuery]:
        """Generate one or more search queries from the context."""


class BaseFetcher(ABC):
    """Fetches repository data from GitHub (or any source)."""

    @abstractmethod
    def fetch(self, query: GitHubQuery) -> list[dict]:
        """Execute a single query, return raw API response items."""

    @abstractmethod
    def fetch_multi(self, queries: list[GitHubQuery]) -> list[dict]:
        """Execute multiple queries, deduplicate by full_name."""

    @abstractmethod
    def parse_repo(self, raw: dict) -> Repo:
        """Convert raw API dict into a validated Repo model."""


# ─── Scoring ───────────────────────────────────────────────────────


class BaseScorer(ABC):
    """A single scoring dimension. Plug into the registry."""

    name: str = "base"
    default_weight: float = 0.0

    def __init__(self, weight: Optional[float] = None, **params):
        self.weight = weight if weight is not None else self.default_weight
        self.params = params

    @abstractmethod
    def compute(self, repo: Repo) -> float:
        """Return a score in [0, 100] for this dimension."""

    def __repr__(self) -> str:
        return f"<Scorer {self.name} weight={self.weight:.2f}>"


class BaseScorerRegistry(ABC):
    """Manages scorer plugins and composite scoring."""

    @abstractmethod
    def register(self, scorer: BaseScorer) -> None:
        """Add a scorer to the registry."""

    @abstractmethod
    def register_auto(self, directory: str) -> int:
        """Auto-discover and register scorers from a directory."""

    @abstractmethod
    def compute_all(self, repo: Repo) -> dict[str, float]:
        """Run all registered scorers, return {name: score}."""

    @abstractmethod
    def composite_score(self, breakdown: dict[str, float]) -> float:
        """Weighted composite from breakdown scores."""


# ─── Semantic Analysis ─────────────────────────────────────────────


class BaseInspector(ABC):
    """Extracts structured meaning from repo README / description."""

    @abstractmethod
    def extract(self, repo: Repo) -> Repo:
        """Enrich the repo with semantic extraction results."""

    @abstractmethod
    def extract_batch(self, repos: list[Repo], limit: int = 5) -> list[Repo]:
        """Extract semantics for top-N repos (expensive operation)."""


# ─── Query Refinement ──────────────────────────────────────────────


class BaseRefiner(ABC):
    """Analyzes first-round results and generates refined queries."""

    @abstractmethod
    def refine(self, context: SearchContext, repos: list[Repo], limit: int = 5) -> list[GitHubQuery]:
        """Analyze repos, compare with context, return refined queries (empty = no refinement)."""

    @abstractmethod
    def should_refine(self, repos: list[Repo], context: SearchContext) -> bool:
        """Decide whether refinement is worth trying."""


# ─── Caching ───────────────────────────────────────────────────────


class BaseCache(ABC):
    """Pluggable cache backend."""

    @abstractmethod
    def get(self, key: str) -> Optional[dict]:
        """Return cached value or None."""

    @abstractmethod
    def set(self, key: str, value: dict, ttl: int = 3600) -> None:
        """Store value with TTL."""

    @abstractmethod
    def get_etag(self, url: str) -> Optional[str]:
        """Return cached ETag for conditional requests."""

    @abstractmethod
    def set_etag(self, url: str, etag: str) -> None:
        """Store ETag for conditional requests."""

    @abstractmethod
    def invalidate(self, pattern: str = "*") -> int:
        """Remove entries matching pattern. Return count removed."""


# ─── Pipeline ──────────────────────────────────────────────────────


class BasePipeline(ABC):
    """Orchestrates the full search workflow."""

    @abstractmethod
    def run(self, raw_query: str, **kwargs) -> SearchResult:
        """Execute the complete search pipeline."""

    @abstractmethod
    def run_partial(self, from_stage: str, context: SearchContext) -> SearchResult:
        """Resume pipeline from a specific stage (for retry/debug)."""
