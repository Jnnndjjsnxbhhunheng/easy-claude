# Scoring Formula

## Core Rule

`structural_missing` 品牌仍然进入同一个正式榜单参与排名，不单独拆出观察名单，也不禁止与完整品牌混排。

硬规则如下：

- 仍使用同一套综合公式计算 `final_score`
- 若品牌缺失结构化数据，则：
  - `sales_score = 0`
  - `market_presence = 0`
- 因为质量维度权重为 55%，所以 `structural_missing` 品牌的理论总分上限为 `55`
- 不允许因为结构化缺失而直接剔除品牌；应让总分自然下压并在输出中解释原因

## Unified Formula

```text
final_score = ugc_semantic_score × 0.30
            + reputation_score   × 0.25
            + sales_score        × 0.25
            + market_presence    × 0.20
```

## Aggregated Inputs

进入评分前，每个品牌至少应聚合出以下字段：

- `product_count`
- `total_sales`
- `avg_price`
- `price_range`
- `total_comments`
- `avg_comments_per_product`
- `category_median_sales`

其中：

- `sales_score` 建议基于 `total_sales` 做 `log10` 归一化，避免头部品牌绝对销量碾压
- `market_presence` 由 `availability` 与 `engagement` 共同构成
- `availability` 采用阶梯门槛，默认 5 个有效 SKU 记满分
- `engagement` 衡量每商品平均评论密度，不直接使用评论总量

## Structural Missing Handling

当品牌被标记为 `structural_missing` 时：

```text
sales_score = 0
market_presence = 0
final_score = ugc_semantic_score × 0.30 + reputation_score × 0.25
```

示例：

```text
ugc_semantic_score = 72
reputation_score   = 68
sales_score        = 0
market_presence    = 0

final_score = 0.30×72 + 0.25×68 + 0.25×0 + 0.20×0
            = 21.6 + 17.0
            = 38.6
```

## Output Requirements

对 `structural_missing` 品牌：

- 仍显示在统一品牌榜单中
- `销量得分` 显示为 `0/100 (无结构化数据)`
- `市场存在感` 显示为 `0/100 (无结构化数据)`
- `排序说明` 必须明确解释：该品牌为什么还能上榜、又为什么排在当前位置

## Sort Strategy Overrides

当用户明确指定排序意图时，允许覆盖综合公式的最终排序顺序：

- `sales desc`：按销量相关指标优先排序
- `avg_price asc`：按平均价格升序排序
- `reputation desc`：按口碑得分优先排序
- `recommend desc`：按推荐相关信号优先排序

若用户没有明确指定，则默认使用 `final_score` 综合排序。

## Tie Breaking

当两个品牌 `final_score` 接近或相同时，按以下顺序决胜：

1. `ugc_semantic_score` 更高者优先
2. `reputation_score` 更高者优先
3. `sales_score` 更高者优先
4. `market_presence` 更高者优先
5. 若仍相同，优先保留代表商品质量更稳定、样本更完整的品牌

## Prohibited Handling

以下做法都不允许：

- 将 `structural_missing` 品牌移出正式榜单
- 单独拆成“观察名单”且不参与排序
- 使用不同公式给 `structural_missing` 品牌打分
- 用 UGC/口碑信号替代 `sales_score` 或 `market_presence`
