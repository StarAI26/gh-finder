"""Scorer registry — auto-discovers and manages scoring plugins."""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import traceback
from pathlib import Path
from typing import Optional

from src.core.models import Repo, ScoringConfig
from src.core.interfaces import BaseScorer, BaseScorerRegistry

logger = logging.getLogger("scorer_registry")


class ScorerRegistry(BaseScorerRegistry):
    """Plugin registry for scoring dimensions.

    Scorers are either:
      1. Registered manually via register()
      2. Auto-discovered from the scorers/ directory via register_auto()

    Composite scoring weights are loaded from ScoringConfig.
    """

    def __init__(self, scoring_config: Optional[ScoringConfig] = None):
        self._scorers: dict[str, BaseScorer] = {}
        self._config = scoring_config

    def register(self, scorer: BaseScorer) -> None:
        """Add a scorer to the registry."""
        self._scorers[scorer.name] = scorer

    def register_auto(self, directory: str) -> int:
        """Auto-discover and register scorers from a directory.

        Scans for *.py files (except __init__.py and registry.py),
        imports each, and registers any BaseScorer subclass found.
        """
        count = 0
        scorers_dir = Path(directory)
        if not scorers_dir.is_dir():
            return 0

        for py_file in sorted(scorers_dir.glob("*.py")):
            if py_file.name in ("__init__.py", "registry.py"):
                continue
            module_name = py_file.stem
            try:
                # Dynamic import
                spec = importlib.util.spec_from_file_location(
                    f"scorers.{module_name}", py_file
                )
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)

                    # Find BaseScorer subclasses
                    for attr_name in dir(mod):
                        attr = getattr(mod, attr_name)
                        if (
                            isinstance(attr, type)
                            and issubclass(attr, BaseScorer)
                            and attr is not BaseScorer
                            and attr.name != "base"
                        ):
                            scorer = attr()
                            self._apply_config(scorer)
                            self.register(scorer)
                            count += 1
            except Exception:
                logger.warning(f"[SCORER] Failed to load {py_file.name}:\n{traceback.format_exc()}")

        return count

    def compute_all(self, repo: Repo) -> dict[str, float]:
        """Run all registered scorers, return {name: score}."""
        result = {}
        for name, scorer in self._scorers.items():
            try:
                result[name] = scorer.compute(repo)
            except Exception:
                result[name] = 0.0
        return result

    def composite_score(self, breakdown: dict[str, float]) -> float:
        """Weighted composite from breakdown scores (0-100 scale)."""
        if not breakdown:
            return 0.0
        total = 0.0
        weight_sum = 0.0
        for name, score in breakdown.items():
            scorer = self._scorers.get(name)
            if scorer and scorer.weight > 0:
                total += score * scorer.weight
                weight_sum += scorer.weight
        return total / weight_sum if weight_sum > 0 else 0.0

    def list_scorers(self) -> list[dict]:
        """Return list of registered scorers with weights."""
        return [
            {"name": s.name, "weight": s.weight, "type": type(s).__name__}
            for s in self._scorers.values()
        ]

    @property
    def scorers(self) -> dict[str, BaseScorer]:
        return dict(self._scorers)

    def _apply_config(self, scorer: BaseScorer) -> None:
        """Override scorer weight from config if available."""
        if self._config and scorer.name in self._config.dimensions:
            dim = self._config.dimensions[scorer.name]
            if dim.enabled:
                scorer.weight = dim.weight
                scorer.params.update(dim.params)
