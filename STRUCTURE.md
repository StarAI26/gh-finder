# gh-finder — Project Structure

## Directory Layout

```
gh-finder/
├── SKILL.md                    # Skill 使用指南 — 参数、Pipeline 流程、陷阱
├── STRUCTURE.md                # 本文件 — 项目结构、模块职责、依赖关系
└── src/
    ├── core/
    │   ├── models.py           # 数据模型 — Repo, SearchResult, ScoringConfig 等
    │   ├── interfaces.py       # 抽象接口 — 所有可插拔组件必须实现的 ABC
    │   └── pipeline.py         # Pipeline 编排 — 8 阶段搜索流程
    ├── scorers/
    │   ├── registry.py         # 插件注册 — 自动发现 scorers/*.py，计算加权分
    │   ├── trust.py            # 信任分 — Code Search API 验证代码引用
    │   ├── niche_boost.py      # 小众加成 — 领域感知的 stars 归一化
    │   ├── quality.py          # 质量分 — license / issue hygiene / archived
    │   ├── community.py        # 社区分 — stars/forks/watchers (log-scaled + fork 比率)
    │   ├── momentum.py         # 活跃度 — push 时间分层
    │   ├── semantic.py         # 语义分 — README 提取完整度
    │   ├── domain_fit.py       # 领域匹配 — 关键词匹配度
    │   └── topics.py           # 标签分 — GitHub topic 相关度
    ├── fetchers/
    │   ├── github.py           # GitHub API — Search API 客户端，重试/限流/ETag
    │   └── cache.py            # 文件缓存 — JSON TTL + ETag
    ├── inspectors/
    │   └── readme_parser.py    # README 分析 — header 检测 / inline fallback / 停用词
    ├── refiners/
    │   └── query_refiner.py    # 查询精化 — 分析结果生成新查询
    ├── query/
    │   └── builders.py         # 查询构建 — 多策略生成 GitHub 搜索查询
    └── config/
        ├── settings.py         # 配置加载 — 读取 JSON，提供类型化访问
        ├── weights.json        # 评分权重 / 阈值 / 限流 / 缓存 / Pool 配置
        ├── domain_rules.json   # 领域规则 — 查询模板 / hints / min_stars / max_stars
        └── stopwords.json      # 停用词 — README 关键词提取过滤
```

## Module Responsibilities

| 模块 | 职责 | 关键类/函数 |
|------|------|-----------|
| `core/models.py` | 所有数据结构定义（dataclasses） | `Repo`, `SearchResult`, `SearchContext` |
| `core/interfaces.py` | 可插拔组件的抽象契约 | `BaseScorer`, `BaseFetcher`, `BaseInspector`, `BaseRefiner` |
| `core/pipeline.py` | 8 阶段搜索流程编排 | `SearchPipeline.run()`, `create_pipeline()` |
| `scorers/registry.py` | Scorer 自动发现 + 加权计算 | `ScorerRegistry.register_auto()`, `composite_score()` |
| `scorers/trust.py` | Code Search API 引用验证 | `TrustScorer.compute()`, `compute_fallback()` |
| `scorers/niche_boost.py` | 领域感知小众项目加成 | `NicheBoostScorer.compute()`, `set_domain_config()` |
| `fetchers/github.py` | GitHub Search API 调用 | `GitHubFetcher.fetch()`, `fetch_multi()`, `parse_repo()` |
| `fetchers/cache.py` | 文件缓存 | `FileCache.get()`, `set()`, `get_etag()` |
| `inspectors/readme_parser.py` | README 深度语义提取 | `ReadmeInspector.extract()`, `extract_keywords()` |
| `refiners/query_refiner.py` | 查询精化 | `QueryRefiner.refine()`, `should_refine()` |
| `query/builders.py` | 多策略查询生成 | `MultiStrategyBuilder.build()` |
| `config/settings.py` | 配置加载 | `Settings.load()`, `get_domain_rule()` |

## Dependencies

```
pipeline.py (入口)
  ├── models.py          ← 所有模块共享数据结构
  ├── interfaces.py      ← 所有模块遵循的契约
  ├── settings.py        → 读取 weights.json, domain_rules.json
  ├── query/builders.py  → 使用 settings 的 domain rules
  ├── fetchers/github.py → 使用 fetchers/cache.py
  ├── scorers/registry.py → 自动发现 scorers/*.py
  ├── scorers/trust.py   → 手动初始化，Stage 7 调用
  ├── scorers/niche_boost.py → 手动初始化，Stage 7 调用
  ├── inspectors/readme_parser.py → README 提取
  └── refiners/query_refiner.py → 使用 settings 阈值
```

## Data Flow

```
raw_query (string)
    ▼
SearchContext (domain, hints, excludes, top_n)
    ▼ [Stage 1: Query Build]
list[GitHubQuery]
    ▼ [Stage 2: Fetch]
list[Repo] (metrics, activity, empty semantics)
    ▼ [Stage 3: Score]
list[Repo] (score_breakdown, composite_score) — 6 维粗排
    ▼ [Stage 4: Inspect — pool 全量]
list[Repo] (semantics enriched)
    ▼ [Stage 5: Refine]
    ├── should_refine()? → NO → Stage 7
    └── YES → refine() → list[GitHubQuery]
    ▼ [Stage 6: Re-Fetch] (only if refined)
list[Repo] (merged, deduplicated)
    ▼ [Stage 7: Re-Score]
list[Repo] (8 维完整评分: 6 标准 + trust + niche_boost)
    ▼ [Stage 8: Rank]
SearchResult.repos (top_n) + SearchResult.pool_repos (full pool)
```

## Config Files

| 文件 | 内容 |
|------|------|
| `weights.json` | 8 维权重、阈值、Pool 配置、限流、缓存 |
| `domain_rules.json` | per-domain: hints/excludes/查询模板/min_stars/max_stars |
| `stopwords.json` | 停用词: generic / project_fluff / phrases |

## Usage Navigation

| 我想知道什么 | 去看哪里 |
|-----------|---------|
| 评分权重具体数字 | `src/config/weights.json` |
| 某个 scorer 的计算公式 | `src/scorers/<name>.py` 的 docstring |
| 领域特定规则（min_stars, 查询模板） | `src/config/domain_rules.json` |
| 完整数据结构定义 | `src/core/models.py` |
| Pipeline 编排细节 | `src/core/pipeline.py` |
| 各模块接口契约 | `src/core/interfaces.py` |
| 如何添加新的 scorer | `src/scorers/registry.py` + 复制已有 scorer 模板 |
