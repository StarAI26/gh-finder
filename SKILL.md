---
name: gh-finder
category: search
version: 1.0
description: Find high-quality GitHub projects via natural language intent. / 通过自然语言意图查找高质量 GitHub 开源项目，推荐最佳工具与库。
---

# GitHub Reference Finder (gh-finder)

## 🎯 Purpose

Find high-quality, relevant open-source projects on GitHub via iterative refinement.
First round may miss the mark — the pipeline inspects results to learn what's actually needed, then refines queries and searches again.

## 🔑 Mandatory Pre-Step: Understand True Needs

When the user first provides a search request, **assess clarity before acting**:

- **If intent is vague** (e.g., "找个画图工具", "推荐个CLI"): Offer `true-needs` refinement naturally:
  > "Before I search, would you like me to refine your request using 'Deep Needs Discovery'? It usually finds better matches."
  > 1. Yes, help me refine (recommended)
  > 2. No, just search with my current words.

- **If intent is already specific** (query + constraints + scope provided): Extract parameters directly. **Do NOT ask.**

If user picks **1** → Load `true-needs` skill, follow its process.
If user picks **2** or says "直接搜" / "just search" → Skip refinement, extract parameters from raw query, still apply domain detection and LLM mapping.

---

## 📦 Usage Guide

### 参数提取

| 参数 | 说明 | 示例 |
|------|------|------|
| query | **最核心的单个关键词**（1-2 个词），不要堆砌 | "figlet", "ascii art" |
| hints | 相关同义词/技术栈 3-6 个，补充搜索面 | ["banner", "toilet", "fancy", "font"] |
| excludes | 明确不需要的 | ["image"] |
| domain | general / frontend_design / search_tool / devops | general |
| top_n | 返回前 N 个 | 5 |

**⚠️ query 不要堆砌多词**。GitHub Search API 用 AND 连接，query 超过 3 个词几乎必然返回 0。
LLM 应当从用户请求中**提炼最核心的 1-2 个关键词**作为 query，其余放 hints。

**LLM 领域映射**：用户通常用描述性语言，LLM 需将其翻译为领域标准术语放入 hints。
- "文字大图案" → `figlet`, `banner`, `ascii art`
- "网站监控报警" → `prometheus`, `grafana`
- "静态博客生成器" → `hugo`, `jekyll`

### 示例代码

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

# 查看完整候选池
print(f"Pool size: {result.pool_size}")
for repo in result.pool_repos:
    print(f"  {repo.full_name}: score={repo.composite_score:.1f}, "
          f"trust={repo.score_breakdown.get('trust', 'N/A')}, "
          f"suspicion={repo.soft_post_suspicion}")
```

### 输出解读

`SearchResult` 返回两层数据：
- `result.repos` — 最终 top_n 个排名结果（用户直接看到的）
- `result.pool_repos` — 完整候选池（top_n × expansion_factor，用于分析）

每个 `Repo` 包含：
- `composite_score` — 加权总分（0-100）
- `score_breakdown` — 各维度分项分数（含 trust, niche_boost）
- `soft_post_suspicion` — True 表示"高星但零代码引用"，疑似营销
- `semantics.purpose/result/audience/tech_stack` — README 语义提取结果

---

## 🔧 Pipeline & Architecture

### 项目结构

详见 `STRUCTURE.md` — 模块职责、依赖关系、数据流向。

```
src/
├── core/       # 数据模型、接口、Pipeline 编排
├── scorers/    # 评分插件（8 个维度，含 trust + niche_boost）
├── fetchers/   # GitHub API 客户端 + 文件缓存
├── inspectors/ # README 深度语义分析
├── refiners/   # 查询精化 — 分析结果生成新查询
├── query/      # 查询构建器（多策略）
└── config/     # weights / domain_rules / stopwords / settings
```

### 8 阶段 Pipeline

```
用户请求 → LLM 提取参数 → pipeline.run() → 8 阶段 + Pool Expansion → SearchResult

Pool Expansion:
  pool_size = min(top_n × 3, 30)
  Stage 4-7 在 pool_size 规模上运行（不是只处理前 top_n）
  Stage 8 最终截断到 top_n 输出
