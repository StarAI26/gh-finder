"""Search Pipeline — orchestrates the full search workflow.

Stages:
  1. Query Build   → MultiStrategyBuilder generates queries
  2. Fetch         → GitHubFetcher executes queries, deduplicates
  3. Score         → ScorerRegistry scores all repos (pre-inspect)
  4. Inspect       → ReadmeInspector extracts semantics for pool candidates
  5. Refine        → QueryRefiner analyzes results, generates refined queries
  6. Re-Fetch      → Execute refined queries, merge with existing repos
  7. Re-Score      → Score merged repos (includes trust + niche_boost)
  8. Rank          → Sort by composite score, assign ranks

Pool expansion: Instead of inspecting only top_n repos, the pipeline
expands the candidate pool to top_n * expansion_factor. This ensures
niche projects with low stars but high real usage (trust signal) get
a chance to be properly evaluated.

Each stage is independent, with timing metrics and error handling.
Stages can degrade gracefully.
"""

from __future__ import annotations

import os
import logging
import time
from typing import Optional

from src.core.models import (
    GitHubQuery, PipelineStage, Repo, SearchContext, SearchResult, StageMetrics
)
from src.core.interfaces import (
    BasePipeline, BaseQueryBuilder, BaseFetcher, BaseScorerRegistry,
    BaseInspector, BaseRefiner,
)
from src.config.settings import Settings
from src.query.builders import MultiStrategyBuilder
from src.fetchers.github import GitHubFetcher
from src.fetchers.cache import FileCache
from src.scorers.registry import ScorerRegistry
from src.scorers.trust import TrustScorer
from src.scorers.niche_boost import NicheBoostScorer
from src.inspectors.readme_parser import ReadmeInspector
from src.refiners.query_refiner import QueryRefiner

logger = logging.getLogger("search_pipeline")

