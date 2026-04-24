"""README inspector — deep semantic extraction from repo README.

Pure stdlib rule-based extraction. No LLM dependency.
Fetches raw README from GitHub API for deep analysis.

Extraction pipeline:
  1. Fetch README (base64 decode)
  2. Clean markdown (strip **, *, `, links, bullets, headers, HTML)
  3. Detect structured sections (Why/Motivation/Features/Result)
  4. Inline phrase fallback (enables/generates/solves)
  5. Stop word filtering
  6. Keyword extraction (~8 words max for query generation)
"""

from __future__ import annotations

import base64
import json
import re
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

from src.core.models import Repo, SemanticExtract
from src.core.interfaces import BaseInspector


# ─── Config path ──────────────────────────────────────────────────

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


# ─── Tech Stack Detection ──────────────────────────────────────────

TECH_PATTERNS = {
    "React": r"\breact\b",
    "Vue": r"\bvue\b",
    "Angular": r"\bangular\b",
    "Svelte": r"\bsvelte\b",
    "Next.js": r"\bnext\.?js\b",
    "TypeScript": r"\btypescript\b|\btsconfig\b",
    "Python": r"\bpython\b|\.py\b",
    "Go": r"\bgolang\b|\bgo\b",
    "Rust": r"\brust\b|\bcargo\.toml\b",
    "JavaScript": r"\bjavascript\b|\.mjs\b",
    "Node.js": r"\bnode\.?js\b|\bnpm\b|\bpackage\.json\b",
    "Docker": r"\bdocker\b|Dockerfile",
    "Tailwind": r"\btailwind\b",
    "CSS": r"\bcss\b|\.css\b",
    "SCSS": r"\bscss\b|\.scss\b",
    "Webpack": r"\bwebpack\b",
    "Vite": r"\bvite\b",
    "GraphQL": r"\bgraphql\b",
    "REST": r"\brest\b|\bapi\b",
    "CLI": r"\bcli\b|command.?line",
    "GitHub Actions": r"\bgithub.?actions\b|\.github/workflows",
    "CI/CD": r"\bci[/-]?cd\b",
}

AUDIENCE_PATTERNS = {
    "Frontend developers": r"frontend|ui|css|design.?system|component",
    "Backend developers": r"backend|server|api|database",
    "DevOps engineers": r"devops|deploy|infrastructure|kubernetes|docker",
    "Data scientists": r"data.?science|ml|machine.?learning|analytics",
    "Designers": r"designer|design.?tool|mockup|prototype",
    "Beginners": r"beginner|tutorial|learning|getting.?started",
    "Enterprise teams": r"enterprise|organization|scalable|production.?ready",
}

# ─── Structured Header Detection ───────────────────────────────────

# Headers that indicate Purpose (Why)
PURPOSE_HEADERS = re.compile(
    r"^#{1,4}\s*(why|motivation|problem|goal|objective|purpose|the problem|"
    r"background|context|introduction|about this project|rationale|"
    r"what is|what this does|why we built)",
    re.MULTILINE | re.IGNORECASE,
)

# Headers that indicate Result (What)
RESULT_HEADERS = re.compile(
    r"^#{1,4}\s*(features|output|result|performance|benefit|capabilities|"
    r"functionality|what it does|how it works|solution|key features|"
    r"highlights|overview|specifications|characteristics)",
    re.MULTILINE | re.IGNORECASE,
)

# Inline phrases that indicate capability
INLINE_PATTERNS = re.compile(
    r"(?:allows?\s*(?:you\s*)?to|enables?\s*(?:you\s*)?to|"
    r"generates?|outputs?|solves?|helps?\s*(?:you\s*)?to|"
    r"provides?\s*(?:a\s*)?way\s*to|offers?\s*|"
    r"designed\s*to|built\s*to|created\s*to|made\s*to|"
    r"lets?\s*(?:you\s*)?|"
    r"(?:can|will)\s+(?:be\s+)?(?:used\s+to|help|generate|create|build))",
    re.IGNORECASE,
)


