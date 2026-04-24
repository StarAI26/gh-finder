"""GitHub API fetcher — HTTP client with retry, backoff, rate-limit awareness, ETag.

Pure stdlib: urllib.request, json, time.
Replaces old api.py with proper separation of concerns:
  - Query building → query/builders.py
  - HTTP transport → this file
  - Caching → fetchers/cache.py
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

from src.core.interfaces import BaseFetcher, BaseCache
from src.core.models import GitHubQuery, Repo, RepoMetrics, RepoActivity, SemanticExtract


class GitHubAPIError(Exception):
    """Base exception for GitHub API failures."""
    def __init__(self, message: str, status_code: Optional[int] = None, retry_after: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class RateLimitExceeded(GitHubAPIError):
    """GitHub API rate limit hit."""


class FetcherError(Exception):
    """Non-recoverable fetcher error (parse failure, network error)."""


class GitHubFetcher(BaseFetcher):
    """Production-grade GitHub Search API client."""

    BASE_URL = "https://api.github.com"
    SEARCH_ENDPOINT = f"{BASE_URL}/search/repositories"

    def __init__(
        self,
        cache: BaseCache,
        token: Optional[str] = None,
        max_retries: int = 3,
        backoff_base: float = 2.0,
        timeout: int = 15,
    ):
        self._cache = cache
        self._token = token
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._timeout = timeout
        self._last_request_time: float = 0.0
        self._remaining_requests: Optional[int] = None
        self._reset_timestamp: Optional[float] = None

    # ─── Public API ────────────────────────────────────────────────

    def fetch(self, query: GitHubQuery) -> list[dict]:
        """Execute a single query, return raw API response items."""
        cache_key = f"gh_search:{query.query_string}:{query.min_stars}"

        # Check cache first
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Build request with conditional headers
        url = self._build_url(query)
        headers = self._build_headers()

        # Add If-None-Match for ETag conditional request
        etag = self._cache.get_etag(url)
        if etag:
            headers["If-None-Match"] = etag

        # Execute with retry
        response = self._request_with_retry(url, headers)
        if response is None:
            return []

        items = response.get("items", [])
        total_count = response.get("total_count", 0)

        # Cache the response
        self._cache.set(cache_key, items, ttl=3600)

        return items

    def fetch_multi(self, queries: list[GitHubQuery]) -> list[dict]:
        """Execute multiple queries, deduplicate by full_name."""
        seen: dict[str, dict] = {}

        for query in queries:
            items = self.fetch(query)
            for item in items:
                full_name = item.get("full_name", "")
                if full_name and full_name not in seen:
                    seen[full_name] = item

        return list(seen.values())

    def parse_repo(self, raw: dict) -> Repo:
        """Convert raw API dict into a validated Repo model."""
        license_info = raw.get("license")
        license_key = None
        if license_info and isinstance(license_info, dict):
            license_key = license_info.get("spdx_id")

        metrics = RepoMetrics(
            stars=raw.get("stargazers_count", 0),
            forks=raw.get("forks_count", 0),
            open_issues=raw.get("open_issues_count", 0),
            watchers=raw.get("watchers_count", 0),
            size_kb=raw.get("size", 0),
            is_archived=raw.get("archived", False),
            is_fork=raw.get("fork", False),
            license_key=license_key,
            topics=raw.get("topics", []) or [],
        )

        pushed_at = raw.get("pushed_at")
        created_at = raw.get("created_at")
        updated_at = raw.get("updated_at")

        days_since = None
        has_recent = False
        if pushed_at:
            try:
                push_dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
                days_since = (datetime.now(timezone.utc) - push_dt).days
                has_recent = days_since <= 30
            except (ValueError, TypeError):
                pass

        activity = RepoActivity(
            pushed_at=pushed_at,
            created_at=created_at,
            updated_at=updated_at,
            has_recent_commits=has_recent,
            days_since_last_push=days_since,
        )

        return Repo(
            full_name=raw["full_name"],
            html_url=raw["html_url"],
            description=raw.get("description"),
            metrics=metrics,
            activity=activity,
            semantics=SemanticExtract(),  # filled later by inspector
        )

    # ─── Internal ──────────────────────────────────────────────────

    def _build_url(self, query: GitHubQuery) -> str:
        """Construct the full API URL with query parameters."""
        parts = [self.SEARCH_ENDPOINT, "?"]

        # Build the q parameter
        q_parts = [query.query_string]
        if query.min_stars > 0:
            q_parts.append(f"stars:>{query.min_stars - 1}")
        if query.language:
            q_parts.append(f"language:{query.language}")

        q = " ".join(q_parts)
        params = f"q={urllib.parse.quote(q)}&per_page={min(query.max_results, 100)}"
        params += f"&sort={query.sort}&order={query.order}"

        return f"{self.SEARCH_ENDPOINT}?{params}"

    def _build_headers(self) -> dict[str, str]:
        """Build request headers with auth and content type."""
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "github-reference-finder/2.0",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _request_with_retry(
        self, url: str, headers: dict[str, str]
    ) -> Optional[dict]:
        """Execute request with exponential backoff retry."""
        last_error: Optional[Exception] = None

        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                delay = self._backoff_base ** attempt
                time.sleep(delay)

            try:
                # Rate limit pacing
                self._wait_for_rate_limit()

                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    # Update ETag
                    etag = resp.headers.get("ETag")
                    if etag:
                        self._cache.set_etag(url, etag)

                    # Update rate limit info
                    self._update_rate_limits(resp.headers)

                    # 304 Not Modified → return cached
                    if resp.status == 304:
                        cache_key = f"gh_search:{url}"
                        cached = self._cache.get(cache_key)
                        return cached

                    body = json.loads(resp.read().decode("utf-8"))
                    return body

            except urllib.error.HTTPError as e:
                last_error = e

                if e.code == 304:
                    # Not modified, return cached data
                    return None

                if e.code == 403:
                    # Check rate limit
                    retry_after = self._parse_retry_after(e.headers)
                    if retry_after:
                        raise RateLimitExceeded(
                            f"Rate limit exceeded, retry after {retry_after}s",
                            status_code=403,
                            retry_after=retry_after,
                        )
                    # Don't retry 403 without retry-after (likely auth issue)
                    raise FetcherError(f"GitHub API 403: {e.reason}")

                if e.code == 422:
                    # Unprocessable Entity — bad query, don't retry
                    raise FetcherError(f"GitHub API query error: {e.reason}")

                # 5xx errors — retry
                if e.code >= 500:
                    continue

                # Other errors — don't retry
                raise FetcherError(f"GitHub API {e.code}: {e.reason}")

            except urllib.error.URLError as e:
                last_error = e
                # Network error — retry
                continue

            except Exception as e:
                last_error = e
                continue

        # All retries exhausted
        raise FetcherError(f"Failed after {self._max_retries} retries: {last_error}")

    def _wait_for_rate_limit(self) -> None:
        """Pace requests to avoid hitting rate limits."""
        now = time.time()
        if self._remaining_requests is not None and self._remaining_requests <= 2:
            if self._reset_timestamp and self._reset_timestamp > now:
                wait = self._reset_timestamp - now + 1
                if wait > 0 and wait < 60:
                    time.sleep(wait)

        # Minimum gap between requests (0.5s for unauthenticated, 0.1s with token)
        min_gap = 0.1 if self._token else 0.5
        elapsed = now - self._last_request_time
        if elapsed < min_gap:
            time.sleep(min_gap - elapsed)
        self._last_request_time = time.time()

    def _update_rate_limits(self, headers) -> None:
        """Parse rate limit headers from response."""
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        if remaining:
            self._remaining_requests = int(remaining)
        if reset:
            self._reset_timestamp = float(reset)

    @staticmethod
    def _parse_retry_after(headers) -> Optional[int]:
        """Parse Retry-After header value."""
        retry = headers.get("Retry-After")
        if retry:
            try:
                return int(retry)
            except ValueError:
                return None
        return None