```

#### Stage 1: Query Build

- **功能**：多策略生成初版搜索查询（5-12 个）
- **模块**：`src/query/builders.py` — `MultiStrategyBuilder`
- **输入**：`SearchContext`（raw_query, domain, hints, excludes）
- **输出**：`list[GitHubQuery]`（带查询字符串、min_stars、语言过滤）
- **策略**：KeywordBuilder + TemplateBuilder（基于 domain_rules.json 模板）

#### Stage 2: Fetch

- **功能**：执行查询，去重（按 full_name），缓存
- **模块**：`src/fetchers/github.py` — `GitHubFetcher.fetch_multi()`
- **输入**：`list[GitHubQuery]`
- **输出**：`list[dict]`（GitHub API 原始响应 items）
- **机制**：ETag 条件请求 + TTL 缓存 + 重试/退避 + 限流感知

#### Stage 3: Score（预检粗排）

- **功能**：对全部候选做 6 维粗排（不含 trust/niche_boost）
- **模块**：`src/scorers/registry.py` 自动加载 `src/scorers/*.py`
- **输入**：`list[Repo]`（解析自 API 原始数据）
- **输出**：`list[Repo]`（附加 `score_breakdown` 和 `composite_score`）
- **维度**：quality, community, momentum, semantic, domain_fit, topics
- **注意**：domain_fit 关键词由 pipeline 在评分前通过 `set_keywords()` 注入

#### Stage 4: Inspect（全量语义分析）

- **功能**：读取 pool 中**全部候选**的 README，提取语义（不是只读前 top_n）
- **模块**：`src/inspectors/readme_parser.py` — `ReadmeInspector.extract_batch()`
- **输入**：`list[Repo]`（按粗排分数排序后的 pool 全部候选）
- **输出**：`list[Repo]`（附加 `semantics.purpose/result/audience/tech_stack/summary`）
- **提取流程**：结构化 header 检测 → inline phrase fallback → markdown 清洗 → 停用词过滤 → 关键词提取
- **注意**：这是 Pool Expansion 的核心受益点——小众项目在粗排分数低但 README 质量高

#### Stage 5: Refine

- **功能**：分析语义差距，决定是否生成精化查询
- **模块**：`src/refiners/query_refiner.py`
- **输入**：`list[Repo]`（已提取语义）, `SearchContext`
- **输出**：`bool`（是否需要 refine）+ 若需要则 `list[GitHubQuery]`（新查询）
- **触发条件**：best domain_fit ≤ 55, OR best semantic ≤ 20, OR avg domain_fit ≤ 55
- **策略**：提取 README 关键词组合成新查询，排除不匹配类型

#### Stage 6: Re-Fetch

- **功能**：执行精化查询，合并结果（去重）
- **模块**：`src/fetchers/github.py` — `fetch_multi()`
- **输入**：`list[GitHubQuery]`（精化查询）
- **输出**：`list[dict]`（新仓库原始数据，合并到主列表）
- **合并**：按 full_name 去重，保留已有数据优先

#### Stage 7: Re-Score（全量重排）

- **功能**：对所有候选做完整 8 维评分（含 trust + niche_boost）
- **模块**：`src/scorers/registry.py` + `scorers/trust.py` + `scorers/niche_boost.py`
- **输入**：`list[Repo]`（合并后的全量候选，含语义）
- **输出**：`list[Repo]`（完整 8 维 `score_breakdown` 和 `composite_score`）
- **trust scorer**：只对 pool 前 `top_n × 2` 执行（控制 Code Search API 成本），限流时 fallback 50 分
  - 搜索 `requirements.txt/package.json/go.mod/Cargo.toml/pom.xml/README` 中的 repo 引用
  - 设置 `soft_post_suspicion` 标记（stars>1000 且引用=0）
- **niche_boost scorer**：基于 domain_rules.json 的 min_stars/max_stars 做领域感知加成
  - 甜区项目（领域内 stars 适中）最多 +10 分
- **fork/star 比率**：community scorer 在 ratio < 0.03 时 -25% 惩罚

#### Stage 8: Rank

- **功能**：按 composite_score 排序，分配 rank，截断到 top_n 返回
- **模块**：`pipeline.py` — `_rank_repos()`
- **输入**：`list[Repo]`（完整 pool，已全量评分）
- **输出**：`list[Repo]`（排序后，前 top_n 放入 `result.repos`，全量放入 `result.pool_repos`）

### 配置与代码导航

详见 STRUCTURE.md 末尾的 **Usage Navigation** — 按"我想知道什么"快速定位文件。

---

### ⚠️ Pitfalls

#### API 相关

1. **`stars:>=N` doesn't work** — 代码自动转为 `stars:>N-1`，不要用 >= 语法
2. **Unauthenticated: 10 req/min** — 设 `GITHUB_TOKEN` 提升到 30 req/min
3. **Over-specific queries return 0** — GitHub API 是 AND 逻辑，query 越长结果越少。从宽到窄。

#### 参数相关

4. **query 不要堆砌** — 提炼最核心 1-2 个词作为 query，其余放 hints
5. **Refiner 可能生成 0 查询** — 首轮结果已经很好时会跳过 refine，这是正常的
6. **Pool expansion 增加 API 成本** — 默认 3x pool 意味着多读 2 倍 README。调整 `pool_expansion_factor` 可控制

#### 输出解读

7. **soft_post_suspicion 标记** — stars>1000 但 trust mentions==0 的项目。说明可能营销炒作
8. **trust 分只在 pool 前 N×2 有值** — 为控制 Code Search API 成本，尾部候选的 trust 为空
