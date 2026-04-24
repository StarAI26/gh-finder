"""Quick integration test — verifies all components wire together correctly."""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.settings import Settings
from src.core.models import Repo, RepoMetrics, RepoActivity, SemanticExtract, SearchContext
from src.core.pipeline import create_pipeline
from src.scorers.registry import ScorerRegistry
from src.query.builders import MultiStrategyBuilder
from src.inspectors.readme_parser import ReadmeInspector
from src.refiners.query_refiner import QueryRefiner


def test_pipeline_creation():
    """Test that the pipeline can be created with all components."""
    settings = Settings.load()
    pipeline = create_pipeline(settings)
    assert pipeline is not None
    assert len(pipeline._scorer_registry.list_scorers()) == 6
    assert pipeline._refiner is not None
    names = [s["name"] for s in pipeline._scorer_registry.list_scorers()]
    print(f"  PASS: Pipeline created with 6 scorers: {names}")


def test_scorers_on_mock_data():
    """Test scoring with mock repo data."""
    settings = Settings.load()
    reg = ScorerRegistry(settings.scoring)
    reg.register_auto("src/scorers")
    assert len(reg.list_scorers()) == 6

    repos = [
        Repo(
            full_name="healthy/active",
            html_url="https://github.com/healthy/active",
            description="A well-maintained project",
            metrics=RepoMetrics(stars=1000, forks=200, open_issues=5, license_key="MIT", topics=["python", "cli"]),
            activity=RepoActivity(days_since_last_push=3, has_recent_commits=True),
        ),
        Repo(
            full_name="stale/archived",
            html_url="https://github.com/stale/archived",
            description="An archived project",
            metrics=RepoMetrics(stars=5000, forks=100, open_issues=200, is_archived=True, topics=[]),
            activity=RepoActivity(days_since_last_push=500),
        ),
    ]

    for repo in repos:
        breakdown = reg.compute_all(repo)
        repo.score_breakdown = breakdown
        repo.composite_score = reg.composite_score(breakdown)

    sorted_repos = sorted(repos, key=lambda r: r.composite_score, reverse=True)
    print(f"  PASS: Scoring works")
    for r in sorted_repos:
        print(f"    {r.full_name:25s} score={r.composite_score:6.1f}  breakdown={r.score_breakdown}")


def test_domain_fit_injection():
    """Test that domain keywords are properly injected into domain_fit scorer."""
    settings = Settings.load()
    pipeline = create_pipeline(settings)

    pipeline._inject_domain_keywords("frontend_design")

    domain_scorer = pipeline._scorer_registry.scorers.get("domain_fit")
    assert domain_scorer is not None
    assert len(domain_scorer.keywords) > 0, "domain_fit should have keywords after injection"
    print(f"  PASS: domain_fit injected {len(domain_scorer.keywords)} keywords")


def test_refiner():
    """Test query refiner analyzes results and generates refined queries."""
    settings = Settings.load()
    refiner = QueryRefiner(settings)

    # Simulate first-round results that are misaligned (image → ASCII, not text → ASCII)
    repos = [
        Repo(
            full_name="bad/image2ascii",
            html_url="https://github.com/bad/image2ascii",
            description="Convert images to ASCII art",
            metrics=RepoMetrics(topics=["image", "ascii"]),
            activity=RepoActivity(days_since_last_push=5),
            semantics=SemanticExtract(
                purpose="Convert images to ASCII art",
                result="A command-line tool",
                audience="Developers",
                tech_stack=["Python", "CLI"],
            ),
            composite_score=35.0,
        ),
        Repo(
            full_name="bad/qr-art",
            html_url="https://github.com/bad/qr-art",
            description="QR code to ASCII art",
            metrics=RepoMetrics(topics=["qr", "ascii"]),
            activity=RepoActivity(days_since_last_push=10),
            semantics=SemanticExtract(
                purpose="QR code to ASCII art",
                result="A command-line tool",
                audience="Developers",
                tech_stack=["Go", "CLI"],
            ),
            composite_score=30.0,
        ),
    ]

    ctx = SearchContext(raw_query="ascii art text generator", domain="general")

    # Should refine — results are incomplete/low quality
    assert refiner.should_refine(repos, ctx) == True
    print("  PASS: Refiner detected need for refinement")

    # Generate refined queries
    refined = refiner.refine(ctx, repos)
    assert len(refined) > 0
    print(f"  PASS: Refiner generated {len(refined)} refined queries:")
    for q in refined[:3]:
        print(f"    → {q.query_string}")


