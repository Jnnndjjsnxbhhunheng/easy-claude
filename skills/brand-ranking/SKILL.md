---
name: brand-ranking
version: 2.0
description: 用于品牌榜、品牌排行、品牌热销榜等品牌维度电商排行任务。采用"aiapi 语义召回 -> aiapi 定向口碑交叉验证 -> hotsell/bmc 结构化补全 -> 四维加权排序"链路，质量指标权重(55%) > 规模指标权重(45%)。当用户提到品牌榜、品牌排行、品牌热销榜、什么牌子好、品牌推荐、品牌对比等品牌维度排行需求时触发。
---

# Brand Ranking v2

品牌维度电商排行 skill。核心理念：品牌榜应反映"哪个牌子好"而非"哪个牌子大"，因此质量信号（UGC 语义 + 口碑）权重 55%，规模信号（销量 + 市场存在感）权重 45%。

## Reference Files

本 skill 将详细规则拆分到 references/ 目录，按需读取：

| 文件 | 内容 | 何时读取 |
|------|------|----------|
| `references/semantic-annotation.md` | 语义标注规则、semantic_score 计算、candidate_tier 分层与升降格 | 执行 Step 3（aiapi 主召回）前必读 |
| `references/scoring-formula.md` | 四维评分公式细节、结构化缺失处理、排序策略覆盖与同分处理 | 执行 Step 6（排序）前必读 |
| `references/output-template.md` | 标准化输出格式、榜单头部/品牌列表/尾部模板、完整示例 | 执行 Step 8（输出）前必读 |

## Bundled Scripts

虽然远端基础版 skill 只描述 workflow，本地实现保留并强化了 Step 5 的脚本链路，以下脚本属于正式 workflow 的一部分：

| 脚本 | 用途 | 何时运行 |
|------|------|----------|
| `scripts/build_hotsell_plan.py` | 冻结 stable/provisional 候选池，生成逐品牌 hotsell 执行清单 | 进入 Step 5 前必跑 |
| `scripts/merge_hotsell_results.py` | 合并多品牌、多页 hotsell 召回结果，按商品 ID/链接去重，避免候选商品丢失 | Step 5 结束后必跑 |
| `scripts/check_hotsell_coverage.py` | 校验执行清单中的品牌是否都完成召回 | Step 5 结束后、Step 6 前必跑 |

## MCP Tools

### Required

- `ranking_ugc_search_aiapi` — UGC 主召回 + 语义标注来源
- `ranking_hotsell_recall` — 按品牌名召回商品样本
- `ranking_bmc_detail_enrich` — 批量补全结构化详情

### Optional

- `ranking_sh_detail_enrich` — 识货体系商品补全
- `ranking_brand_normalize` — 品牌名标准化归一

## Core Rules

- 只使用 `ranking_*` 工具，不调用远端 skill 接口。
- `aiapi` 是唯一 UGC 能力来源；Step 3 做主召回，Step 4 继续用 `aiapi` 做定向口碑调查与交叉验证。
- 不再使用 `uiapi`。
- 每次品牌提及必须做语义标注（sentiment / mention_type / context_strength），不允许"提到即入池"。
- 被集体批评的品牌（`semantic_score < 0`）进入 noise，不得入池。
- 若 `rank_type = product`，终止本 skill，改用 `product-ranking`。
- 未指定平台时默认 `mall = 京东`。
- 不允许编造数据；无法通过质量门槛时必须标注置信度。
- `structural_missing` 品牌仍回到同一个榜单里参与排名；缺失的结构化维度按 0 计算，总分自然下压。
- 不允许把 `structural_missing` 品牌拆到榜单外，也不允许用另一套公式单独排序。

## Workflow Overview

整条链路分 8 步，前 5 步是召回，第 6 步是排序，第 7 步是质量控制，第 8 步是输出。

```text
Step 1: Query 理解
    ↓
Step 2: 确定排序策略
    ↓
Step 3: 多轮 aiapi 主召回 + 语义标注 + 品牌候选入池  <- 读 semantic-annotation.md
    ↓
Step 4: aiapi 逐品牌定向口碑调查 + 交叉验证
    ↓
Step 5: hotsell 商品样本召回 + bmc 补全 + 本地脚本链
    ↓
Step 6: 四维加权排序  <- 读 scoring-formula.md
    ↓
Step 7: 后置质量控制
    ↓
Step 8: 标准化输出  <- 读 output-template.md
```

### Step 1: Query 理解

不要调用任何 query-understanding MCP 工具。
直接使用大模型根据用户原始 query 自行判断并提取：`rank_type`、`recall_query`、`brand`、`category`、`slot`、`sort_strategy`、`category_timeliness`。

