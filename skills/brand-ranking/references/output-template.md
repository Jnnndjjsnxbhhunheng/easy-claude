# Output Template

输出使用三段式结构：榜单头部 → 统一品牌列表 → 尾部说明。

## 1. 榜单头部

```text
## {年份/时间范围}{品类}品牌榜 TOP{N}（{mall}）

- **排序依据**：{排序依据说明}
- **数据来源**：UGC 语义分析(aiapi) + 定向口碑调查(aiapi) + 结构化补全({mall})
- **统计时间**：{统计日期}
- **样本量**：共召回 {total_products} 个商品，覆盖 {brand_count} 个品牌
- **置信度**：{高/中/低}
```

## 2. 统一品牌列表

所有品牌都在同一个正式榜单中输出，包括 `structural_missing` 品牌。

单个品牌模板：

```text
### {排名}. {品牌名} {标签}

| 字段 | 值 |
|------|-----|
| 综合得分 | {final_score} |
| UGC 语义得分 | {ugc_semantic_score}/100 |
| 口碑得分 | {reputation_score}/100 |
| 销量得分 | {sales_score_display} |
| 市场存在感 | {market_presence_display} |
| 商品数 | {product_count_display} |
| 总销量 | {total_sales_display} |
| 平均价格 | {avg_price_display} |
| 价格带 | {price_range_display} |
| 推荐次数 | {recommend_count_display} |
| 品牌定位 | {brand_positioning_display} |

**品牌画像**：{brand_summary}

**代表商品**：
- {example_product_1}
- {example_product_2}

**排序说明**：{ranking_reason}
```

## Structural Missing Labeling

当品牌为 `structural_missing` 时：

- 品牌标题加标签：`⚠️ 数据不完整`
- 字段显示必须写成：
  - `销量得分 | 0/100 (无结构化数据)`
  - `市场存在感 | 0/100 (无结构化数据)`
- `排序说明` 必须写清楚：
  - 该品牌为什么仍能进入同一榜单
  - 又为什么因为两个维度记 0 而排在这个位置

## 3. 尾部说明

```text
---

### 说明

- **数据来源**：商品信息来自 {mall}，内容热度与口碑基于全网 UGC 搜索与语义分析
- **统计口径**：品牌聚合基于召回的 {total_products} 个商品样本，样本量影响统计精度
- **排序方法**：综合评分 = UGC语义得分(30%) + 口碑得分(25%) + 销量得分(25%) + 市场存在感(20%)
- **结构化缺失处理**：`structural_missing` 品牌仍参与同一榜单排名，但销量得分与市场存在感按 0 计算，因此总分会自然下压
- **免责声明**：价格与库存可能实时变动，请以平台实际页面为准
{optional_warnings}
```