class ReadmeInspector(BaseInspector):
    """Extracts structured semantic data from repos."""

    def __init__(self, token: Optional[str] = None, timeout: int = 10):
        self._token = token
        self._timeout = timeout
        self._stopwords: Optional[set] = None

    def _load_stopwords(self) -> set:
        """Load stop words from config file."""
        if self._stopwords is not None:
            return self._stopwords

        stopwords_path = _CONFIG_DIR / "stopwords.json"
        if stopwords_path.exists():
            with open(stopwords_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            generic = set(w.lower() for w in data.get("generic", []))
            fluff = set(w.lower() for w in data.get("project_fluff", []))
            self._stopwords = generic | fluff
        else:
            # Minimal fallback
            self._stopwords = {"the", "a", "an", "is", "are", "and", "or", "for", "of", "to", "in"}

        return self._stopwords

    def extract(self, repo: Repo) -> Repo:
        """Enrich a single repo with semantic extraction."""
        readme_text = self.fetch_readme(repo)

        purpose = self._extract_purpose(repo, readme_text)
        result = self._extract_result(repo, readme_text=readme_text)
        audience = self._extract_audience(repo, readme_text=readme_text)
        tech_stack = self._extract_tech_stack(repo, readme_text=readme_text)
        summary = self._build_summary_from(purpose, tech_stack)

        repo.semantics = SemanticExtract(
            purpose=purpose,
            result=result,
            audience=audience,
            tech_stack=tech_stack,
            summary=summary,
        )
        return repo

    def extract_batch(self, repos: list[Repo], limit: int = 5) -> list[Repo]:
        """Extract semantics for top-N repos."""
        for repo in repos[:limit]:
            self.extract(repo)
        return repos

    # ─── Deep README Analysis ────────────────────────────────────

    def _extract_purpose(self, repo: Repo, readme_text: Optional[str] = None) -> str:
        """Extract purpose using structured headers + inline fallback."""
        # First try: structured header detection
        if readme_text:
            purpose = self._extract_from_headers(readme_text, PURPOSE_HEADERS)
            if purpose:
                cleaned = self._clean_and_limit(purpose)
                if len(cleaned) > 10:
                    return cleaned

        # Fallback: use description
        desc = repo.description or ""
        if desc:
            purpose = desc.strip().rstrip(".")
            if len(purpose) > 200:
                purpose = purpose[:200] + "..."
            return purpose

        # Fallback: derive from repo name
        name = repo.full_name.split("/")[-1] if "/" in repo.full_name else repo.full_name
        return f"Project: {name.replace('-', ' ').replace('_', ' ').title()}"

    def _extract_result(self, repo: Repo, readme_text: Optional[str] = None) -> str:
        """Extract what the repo delivers using headers + inline fallback."""
        if readme_text:
            # Try structured headers first
            result = self._extract_from_headers(readme_text, RESULT_HEADERS)
            if result:
                cleaned = self._clean_and_limit(result)
                if len(cleaned) > 10:
                    return cleaned

            # Inline phrase fallback
            result = self._extract_from_inline_phrases(readme_text)
            if result:
                return self._clean_and_limit(result)

        # Fallback: keyword-based inference
        desc = (repo.description or "").lower()
        topics = " ".join(repo.metrics.topics).lower()
        readme = (readme_text or "").lower()[:2000]
        text = desc + " " + topics + " " + readme

        if any(w in text for w in ["framework", "library", "toolkit"]):
            return "A reusable framework/library"
        if "cli" in text or "command" in text:
            return "A command-line tool"
        if "template" in text or "boilerplate" in text or "starter" in text:
            return "A starter template / boilerplate"
        if "api" in text or "server" in text:
            return "An API or server application"
        if "component" in text or "ui" in text:
            return "UI components"
        if "plugin" in text or "extension" in text:
            return "A plugin or extension"
        return "Open-source project"

    def _extract_audience(self, repo: Repo, readme_text: Optional[str] = None) -> str:
        """Detect target audience from keywords."""
        desc = (repo.description or "").lower()
        topics = " ".join(repo.metrics.topics).lower()
        readme = (readme_text or "").lower()[:2000]
        text = desc + " " + topics + " " + readme

        for audience, pattern in AUDIENCE_PATTERNS.items():
            if re.search(pattern, text):
                return audience
        return "Developers"

    def _extract_tech_stack(self, repo: Repo, readme_text: Optional[str] = None) -> list[str]:
        """Detect technologies mentioned in description and topics."""
        desc = (repo.description or "").lower()
        topics = " ".join(repo.metrics.topics).lower()
        readme = (readme_text or "").lower()[:2000]
        text = desc + " " + topics + " " + readme

        detected = []
        for tech, pattern in TECH_PATTERNS.items():
            if re.search(pattern, text, re.IGNORECASE):
                detected.append(tech)

        return detected[:8]

    def _build_summary_from(self, purpose: str, tech_stack: list[str]) -> str:
        """Build a one-line summary from extracted values."""
        parts = []
        if purpose:
            parts.append(purpose)
        if tech_stack:
            parts.append(f"[{', '.join(tech_stack[:3])}]")
        return " ".join(parts)

    # ─── Header & Phrase Extraction ──────────────────────────────

    def _extract_from_headers(self, text: str, header_pattern: re.Pattern) -> str:
        """Extract content following a matched header section."""
        matches = list(header_pattern.finditer(text))
        if not matches:
            return ""

        # Use the first matching header
        start = matches[0].end()

        # Find the next header (any level) or end of text
        next_header = re.search(r"^#{1,4}\s+", text[start:], re.MULTILINE)
        if next_header:
            end = start + next_header.start()
        else:
            end = len(text)

        section = text[start:end].strip()
        return section

    def _extract_from_inline_phrases(self, text: str) -> str:
        """Extract capability statements from inline phrases."""
        matches = INLINE_PATTERNS.findall(text)
        if not matches:
            return ""

        # Take first 2 matches (enough for a meaningful query)
        phrases = matches[:2]
        return " ".join(phrases)

    # ─── Markdown Cleaning ───────────────────────────────────────

    def _clean_text(self, text: str) -> str:
        """Aggressively clean markdown text for keyword extraction."""
        # Remove HTML tags
        text = re.sub(r"<[^>]+>", "", text)

        # Remove images: ![alt](url)
        text = re.sub(r"!\[[^\]]*\]\([^\)]*\)", "", text)

        # Remove links: [text](url) → text
        text = re.sub(r"\[([^\]]*)\]\([^\)]*\)", r"\1", text)

        # Remove bold/italic: **text** or *text* → text
        text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
        text = re.sub(r"_{1,3}([^_]+)_{1,3}", r"\1", text)

        # Remove inline code: `code` → code
        text = re.sub(r"`([^`]+)`", r"\1", text)

        # Remove headers: # text → text
        text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)

        # Remove bullet points: -, *, + at start of line
        text = re.sub(r"^\s*[-*+]\s*", "", text, flags=re.MULTILINE)

        # Remove horizontal rules
        text = re.sub(r"^[-*_]{3,}$", "", text, flags=re.MULTILINE)

        # Remove extra whitespace
        text = re.sub(r"\s+", " ", text).strip()

        return text

    def _clean_and_limit(self, text: str, max_words: int = 50) -> str:
        """Clean text and limit word count."""
        cleaned = self._clean_text(text)

        # Filter stop words
        stopwords = self._load_stopwords()
        words = cleaned.split()
        filtered = [w for w in words if w.lower() not in stopwords and len(w) > 2]

        # Limit to max_words
        if len(filtered) > max_words:
            filtered = filtered[:max_words]

        return " ".join(filtered)

    def extract_keywords(self, text: str, max_keywords: int = 8) -> list[str]:
        """Extract meaningful keywords from text for query generation.

        Used by the refiner to generate refined search queries.
        """
        cleaned = self._clean_text(text)

        # Filter stop words and short words
        stopwords = self._load_stopwords()
        words = cleaned.split()
        keywords = [
            w for w in words
            if w.lower() not in stopwords
            and len(w) > 2
            and not w.startswith("http")
            and re.match(r"^[a-zA-Z][a-zA-Z0-9._-]*$", w)
        ]

        # Remove duplicates while preserving order
        seen = set()
        unique = []
        for w in keywords:
            wl = w.lower()
            if wl not in seen:
                seen.add(wl)
                unique.append(w)

        return unique[:max_keywords]

    # ─── README Fetching (optional, best-effort) ─────────────────

    def fetch_readme(self, repo: Repo) -> Optional[str]:
        """Fetch raw README content from GitHub API (best-effort)."""
        owner, name = repo.full_name.split("/", 1) if "/" in repo.full_name else ("", repo.full_name)
        if not owner or not name:
            return None

        url = f"https://api.github.com/repos/{owner}/{name}/readme"
        headers = {"Accept": "application/vnd.github.v3+json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content_b64 = data.get("content", "")
                return base64.b64decode(content_b64).decode("utf-8", errors="replace")
        except Exception:
            return None