# Resolve project root for absolute path references
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class SearchPipeline(BasePipeline):
    """Production search pipeline with pool expansion and trust scoring."""

    def __init__(
        self,
        query_builder: Optional[BaseQueryBuilder] = None,
        fetcher: Optional[BaseFetcher] = None,
        scorer_registry: Optional[BaseScorerRegistry] = None,
        inspector: Optional[BaseInspector] = None,
        refiner: Optional[BaseRefiner] = None,
        settings: Optional[Settings] = None,
    ):
        self._settings = settings or Settings.load()
        self._query_builder = query_builder or MultiStrategyBuilder(self._settings)

        if fetcher is None:
            cache_dir = os.path.join(_PROJECT_ROOT, "cache")
            cache = FileCache(
                cache_dir=cache_dir,
                default_ttl=self._settings.cache.default_ttl,
            )
            fetcher = GitHubFetcher(
                cache=cache,
                token=self._settings.github_token,
                max_retries=self._settings.rate_limits.max_retries,
                backoff_base=self._settings.rate_limits.backoff_base,
                timeout=self._settings.rate_limits.request_timeout,
            )
        self._fetcher = fetcher

        if scorer_registry is None:
            scorers_dir = os.path.join(_PROJECT_ROOT, "src/scorers")
            scorer_registry = ScorerRegistry(self._settings.scoring)
            scorer_registry.register_auto(scorers_dir)
        self._scorer_registry = scorer_registry

        # Special scorers (not auto-discovered, configured manually)
        self._trust_scorer: Optional[TrustScorer] = None
        self._niche_scorer: Optional[NicheBoostScorer] = None
        self._init_special_scorers()

        if inspector is None:
            inspector = ReadmeInspector(token=self._settings.github_token)
        self._inspector = inspector

        if refiner is None:
            refiner = QueryRefiner(self._settings)
        self._refiner = refiner

    def _init_special_scorers(self) -> None:
        """Initialize trust and niche boost scorers with config."""
        trust_cfg = self._settings.scoring.dimensions.get("trust")
        if trust_cfg and trust_cfg.enabled:
            self._trust_scorer = TrustScorer(
                weight=trust_cfg.weight,
                **trust_cfg.params,
            )

        niche_cfg = self._settings.scoring.dimensions.get("niche_boost")
        if niche_cfg and niche_cfg.enabled:
            self._niche_scorer = NicheBoostScorer(
                weight=niche_cfg.weight,
                **niche_cfg.params,
            )

    def run(self, raw_query: str, **kwargs) -> SearchResult:
        """Execute the complete search pipeline.

        Args:
            raw_query: User's search query
            **kwargs: Optional overrides (domain, hints, excludes, top_n, pool_size)

        Returns:
            SearchResult with ranked repos and stage metrics
        """
        # Build context
        domain = kwargs.get("domain", "general")
        hints = kwargs.get("hints") or self._settings.get_domain_rule(domain).default_hints
        excludes = kwargs.get("excludes") or self._settings.get_domain_rule(domain).default_excludes
        top_n = kwargs.get("top_n", 5)

        # Pool expansion: calculate pool size
        pool_expansion = self._settings.pool.expansion_factor
        max_pool = self._settings.pool.max_pool_size
        pool_size = min(top_n * pool_expansion, max_pool)
        # Override from kwargs if provided
        pool_size = kwargs.get("pool_size", pool_size)

        context = SearchContext(
            raw_query=raw_query,
            domain=domain,
            hints=hints,
            excludes=excludes,
            top_n=top_n,
        )

        result = SearchResult(context=context, pool_size=pool_size)

        try:
            # ── Stage 1: Query Build ──────────────────────────────
            queries = self._run_stage(
                PipelineStage.QUERY_BUILD,
                lambda: self._query_builder.build(context),
                result,
            )
            context.generated_queries = queries
            result.query_strings_executed = [q.query_string for q in queries]
            logger.info(f"[QUERY_BUILD] Generated {len(queries)} queries")

            if not queries:
                result.errors.append("No queries generated")
                return result

            # ── Stage 2: Fetch ────────────────────────────────────
            raw_repos = self._run_stage(
                PipelineStage.FETCH,
                lambda: self._fetcher.fetch_multi(queries),
                result,
            )
            result.total_repos_found = len(raw_repos)
            logger.info(f"[FETCH] Retrieved {len(raw_repos)} unique repos")

            if not raw_repos:
                result.errors.append("No repos found for any query")
                return result

            # Parse raw data into Repo models
            repos = []
            for raw in raw_repos:
                try:
                    repo = self._fetcher.parse_repo(raw)
                    repos.append(repo)
                except Exception as e:
                    logger.warning(f"Failed to parse repo: {e}")

            # ── Stage 3: Score (pre-inspect) ──────────────────────
            self._inject_domain_keywords(domain)
            scored_repos = self._run_stage(
                PipelineStage.SCORE,
                lambda: self._score_repos(repos),
                result,
            )
            result.total_repos_scored = len(scored_repos)
            logger.info(f"[SCORE] Scored {len(scored_repos)} repos")

            # ── Stage 4: Inspect ──────────────────────────────────
            # KEY CHANGE: Inspect the entire pool, not just top_n
            sorted_repos = sorted(scored_repos, key=lambda r: r.composite_score, reverse=True)
            inspect_limit = min(pool_size, len(sorted_repos))
            inspected = self._run_stage(
                PipelineStage.INSPECT,
                lambda: self._inspector.extract_batch(sorted_repos, limit=inspect_limit),
                result,
            )
            logger.info(f"[INSPECT] Extracted semantics for {inspect_limit} pool candidates")

            # ── Stage 5: Refine ───────────────────────────────────
            needs_refine = self._run_stage(
                PipelineStage.REFINE,
                lambda: self._refiner.should_refine(inspected, context),
                result,
            )

            if needs_refine:
                refined_queries = self._run_stage(
                    PipelineStage.REFINE,
                    lambda: self._refiner.refine(context, inspected, limit=context.top_n),
                    result,
                )

                if refined_queries:
                    logger.info(f"[REFINE] Generated {len(refined_queries)} refined queries")

                    # ── Stage 6: Re-Fetch ─────────────────────────
                    new_raw = self._run_stage(
                        PipelineStage.REFETCH,
                        lambda: self._fetcher.fetch_multi(refined_queries),
                        result,
                    )

                    # Merge: deduplicate by full_name, prefer existing data
                    existing_names = {r.full_name for r in repos}
                    for raw in new_raw:
                        full_name = raw.get("full_name", "")
                        if full_name and full_name not in existing_names:
                            try:
                                repo = self._fetcher.parse_repo(raw)
                                repos.append(repo)
                                existing_names.add(full_name)
                            except Exception as e:
                                logger.warning(f"Failed to parse refined repo: {e}")

                    # Update total count
                    result.total_repos_found = len(repos)
                    result.query_strings_executed.extend(
                        [q.query_string for q in refined_queries]
                    )
                    logger.info(f"[REFETCH] Merged {len(new_raw)} new repos, total: {len(repos)}")

            # ── Stage 7: Re-Score (includes trust + niche_boost) ─
            # Inspect new repos from refine, then re-score all
            new_repo_count = len(repos) - result.total_repos_scored
            if new_repo_count > 0:
                # Sort to find un-inspected repos, inspect them
                sorted_all = sorted(repos, key=lambda r: r.composite_score, reverse=True)
                uninspected = [r for r in sorted_all if not r.semantics.purpose]
                if uninspected:
                    self._inspector.extract_batch(uninspected, limit=pool_size)

            # Apply trust and niche_boost scorers to pool
            final_repos = self._run_stage(
                PipelineStage.SCORE,
                lambda: self._score_repos_with_special(repos, domain, top_n),
                result,
            )
            result.total_repos_scored = len(final_repos)
            logger.info(f"[RE-SCORE] Scored {len(final_repos)} repos (with trust + niche)")

            # ── Stage 8: Rank ─────────────────────────────────────
            final_ranked = self._run_stage(
                PipelineStage.RANK,
                lambda: self._rank_repos(final_repos),
                result,
            )

            # Store full pool + top results
            result.pool_repos = final_ranked  # Full ranked pool
            result.repos = final_ranked[:top_n]  # Final top_n for output
            result.pool_size = len(final_ranked)
            logger.info(f"[RANK] Final ranking complete, pool={len(final_ranked)}, returning top {top_n}")

        except Exception as e:
            result.errors.append(f"Pipeline error: {str(e)}")
            logger.error(f"Pipeline failed: {e}")

        return result

    def run_partial(self, from_stage: str, context: SearchContext) -> SearchResult:
        """Resume pipeline from a specific stage (for retry/debug)."""
        raise NotImplementedError("Partial pipeline resume not yet implemented")

    # ─── Stage Implementations ───────────────────────────────────

    def _inject_domain_keywords(self, domain: str) -> None:
        """Inject domain-specific keywords into the domain_fit scorer."""
        rule = self._settings.get_domain_rule(domain)
        keywords = rule.default_hints + rule.topic_boost
        domain_scorer = self._scorer_registry.scorers.get("domain_fit")
        if domain_scorer and hasattr(domain_scorer, "set_keywords"):
            domain_scorer.set_keywords(list(set(keywords)))

        # Configure niche boost scorer with domain rules
        if self._niche_scorer:
            domain_rules = {}
            for name, rule in self._settings.domain_rules.items():
                domain_rules[name] = {
                    "min_stars": rule.min_stars,
                    "max_stars": rule.max_stars,
                }
            self._niche_scorer.set_domain_config({
                "domain": domain,
                "rules": domain_rules,
            })

    def _score_repos(self, repos: list[Repo]) -> list[Repo]:
        """Score all repos using the standard registry."""
        for repo in repos:
            breakdown = self._scorer_registry.compute_all(repo)
            repo.score_breakdown = breakdown
            repo.composite_score = self._scorer_registry.composite_score(breakdown)
        return repos

    def _score_repos_with_special(self, repos: list[Repo], domain: str, top_n: int = 5) -> list[Repo]:
        """Score repos including trust and niche_boost special scorers."""
        # Trust scorer: run on top N*2 candidates to limit API cost
        trust_limit = min(top_n * self._settings.pool.trust_scorer_pool_ratio, len(repos))
        sorted_check = sorted(repos, key=lambda r: r.composite_score, reverse=True)
        sorted_names = {r.full_name for r in sorted_check[:trust_limit]}

        for repo in repos:
            breakdown = self._scorer_registry.compute_all(repo)

            # Trust scorer: only on top candidates
            if self._trust_scorer and repo.full_name in sorted_names:
                try:
                    trust_score = self._trust_scorer.compute(repo)
                except Exception:
                    trust_score = self._trust_scorer.compute_fallback(repo)
                breakdown["trust"] = trust_score

            # Niche boost scorer
            if self._niche_scorer:
                try:
                    niche_score = self._niche_scorer.compute(repo)
                except Exception:
                    niche_score = 50.0
                breakdown["niche_boost"] = niche_score

            repo.score_breakdown = breakdown
            repo.composite_score = self._scorer_registry.composite_score(breakdown)
        return repos

    def _rank_repos(self, repos: list[Repo]) -> list[Repo]:
        """Assign ranks based on composite score."""
        sorted_repos = sorted(repos, key=lambda r: r.composite_score, reverse=True)
        for i, repo in enumerate(sorted_repos, 1):
            repo.rank = i
        return sorted_repos

    # ─── Stage Runner ──────────────────────────────────────────

    def _run_stage(self, stage: PipelineStage, func, result: SearchResult):
        """Execute a pipeline stage with timing and error tracking."""
        metrics = StageMetrics(
            stage=stage,
            started_at=time.time(),
            status="running",
        )
        result.stage_metrics.append(metrics)

        try:
            output = func()
            metrics.completed_at = time.time()
            metrics.duration_ms = (metrics.completed_at - metrics.started_at) * 1000
            metrics.status = "completed"
            if isinstance(output, list):
                metrics.items_processed = len(output)
            return output
        except Exception as e:
            metrics.completed_at = time.time()
            metrics.duration_ms = (metrics.completed_at - metrics.started_at) * 1000
            metrics.status = "failed"
            metrics.error = str(e)
            result.errors.append(f"[{stage.value}] {e}")
            return []


def create_pipeline(
    settings: Optional[Settings] = None,
    query_builder: Optional[BaseQueryBuilder] = None,
    fetcher: Optional[BaseFetcher] = None,
    scorer_registry: Optional[BaseScorerRegistry] = None,
    inspector: Optional[BaseInspector] = None,
    refiner: Optional[BaseRefiner] = None,
) -> SearchPipeline:
    """Factory function for creating a search pipeline."""
    return SearchPipeline(
        query_builder=query_builder,
        fetcher=fetcher,
        scorer_registry=scorer_registry,
        inspector=inspector,
        refiner=refiner,
        settings=settings,
    )
