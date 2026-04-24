"""Query refiner — analyzes first-round results, reads README deeply,
extracts meaningful signals, and generates refined queries.

Purpose: When initial search results don't match user intent,
deep-read the best repos' README to understand what they actually are,
then use that understanding to generate more precise queries.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Optional

from src.core.interfaces import BaseRefiner
from src.core.models import GitHubQuery, QueryStrategy, Repo, SearchContext
from src.config.settings import Settings

logger = logging.getLogger("query_refiner")

# Common English words and programming noise to ignore when extracting keywords
STOP_WORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "out", "off", "over",
    "under", "again", "further", "then", "once", "here", "there", "when",
    "where", "why", "how", "all", "each", "every", "both", "few", "more",
    "most", "other", "some", "such", "no", "nor", "not", "only", "own",
    "same", "so", "than", "too", "very", "just", "and", "but", "or",
    "if", "because", "until", "while", "about", "against", "this", "that",
    "these", "those", "it", "its", "it's", "what", "which", "who", "whom",
    "use", "using", "used", "make", "makes", "made", "get", "gets", "got",
    "one", "two", "first", "also", "new", "simple", "support", "supports",
    "supports", "provide", "provides", "includes", "include", "based",
    "like", "want", "need", "way", "many", "much", "your", "you", "we",
    "our", "they", "them", "their", "his", "her", "my", "me", "project",
    "example", "examples", "install", "installation", "usage", "quick",
    "start", "start", "start", "run", "running", "build", "building",
    "version", "release", "latest", "latest", "note", "notes", "see",
    "also", "however", "even", "still", "already", "back", "down",
}


class QueryRefiner(BaseRefiner):
    """Deep-reads README of top repos, extracts meaningful keywords, generates refined queries."""

    def __init__(self, settings: Optional[Settings] = None):
        self._settings = settings or Settings.load()

    def should_refine(self, repos: list[Repo], context: SearchContext) -> bool:
        """Decide whether refinement is worth trying.

        Returns True if any of:
        - Best repo's domain_fit score ≤ 55 (near-neutral = no domain match)
        - Best repo's semantic score ≤ 20 (little/no semantic extraction)
        - Average domain_fit of top 5 ≤ 55 (none matched well)
        """
        if not repos:
            return True

        top = repos[:min(5, len(repos))]
        domain_fits = [r.score_breakdown.get("domain_fit", 50) for r in top]
        semantics = [r.score_breakdown.get("semantic", 0) for r in top]

        best_domain_fit = max(domain_fits)
        best_semantic = max(semantics)
        avg_domain_fit = sum(domain_fits) / len(domain_fits)

        if best_domain_fit <= 55 or best_semantic <= 20 or avg_domain_fit <= 55:
            logger.info(
                f"[REFINER] best_domain_fit={best_domain_fit:.0f}, "
                f"best_semantic={best_semantic:.0f}, "
                f"avg_domain_fit={avg_domain_fit:.0f} → refine"
            )
            return True

        logger.info(
            f"[REFINER] best_domain_fit={best_domain_fit:.0f}, "
            f"avg_domain_fit={avg_domain_fit:.0f} → skip (results aligned)"
        )
        return False

    def refine(self, context: SearchContext, repos: list[Repo], limit: int = 5) -> list[GitHubQuery]:
        """Analyze repos, deep-read README, return refined queries."""
        top = repos[:limit]

        # Step 1: Deep-read README for repos that haven't been read yet
        self._deep_read(top)

        # Step 2: Extract meaningful signals from README
        readme_keywords = self._extract_keywords_from_readme(top)
        tech_signals = self._extract_tech_signals(top)
        result_types = self._extract_result_types(top)

        # Step 3: Generate refined queries
        queries: list[GitHubQuery] = []
        min_stars = self._settings.get_min_stars(context.domain)
        max_results = self._settings.thresholds.max_results_per_query
        base = context.raw_query.strip()

        # Strategy 1: Add top README-extracted keywords
        for kw in readme_keywords[:3]:
            queries.append(GitHubQuery(
                query_string=f"{base} {kw}",
                strategy=QueryStrategy.KEYWORD,
                min_stars=min_stars,
                max_results=max_results,
            ))

        # Strategy 2: Combine top keyword + tech signal
        if readme_keywords and tech_signals:
            queries.append(GitHubQuery(
                query_string=f"{base} {readme_keywords[0]} {tech_signals[0]}",
                strategy=QueryStrategy.KEYWORD,
                min_stars=min_stars,
                max_results=max_results,
            ))

        # Strategy 3: Result type focused
        if result_types:
            rt = result_types[0]
            if "framework" in rt or "library" in rt:
                queries.append(GitHubQuery(
                    query_string=f"{base} framework library",
                    strategy=QueryStrategy.KEYWORD,
                    min_stars=min_stars,
                    max_results=max_results,
                ))
            elif "cli" in rt or "command" in rt:
                queries.append(GitHubQuery(
                    query_string=f"{base} cli tool",
                    strategy=QueryStrategy.KEYWORD,
                    min_stars=min_stars,
                    max_results=max_results,
                ))

        # Strategy 4: Exclude misaligned types from context
        if context.excludes:
            exclude_str = " ".join(f"-{e}" for e in context.excludes[:3])
            queries.append(GitHubQuery(
                query_string=f"{base} {exclude_str}",
                strategy=QueryStrategy.KEYWORD,
                min_stars=min_stars,
                max_results=max_results,
            ))

        # Deduplicate
        seen: set[str] = set()
        deduped = []
        for q in queries:
            if q.query_string not in seen:
                seen.add(q.query_string)
                deduped.append(q)

        max_q = self._settings.thresholds.max_queries_per_search
        refined = deduped[:max_q]

        logger.info(f"[REFINER] Generated {len(refined)} refined queries:")
        for q in refined:
            logger.info(f"  → {q.query_string}")

        return refined

    def _deep_read(self, repos: list[Repo]) -> None:
        """Fetch README for repos that haven't been deep-read yet."""
        for repo in repos:
            # If semantics already has tech stack from README, skip
            if repo.semantics.tech_stack and len(repo.semantics.tech_stack) > 1:
                continue
            # Best-effort fetch
            repo._readme_text = self._fetch_readme_text(repo)

    def _fetch_readme_text(self, repo: Repo) -> Optional[str]:
        """Fetch README text (reuses the inspector's logic)."""
        try:
            import urllib.request
            import json
            import base64

            owner, name = repo.full_name.split("/", 1)
            url = f"https://api.github.com/repos/{owner}/{name}/readme"
            headers = {"Accept": "application/vnd.github.v3+json"}

            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content_b64 = data.get("content", "")
                raw = base64.b64decode(content_b64).decode("utf-8", errors="replace")
                # Strip markdown formatting for keyword extraction
                raw = re.sub(r"```[\s\S]*?```", "", raw)  # remove code blocks
                raw = re.sub(r"#[^\n]*", "", raw)  # remove headings
                raw = re.sub(r"!\[.*?\]\(.*?\)", "", raw)  # remove images
                raw = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", raw)  # inline links → text
                raw = re.sub(r"[*_`~]", "", raw)  # formatting chars
                return raw
        except Exception:
            return None

    def _extract_keywords_from_readme(self, repos: list[Repo]) -> list[str]:
        """Extract top meaningful keywords from README text."""
        all_text_parts = []
        for repo in repos:
            # Try README text first
            readme = getattr(repo, "_readme_text", None) or ""
            if readme:
                all_text_parts.append(readme[:3000])  # First 3000 chars
            # Fallback to description + purpose
            desc = repo.description or ""
            purpose = repo.semantics.purpose or ""
            if desc or purpose:
                all_text_parts.append(f"{desc} {purpose}")

        full_text = " ".join(all_text_parts).lower()
        if not full_text.strip():
            return []

        # Tokenize: split on non-alphanumeric, keep words with length >= 3
        tokens = re.findall(r"[a-z][a-z0-9]{2,}", full_text)
        # Filter stop words
        meaningful = [t for t in tokens if t not in STOP_WORDS]
        # Count frequency
        counter = Counter(meaningful)
        # Return top keywords
        return [word for word, _ in counter.most_common(10)]

    def _extract_tech_signals(self, repos: list[Repo]) -> list[str]:
        """Extract tech signals from repos (language + README tech mentions)."""
        signals = []
        for repo in repos:
            # Use already-detected tech stack
            signals.extend(repo.semantics.tech_stack)
            # Check README for additional tech mentions
            readme = getattr(repo, "_readme_text", None) or ""
            if readme:
                # Look for common tech mentions
                for tech in ["python", "javascript", "typescript", "rust", "go",
                             "java", "ruby", "php", "swift", "kotlin",
                             "react", "vue", "angular", "node", "npm", "yarn",
                             "webpack", "vite", "docker", "wasm", "sixel",
                             "unicode", "utf8", "ansi", "ncurses", "termbox",
                             "curses", "tput", "figlet", "toilet"]:
                    if re.search(rf"\b{tech}\b", readme, re.IGNORECASE):
                        signals.append(tech)
        # Deduplicate, return most common
        counter = Counter(s.lower() for s in signals)
        return [tech for tech, _ in counter.most_common(5)]

    def _extract_result_types(self, repos: list[Repo]) -> list[str]:
        """Extract what kind of deliverable these repos are."""
        types = []
        for repo in repos:
            if repo.semantics.result:
                types.append(repo.semantics.result.lower())
            readme = getattr(repo, "_readme_text", None) or ""
            if readme:
                if re.search(r"\bcli\b|\bcommand.?line\b", readme, re.IGNORECASE):
                    types.append("cli tool")
                if re.search(r"\blibrary\b|\bframework\b", readme, re.IGNORECASE):
                    types.append("library")
        counter = Counter(types)
        return [t for t, _ in counter.most_common(3)]