- `rank_type = product` -> 终止，改用 `product-ranking`。
- Query 理解失败 -> 保留原始 query 继续。
- 未指定平台时后续默认 `mall = 京东`。

### Step 2: 确定排序策略

优先用 Step 1 的大模型判断结果中的 `sort_strategy`，缺失时按默认映射：

- 热门/销量/热销 -> `sales desc`
- 好评/口碑 -> `reputation desc`
- 便宜/性价比 -> `avg_price asc`
- 推荐最多 -> `recommend desc`
- 无明确意图 -> `comprehensive`（综合评分）

### Step 3: 多轮 aiapi 主召回（语义标注 + 品牌候选入池）

> 执行本步前，读取 `references/semantic-annotation.md` 获取完整的标注规则和分层逻辑。

这是品牌候选的唯一主召回步骤。每轮循环：调 aiapi -> 抽品牌名 -> 语义标注 -> 前置质量控制 -> 更新候选池 -> 检查终止条件。

#### 3.1 搜索轮次

| 轮次 | query 模板 | main/video/aladdin | 触发条件 |
|------|-----------|-------------------|----------|
| 1 | `{recall_query} 品牌排行榜` | 8/2/1 | 必执行 |
| 2 | `{recall_query} 哪个牌子好` | 4/4/0 | 必执行 |
| 3 | `{recall_query} 品牌推荐` | 6/2/0 | 必执行 |
| 4 | `{category} 十大品牌` | 6/2/0 | 候选池 < 8 个品牌 |
| 5 | `{recall_query} 新锐品牌` | 4/3/0 | timeliness=high 且缺新兴品牌 |

#### 3.2 每轮处理流程

1. **穷举抽取**：从 title + sentence 中尽可能多提取品牌名（中文/英文/混合）
2. **语义标注**：对每次品牌提及标注 sentiment / mention_type / context_strength / evidence_snippet（规则见 semantic-annotation.md）
3. **归并标准化**：同品牌不同写法合并，子品牌独立保留，去噪声词
4. **前置质量控制**：
   - 粒度检查：过滤品类词、平台名、修饰词
   - 语义 noise 检查：`semantic_score < 0` 或全部 `passing` 提及 -> noise，不入池
   - 去重：合并后累加语义记录
   - 品牌归属验证（仅"国产/国货"约束时触发）

#### 3.3 候选池核心字段

每个品牌保留：`brand_name`、`brand_aliases`、`mention_count`、`positive_mention_count`、`negative_mention_count`、`recommend_count`、`criticize_count`、`semantic_score`、`source_rounds`、`candidate_tier`（stable/provisional/noise）、`mention_details[]`。

semantic_score 和 candidate_tier 的计算规则见 semantic-annotation.md。

#### 3.4 终止条件

- `stable + provisional >= 12` -> 停止
- 连续 2 轮无新增 -> 停止
- 已跑满 5 轮 -> 停止

### Step 4: aiapi 定向口碑调查 + 交叉验证

对候选池中每个品牌（stable 优先，provisional 其次），继续调 `ranking_ugc_search_aiapi` 做定向口碑调查：

- query 模板：`"{brand_name} {category} 怎么样"` 或 `"{brand_name} 品牌评价"`
- 优先拉主结果与视频结果，聚焦品牌体验、好评、踩坑、推荐等内容
- 这是定向调查步骤，不替代 Step 3 的主召回轮次

**提取信号**：`brand_positioning`、`positive_signals`（含条数）、`negative_signals`（含条数）、`target_audience`。

**交叉验证**：对比 Step 3 主召回的 aiapi semantic_score 方向与 Step 4 定向 aiapi 口碑信号方向，判定 `consistency_level`（consistent / partial / conflicting）。规则见 semantic-annotation.md。

**升降格**：口碑调查后可能触发 provisional->stable 升格、provisional->noise 降格、stable->provisional 反向降格。规则见 semantic-annotation.md。

定向 aiapi 调查失败 -> 保留候选，标记 `reputation_unknown`，`consistency_level = unknown`。

### Step 5: hotsell/bmc 结构化补全

对候选池中每个品牌执行商品样本召回与补全，本地实现要求严格走脚本链：