def test_refiner_skip():
    """Test refiner skips when results are good enough."""
    settings = Settings.load()
    refiner = QueryRefiner(settings)

    # Simulate good results — high domain_fit and semantic scores
    repos = [
        Repo(
            full_name="good/figlet",
            html_url="https://github.com/good/figlet",
            description="Text to ASCII art generator",
            metrics=RepoMetrics(stars=5000, topics=["ascii", "text", "figlet"]),
            activity=RepoActivity(days_since_last_push=3),
            semantics=SemanticExtract(
                purpose="Generate ASCII art from text",
                result="A command-line tool",
                audience="Developers",
                tech_stack=["C", "CLI"],
            ),
            composite_score=75.0,
            score_breakdown={"health": 70, "semantic": 65, "momentum": 100, "domain_fit": 80, "topics": 30},
        ),
    ]

    ctx = SearchContext(raw_query="ascii art text generator", domain="general")
    should = refiner.should_refine(repos, ctx)
    assert should == False, f"Good results should not trigger refine, got {should}"
    print(f"  PASS: Refiner correctly skipped refinement (domain_fit=80, semantic=65)")


def test_inspector():
    """Test semantic extraction."""
    inspector = ReadmeInspector()
    repo = Repo(
        full_name="test/swiper",
        html_url="https://github.com/test/swiper",
        description="Modern touch slider with React and Vue support",
        metrics=RepoMetrics(topics=["slider", "react", "vue", "javascript"]),
        activity=RepoActivity(days_since_last_push=5),
    )
    result = inspector.extract(repo)
    assert result.semantics.tech_stack, "Tech stack should be detected"
    assert result.semantics.purpose, "Purpose should be extracted"
    print(f"  PASS: Inspector extracted tech={result.semantics.tech_stack}")


def test_query_builder():
    """Test query generation."""
    settings = Settings.load()
    builder = MultiStrategyBuilder(settings)

    ctx = SearchContext(
        raw_query="admin dashboard",
        domain="frontend_design",
        hints=["css"],
    )
    queries = builder.build(ctx)
    assert len(queries) > 0, "Should generate at least one query"
    q_strings = [q.query_string for q in queries]
    assert len(q_strings) == len(set(q_strings)), "No duplicate queries"
    print(f"  PASS: Generated {len(queries)} unique queries")


def test_cache():
    """Test file cache."""
    import tempfile
    from src.fetchers.cache import FileCache

    with tempfile.TemporaryDirectory() as tmpdir:
        cache = FileCache(tmpdir, default_ttl=1)
        cache.set("test", {"key": "value"}, ttl=10)
        assert cache.get("test") == {"key": "value"}
        assert cache.get("nonexistent") is None

        cache.set("expire", {"data": "gone"}, ttl=1)
        import time
        time.sleep(1.1)
        assert cache.get("expire") is None
    print("  PASS: Cache set/get/expire")


def test_markdown_output():
    """Test SearchResult markdown rendering."""
    from src.core.models import SearchResult
    ctx = SearchContext(raw_query="test", domain="general", top_n=3)
    result = SearchResult(context=ctx)
    result.repos = [
        Repo(
            full_name="test/repo",
            html_url="https://github.com/test/repo",
            description="A test repo",
            metrics=RepoMetrics(stars=100, license_key="MIT"),
            activity=RepoActivity(days_since_last_push=5),
            semantics=SemanticExtract(purpose="Test project", tech_stack=["python"]),
            composite_score=65.5,
        ),
    ]
    result.total_repos_found = 1
    result.total_repos_scored = 1
    md = result.to_markdown()
    assert "test/repo" in md
    assert "65.5" in md
    print("  PASS: Markdown output renders correctly")


if __name__ == "__main__":
    print("=== Integration Tests ===\n")
    test_pipeline_creation()
    test_scorers_on_mock_data()
    test_domain_fit_injection()
    test_refiner()
    test_refiner_skip()
    test_inspector()
    test_query_builder()
    test_cache()
    test_markdown_output()
    print("\n=== All tests passed ===")
