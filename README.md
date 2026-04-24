# gh-finder

Find high-quality GitHub projects via natural language intent.

## What It Does

gh-finder is a Claude skill that searches GitHub for the best open-source projects matching your needs. Instead of relying on a single search, it runs an **8-stage pipeline** that inspects results, learns what's actually needed, refines queries, and searches again.

Key features:
- **Natural language search** — describe what you need in plain language
- **8-dimensional scoring** — quality, community, momentum, semantics, domain fit, topics, trust verification, niche boost
- **Trust verification** — detects "vapor repos" with inflated stars but zero real code usage
- **Iterative refinement** — automatically improves search based on first-round results
- **Pool expansion** — evaluates a broader candidate set, not just top results
- **Pure stdlib** — zero dependencies, Python only

## Quick Start

```python
from src.core.pipeline import create_pipeline
from src.config.settings import Settings

settings = Settings.load()
pipeline = create_pipeline(settings)
result = pipeline.run(
    "ascii art text generator",
    domain="general",
    hints=["ascii", "figlet", "banner"],
    top_n=5,
)
print(result.to_markdown())
```

Requires a `GITHUB_TOKEN` environment variable for rate limits (30 req/min unauthenticated = 10 req/min).

## How It Works

```
Query Build → Fetch → Score (6D) → Inspect READMEs → Refine? → Re-Fetch → Re-Score (8D) → Rank
```

| Stage | What |
|-------|------|
| 1. Query Build | Generate 5-12 search queries from domain rules + keyword expansion |
| 2. Fetch | Execute queries, deduplicate, cache with ETag |
| 3. Score | 6-dimension coarse ranking (quality, community, momentum, semantic, domain, topics) |
| 4. Inspect | Read all candidate READMEs, extract purpose/result/audience/tech_stack |
| 5. Refine | Analyze semantic gaps, generate better queries if needed |
| 6. Re-Fetch | Execute refined queries, merge results |
| 7. Re-Score | Full 8D scoring including trust (Code Search API) and niche boost |
| 8. Rank | Sort, assign rank, return top N |

## Configuration

All config lives in `src/config/`:

| File | Purpose |
|------|---------|
| `weights.json` | Scoring weights, thresholds, rate limits, cache settings |
| `domain_rules.json` | Per-domain query templates, hints, min/max stars |
| `stopwords.json` | Stop words for README keyword extraction |
| `settings.py` | Config loader |

## Project Structure

See [STRUCTURE.md](STRUCTURE.md) for full module details.

## Testing

```bash
python -m pytest src/test_integration.py
```

Or run directly:

```bash
python src/test_integration.py
```