1. 先把候选池导出成 JSON，调用 `scripts/build_hotsell_plan.py` 冻结 stable/provisional 品牌清单
2. 严格按脚本输出的 `plan` 逐品牌调用 `ranking_hotsell_recall`，不要临场手工改品牌顺序或漏品牌
3. 每个品牌的多页 hotsell 原始结果都保留到结构化 JSON，中途不要只靠对话记忆追踪候选商品
4. 召回完成后，调用 `scripts/merge_hotsell_results.py` 合并多品牌、多页结果，按商品 ID/链接去重
5. 再调用 `scripts/check_hotsell_coverage.py` 校验 `plan` 中所有品牌都已执行完成
6. 最后对合并后的商品批量调 `ranking_bmc_detail_enrich` 补全结构化详情；识货商品补调 `ranking_sh_detail_enrich`
7. 需要时调用 `ranking_brand_normalize` 统一品牌写法，归一失败不中断

补全后每商品需具备：name、brand、price、sales_num、bmc_id/spuid；建议补齐 comment_num、shop_name。

**降级处理**：

- hotsell 召回失败 -> `sales_score = 0` 且 `market_presence = 0`
- bmc 补全失败 -> 用 hotsell 基础字段继续
- 召回商品 < 3 -> 标记"样本不足"
- 结构化补全失败或商品样本缺失 -> 将品牌标记为 `structural_missing`
- `structural_missing` 不终止链路，也不移出正式榜单；仅在后续评分中将结构化维度记为 0

### Step 6: 四维加权排序

> 执行本步前，读取 `references/scoring-formula.md` 获取完整的计算公式。

#### 6.1 聚合指标

对每个品牌计算：product_count、total_sales、avg_price、price_range、total_comments、avg_comments_per_product、category_median_sales。

#### 6.2 评分公式

```text
final_score = ugc_semantic_score × 0.30
            + reputation_score   × 0.25
            + sales_score        × 0.25
            + market_presence    × 0.20
```

各分项计算细节见 scoring-formula.md。关键设计：

- sales 用 log10 归一化，压缩头部碾压
- market_presence 的 availability 是阶梯门槛（5 个 SKU 满分），不鼓励铺货
- engagement 衡量每商品平均评论数（密度），不是评论总量

#### 6.3 代表商品

每品牌选 1-2 个：优先销量最高，有爆品（>= category_median × 3）优先选入。

#### 6.4 排序策略覆盖

用户明确指定时覆盖默认公式：`sales desc` / `avg_price asc` / `reputation desc` / `recommend desc`。

### Step 7: 后置质量控制

1. **相关性校验**：品类匹配、品牌归属匹配、平台匹配，不符合直接剔除
2. **唯一性校验**：归一后仍重复的品牌合并
3. **量级终检**：
   - `>= 8` 品牌 -> 正常输出
   - `< 8` -> 先回 Step 3 追加搜索；补救后仍不足 -> 降级输出 + 标注"置信度较低"
4. **截取 Top N**：默认 10，用户指定时按要求

### Step 8: 标准化输出

> 执行本步前，读取 `references/output-template.md` 获取完整的输出模板。

- 严格按 `references/output-template.md` 输出
- 输出采用三段式：榜单头部 -> 统一品牌列表 -> 尾部说明
- `structural_missing` 品牌仍在同一榜单中展示
- 对这类品牌使用 `⚠️ 数据不完整` 标签
- `销量得分` 与 `市场存在感` 字段显示为 `0/100 (无结构化数据)`
- `排序说明` 必须写清楚它为何还能进榜、以及为何排在当前位置

## Tool Usage Quick Reference

| 工具 | 用途 | 关键参数 |
|------|------|----------|
| `ranking_ugc_search_aiapi` | 主召回 + 语义标注 | 见 Step 3.1 各轮次参数 |
| `ranking_ugc_search_aiapi` | 定向口碑调查 | `query="{brand} {category} 怎么样"` 或 `"{brand} 品牌评价"` |
| `ranking_ugc_search_aiapi` | 品牌归属验证 | `query="{品牌} 是国产品牌吗"` |
| `ranking_hotsell_recall` | 商品样本召回 | `query="{brand} {category}", rn = 10` |
| `ranking_bmc_detail_enrich` | 批量补全 | 优先批量调用 |
| `ranking_brand_normalize` | 品牌名归一 | 归一失败不中断 |

## Configuration

```yaml
min_brand_candidate_pool: 12
max_search_rounds: 5
stop_on_no_new_rounds: 2
stable_positive_mention_threshold: 2
stable_recommend_threshold: 1
stable_semantic_score_threshold: 0
score_weights: {ugc_semantic: 0.30, reputation: 0.25, sales: 0.25, market_presence: 0.20}
sales_normalization: log10
availability_full_score_threshold: 5
hotsell_rn_per_brand: 10
default_top_n: 10
min_list_size: 8
default_mall: 京东
```
