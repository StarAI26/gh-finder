"""Configuration loader — reads JSON configs, validates, provides typed access.

Pure stdlib: json + pathlib. No pydantic, no pyyaml.
All paths are relative to this file's location (src/config/).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.core.models import ScoringConfig

# Resolve config directory — works whether running as script or imported
_CONFIG_DIR = Path(__file__).resolve().parent


def _load_json(name: str) -> dict:
    """Load a JSON file from the config directory."""
    path = _CONFIG_DIR / name
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@dataclass
class RateLimitConfig:
    unauthenticated_per_min: int = 10
    authenticated_per_min: int = 30
    backoff_base: float = 2.0
    max_retries: int = 3
    request_timeout: int = 15


@dataclass
class CacheConfig:
    default_ttl: int = 3600
    error_ttl: int = 60
    etag_enabled: bool = True


@dataclass
class PoolConfig:
    expansion_factor: int = 3
    max_pool_size: int = 30
    trust_scorer_pool_ratio: int = 2  # trust scorer runs on top_n * this ratio


@dataclass
class ThresholdConfig:
    min_stars_default: int = 2
    min_stars_niche: int = 2
    min_stars_popular: int = 10
    max_results_per_query: int = 30
    max_queries_per_search: int = 12


@dataclass
class DomainRule:
    description: str = ""
    default_hints: list[str] = field(default_factory=list)
    default_excludes: list[str] = field(default_factory=list)
    query_templates: list[str] = field(default_factory=list)
    topic_boost: list[str] = field(default_factory=list)
    min_stars: int = 50
    max_stars: int = 100000


@dataclass
class Settings:
    """Root configuration object."""

    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    thresholds: ThresholdConfig = field(default_factory=ThresholdConfig)
    rate_limits: RateLimitConfig = field(default_factory=RateLimitConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    pool: PoolConfig = field(default_factory=PoolConfig)
    domain_rules: dict[str, DomainRule] = field(default_factory=dict)
    github_token: Optional[str] = None

    @classmethod
    def load(cls, config_dir: Optional[Path] = None) -> "Settings":
        """Load all configuration files."""
        weights = _load_json("weights.json") if config_dir is None else _load_json_from(config_dir, "weights.json")
        domain = _load_json("domain_rules.json") if config_dir is None else _load_json_from(config_dir, "domain_rules.json")

        scoring = ScoringConfig.from_dict(weights.get("scoring", {}))
        scoring.normalize_weights()

        thresholds_data = weights.get("thresholds", {})
        thresholds = ThresholdConfig(
            min_stars_default=thresholds_data.get("min_stars_default", 2),
            min_stars_niche=thresholds_data.get("min_stars_niche", 2),
            min_stars_popular=thresholds_data.get("min_stars_popular", 10),
            max_results_per_query=thresholds_data.get("max_results_per_query", 30),
            max_queries_per_search=thresholds_data.get("max_queries_per_search", 12),
        )

        rate_data = weights.get("rate_limits", {})
        rate_limits = RateLimitConfig(
            unauthenticated_per_min=rate_data.get("unauthenticated_per_min", 10),
            authenticated_per_min=rate_data.get("authenticated_per_min", 30),
            backoff_base=rate_data.get("backoff_base", 2.0),
            max_retries=rate_data.get("max_retries", 3),
            request_timeout=rate_data.get("request_timeout", 15),
        )

        cache_data = weights.get("cache", {})
        cache = CacheConfig(
            default_ttl=cache_data.get("default_ttl", 3600),
            error_ttl=cache_data.get("error_ttl", 60),
            etag_enabled=cache_data.get("etag_enabled", True),
        )

        pool_data = weights.get("pool", {})
        pool = PoolConfig(
            expansion_factor=pool_data.get("expansion_factor", 3),
            max_pool_size=pool_data.get("max_pool_size", 30),
            trust_scorer_pool_ratio=pool_data.get("trust_scorer_pool_ratio", 2),
        )

        domain_rules = {}
        for name, rule_data in domain.items():
            domain_rules[name] = DomainRule(
                description=rule_data.get("description", ""),
                default_hints=rule_data.get("default_hints", []),
                default_excludes=rule_data.get("default_excludes", []),
                query_templates=rule_data.get("query_templates", []),
                topic_boost=rule_data.get("topic_boost", []),
                min_stars=rule_data.get("min_stars", 50),
                max_stars=rule_data.get("max_stars", 100000),
            )

        return cls(
            scoring=scoring,
            thresholds=thresholds,
            rate_limits=rate_limits,
            cache=cache,
            pool=pool,
            domain_rules=domain_rules,
            github_token=os.environ.get("GITHUB_TOKEN"),
        )

    def get_domain_rule(self, domain: str) -> DomainRule:
        """Get rules for a domain, fallback to general."""
        return self.domain_rules.get(domain, self.domain_rules.get("general", DomainRule()))

    def get_min_stars(self, domain: str = "general") -> int:
        """Return min_stars threshold for a domain."""
        if domain in ("search_tool", "devops"):
            return self.thresholds.min_stars_niche
        return self.thresholds.min_stars_default


def _load_json_from(config_dir: Path, name: str) -> dict:
    path = config_dir / name
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
