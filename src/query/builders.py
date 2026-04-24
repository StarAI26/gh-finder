"""Query builders — strategy pattern for generating GitHub search queries.

Three strategies:
  KeywordBuilder: Direct keyword + hint expansion
  TemplateBuilder: Domain-specific query templates
  MultiStrategyBuilder: Combines all strategies, deduplicates

Each produces a list of GitHubQuery objects for the fetcher.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from src.core.interfaces import BaseQueryBuilder
from src.core.models import GitHubQuery, QueryStrategy, SearchContext
from src.config.settings import Settings, DomainRule


class KeywordBuilder(BaseQueryBuilder):
    """Simple keyword-based query expansion."""

    def __init__(self, settings: Optional[Settings] = None):
        self._settings = settings or Settings.load()

    def build(self, context: SearchContext) -> list[GitHubQuery]:
        queries: list[GitHubQuery] = []
        min_stars = self._settings.get_min_stars(context.domain)
        max_results = self._settings.thresholds.max_results_per_query

        # Base query with user hints
        base = context.raw_query.strip()

        # Direct query
        queries.append(GitHubQuery(
            query_string=base,
            strategy=QueryStrategy.KEYWORD,
            min_stars=min_stars,
            max_results=max_results,
        ))

        # Hint-augmented queries
        for hint in context.hints[:5]:
            queries.append(GitHubQuery(
                query_string=f"{base} {hint}",
                strategy=QueryStrategy.KEYWORD,
                min_stars=min_stars,
                max_results=max_results,
            ))

        # Exclude-filtered query
        if context.excludes:
            exclude_str = " ".join(f"-{e}" for e in context.excludes[:3])
            queries.append(GitHubQuery(
                query_string=f"{base} {exclude_str}",
                strategy=QueryStrategy.KEYWORD,
                min_stars=min_stars,
                max_results=max_results,
            ))

        return queries


class TemplateBuilder(BaseQueryBuilder):
    """Domain-template-based query generation."""

    def __init__(self, settings: Optional[Settings] = None):
        self._settings = settings or Settings.load()

    def build(self, context: SearchContext) -> list[GitHubQuery]:
        queries: list[GitHubQuery] = []
        rule = self._settings.get_domain_rule(context.domain)
        min_stars = self._settings.get_min_stars(context.domain)
        max_results = self._settings.thresholds.max_results_per_query

        if not rule.query_templates:
            return queries

        base = context.raw_query.strip()
        hints = context.hints or rule.default_hints or [""]

        for template in rule.query_templates:
            for hint in hints[:3]:
                query_str = template.replace("{query}", base).replace("{hint}", hint)
                queries.append(GitHubQuery(
                    query_string=query_str,
                    strategy=QueryStrategy.SEMANTIC,
                    min_stars=min_stars,
                    max_results=max_results,
                ))

        return queries


class MultiStrategyBuilder(BaseQueryBuilder):
    """Combines keyword and template strategies, deduplicates queries."""

    def __init__(self, settings: Optional[Settings] = None):
        self._settings = settings or Settings.load()

    def build(self, context: SearchContext) -> list[GitHubQuery]:
        all_queries: list[GitHubQuery] = []

        # Strategy 1: Keyword expansion
        keyword_builder = KeywordBuilder(self._settings)
        all_queries.extend(keyword_builder.build(context))

        # Strategy 2: Domain templates
        template_builder = TemplateBuilder(self._settings)
        all_queries.extend(template_builder.build(context))

        # Deduplicate by query_string
        seen: set[str] = set()
        deduped: list[GitHubQuery] = []
        for q in all_queries:
            if q.query_string not in seen:
                seen.add(q.query_string)
                deduped.append(q)

        # Cap at max queries
        max_q = self._settings.thresholds.max_queries_per_search
        return deduped[:max_q]
