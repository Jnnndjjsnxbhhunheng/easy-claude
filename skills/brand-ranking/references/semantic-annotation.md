# 语义标注规则与候选分层

本文件包含 Step 3（aiapi 主召回）和 Step 4（aiapi 定向交叉验证）中需要的全部语义相关规则。

## 1. 语义标注维度

对每次品牌提及（一条 aiapi 结果中出现一个品牌名 = 一次提及），标注四个字段：

### sentiment

判定该条内容对该品牌的情感倾向。

| 值 | 判定规则 |
|----|----------|
| `positive` | 出现"推荐""好用""首选""性价比高""口碑好""热销""必买""好评"等正面信号词，且语境指向该品牌 |
| `negative` | 出现"踩坑""不推荐""翻车""质量差""售后差""智商税""避雷"等负面信号词，且语境指向该品牌 |
| `neutral` | 品牌被提及但无明确正负面倾向（如"A主打高端，B主打性价比"） |
| `ambiguous` | 正负面信号混合或无法清晰判断 |

### mention_type

判定该品牌在内容中的角色。

| 值 | 判定规则 |
|----|----------|
| `recommend` | 明确列为推荐选项，使用"推荐""首选""闭眼入"等推荐语 |
| `compare` | 作为多品牌对比中的一方 |
| `criticize` | 明确批评或警告避开 |
| `passing` | 仅出现在列表、导航、背景描述中，无实质讨论 |

### context_strength

判定该品牌在该条内容中被讨论的深度。

| 值 | 判定规则 |
|----|----------|
| `strong` | 标题含该品牌名，或 sentence 中用 2 句以上讨论该品牌 |
| `medium` | sentence 中有 1 句实质讨论（非列举） |
| `weak` | 仅出现在列表枚举或一笔带过 |

### evidence_snippet

从 title + sentence 中截取支撑判断的原文片段，不超过 50 字。

## 2. 标注注意事项

- **否定句检测**：扫描"不建议""不推荐""别买""不要选"等否定词 + 品牌名组合 → `criticize` + `negative`
- **反讽检测**：引号包裹的正面词（如"好"）→ 优先标为 `ambiguous`
- **中立对比**："各有优劣"类评价 → `neutral` + `compare`，不计入正负面
- **内容极短**（title 3 字 + sentence 为空）→ `passing` + `neutral`，不贡献正负面分

## 3. semantic_score 计算

```
semantic_score = positive_weighted_sum - negative_weighted_sum

positive_weighted_sum = Σ (每条 sentiment=positive 提及的 context_weight × type_weight)
negative_weighted_sum = Σ (每条 sentiment=negative 提及的 context_weight × type_weight)
```

neutral 和 ambiguous 提及不计入（既不加分也不减分）。

**权重表：**

| context_strength | context_weight |
|-----------------|---------------|
| strong | 1.0 |
| medium | 0.6 |
| weak | 0.2 |

| mention_type | type_weight |
|-------------|------------|
| recommend | 1.5 |
| compare | 0.8 |
| criticize | 1.5 |
| passing | 0.3 |

**示例：**

品牌 A（3 次提及）：
- recommend + strong + positive → +1.5 × 1.0 = +1.5
- compare + medium + neutral → 不计入
- criticize + strong + negative → -1.5 × 1.0 = -1.5
- **semantic_score = 0.0**

品牌 B（2 次提及）：
- recommend + strong + positive → +1.5
- recommend + medium + positive → +1.5 × 0.6 = +0.9
- **semantic_score = 2.4**（虽然 mention_count 更低，但语义质量更高）

品牌 C（4 次提及）：
- criticize + strong + negative → -1.5
- criticize + medium + negative → -0.9
- passing + weak + neutral → 不计入
- compare + medium + negative → -0.48
- **semantic_score = -2.88** → 进入 noise

## 4. candidate_tier 分层

### stable（全部满足）

- 品牌名清晰可识别
- `positive_mention_count >= 2`
- `recommend_count >= 1`
- `source_rounds >= 2`
- `semantic_score > 0`

### provisional（满足任一）

- `positive_mention_count = 1` 且 `semantic_score > 0`
- `mention_count >= 2` 但 `recommend_count = 0`
- `semantic_score = 0` 但 `mention_count >= 3`（正负抵消）
- 品牌名存在歧义

### noise（满足任一）

- `semantic_score < 0`
- `mention_count >= 2` 且全部 `mention_type = criticize`
- 明确是品类词、平台名、修饰词
- 所有提及均为 `passing`
- 无法判断是否为品牌实体

noise 在 Step 3 结束后从候选池移除，不进入 Step 4/5。

**例外**：后续轮次中同一品牌被重新提及且 `positive_mention_count >= 2` 且 `semantic_score > 0`，允许从 noise 重入 provisional。

## 5. 升降格规则

### provisional → stable（Step 4 口碑调查后触发）

满足任一：
- Step 4 定向 aiapi 返回正面口碑证据 >= 2 条，且无集中负面
- hotsell_recall 召回 >= 3 个商品，且 `semantic_score > 0`
- Step 3 aiapi + Step 4 aiapi 合并后 `positive_mention_count` 达到 stable 门槛

### provisional → noise

满足任一：
- Step 4 定向 aiapi 返回集中负面（negative > positive × 2）
- hotsell 召回 0 + `semantic_score <= 0`
- Step 3 aiapi + Step 4 aiapi 合并后 `semantic_score` 仍 < 0

### stable → provisional（反向降格）

触发条件：Step 3 aiapi `semantic_score >= 2.0`，但 Step 4 定向 aiapi 返回集中负面证据（`consistency_level = conflicting`）。降为 provisional，最终排序通过 `consistency_bonus = 0` 下调。

## 6. 交叉验证（Step 4）

### 判定 aiapi 方向

- `semantic_score > 1.0` → 偏正面
- `-1.0 <= semantic_score <= 1.0` → 中性
- `semantic_score < -1.0` → 偏负面

### 判定 Step 4 定向 aiapi 方向

- `positive_signals_count > negative_signals_count × 2` → 偏正面
- `negative_signals_count > positive_signals_count × 2` → 偏负面
- 其他 → 中性

### consistency_level

| Step 3 aiapi | Step 4 aiapi | 结果 |
|--------------|--------------|------|
| 同方向 | 同方向 | `consistent` |
| 一方中性 | 另一方有方向 | `partial` |
| 方向矛盾 | | `conflicting` |

Step 4 定向 aiapi 调查失败 → `consistency_level = unknown`。
