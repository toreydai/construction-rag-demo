# 架构与配置

> 版本 v1.2 | 2026-06-22
> 基于 [airline-ticketing-rag-assistant](https://github.com/toreydai/airline-ticketing-rag-assistant) 检索管线改造，替换知识域为建筑工程施工方案。

## 目标

施工企业总工、技术员、商务人员输入方案编制问题 → **混合检索**（向量 + 中文 BM25 + RRF + Rerank 级联）召回最相关施工方案片段 → **Kimi K2.5** 流式生成带引用来源的答案；多来源存在分歧时触发**矛盾分析模式**，置信度不足时**主动拒答**。

## 架构图

```
                 ┌──────────────────────────────────────────────────────┐
                 │  Streamlit 前端（EC2 t3.small → ALB port 80）          │
                 │  · 快捷问题面板（L1/L2/L3）+ 随机抽题 + 对话导出        │
                 │  · 文档类型筛选 / Rerank 开关 / TOP_M 滑块             │
                 │  · 检索管线可视化（向量/BM25/RRF/Rerank 4 tab）         │
                 │  · 矛盾双列高亮 + 置信度进度条 + 引用来源面板            │
                 └─────────────────────┬────────────────────────────────┘
                                       │ boto3
        ┌──────────────────────────────┼─────────────────────────────────┐
        ▼                              ▼                                 ▼
┌───────────────┐            ┌──────────────────┐             ┌──────────────────┐
│ Bedrock        │            │ Amazon OpenSearch │             │ Bedrock           │
│ Titan Embed v2 │─ 查询向量 ─▶│  向量 kNN 索引    │             │ Kimi K2.5         │
│ (向量化)       │            │  + 中文 BM25 索引  │             │ (流式生成+矛盾分析) │
└───────────────┘            └────────┬─────────┘             └──────────────────┘
                                      │ 两路各 top-20
                                      ▼
                          ┌─────────────────────────────────────┐
                          │ 应用层 RRF 融合 → top-10             │
                          │ → Bedrock Rerank 精排（us-west-2）   │
                          │   Amazon Rerank v1                  │
                          │   → RRF fallback                    │
                          │ → 矛盾信号检测（rerank gap 分析）      │
                          └─────────────────────────────────────┘
```

**在线数据流：**

1. 用户提问 → 追问词检测 → 必要时 Kimi 改写为独立完整问题
2. Bedrock Titan Embed v2 将查询向量化（1024 维）
3. OpenSearch 同时跑**向量 kNN 召回**（top-20）和**中文 BM25 词法召回**（top-20）
4. 应用层 **RRF 融合** → top-10 → Bedrock Rerank 精排 → top-8
5. **矛盾信号检测**：top1 与 top2 rerank score gap 小 → 切换矛盾分析模式
6. top-8 片段 + System Prompt → Kimi K2.5 **流式生成**，每条结论带【文件名】来源标注
7. Rerank top1 低于 `TAU_ABS` → **拒答**，不生成内容

## 技术选型

| 组件 | 选型 | 说明 |
|------|------|------|
| 向量 + 词法库 | Amazon OpenSearch（kNN + smartcn BM25）| 双索引共存，融合逻辑可见可调 |
| Embedding | Bedrock Titan Text Embeddings v2（1024 维）| 零运维，中文技术文本效果稳定 |
| 融合 | 应用层 RRF（k=60）| 分数尺度无关 |
| Rerank | Amazon Rerank v1（us-west-2）→ RRF fallback | amazon.rerank-v1:0 仅在 us-west-2/ap-northeast-1 上线，跨区调用 |
| 矛盾检测 | rerank gap 分析 | gap 小 = 多来源分歧，触发对比分析 |
| 生成 LLM | Kimi K2.5（moonshotai.kimi-k2.5）| OpenAI 兼容格式，支持流式，max_tokens=4096 |
| 多轮对话 | 应用层 query 改写（Kimi 短调用）| 追问词/短句改写为独立完整问题 |
| 前端 | Streamlit | 检索管线可视化 + 矛盾高亮 + 参数调节 |
| IaC | AWS CDK（Python）| OpenSearch + EC2 + ALB 声明式一键部署 |

## 基础设施（CDK RagInfraStack）

| 资源 | 规格 | 说明 |
|------|------|------|
| OpenSearch | t3.medium.search，30GB GP3，2.9 | 索引 `construction-rag`，80 chunks |
| EC2 | t3.small，Amazon Linux 2023 | 运行 Streamlit，无 UserData，SSM 管理 |
| ALB | internet-facing，port 80→EC2:8501 | 公网访问入口 |
| IAM Role | SSM + Bedrock + OpenSearch + S3ReadOnly | EC2 实例角色 |

**关键端点：**

| 项目 | 值 |
|------|---|
| ALB 访问地址 | 部署后从 CDK 输出获取（ALB DNS）|
| OpenSearch Host | `$OPENSEARCH_HOST`（部署后从 CDK 输出获取）|
| EC2 Instance ID | 部署后从 CDK 输出获取 |

## 知识库设计

### 文档分类

| 类别 | 文件编号 | 格式 | 数量 | 内容 |
|------|---------|------|------|------|
| 历史方案 | A01–A12 | PDF / DOCX | 12 | 施工方案、专家论证纪要、技术交底 |
| 现行规范 | B01–B04 | PDF / DOCX | 4 | JGJ120、GB50497、JGJ94、GB50204 |
| 商务数据 | C01–C03 | TXT（xlsx 预处理）| 3 | 基坑 / 桩基 / 地基处理造价参考 |

### 差异化 Chunking

| 数据源 | Chunk 大小 | 重叠 | 原因 |
|--------|-----------|------|------|
| 历史方案（A）| 600 字 | 200 字 | 大重叠保留上下文，避免参数跨 chunk 断裂 |
| 现行规范（B）| 350 字 | 120 字 | 按条文粒度，重叠 120 防参数断裂 |
| 商务数据（C）| 250 字 | 50 字 | 结构化表格，每条价格数据短 |

### 矛盾点设计（刻意埋入）

| 矛盾 | 涉及文件 | 差异说明 |
|------|---------|---------|
| 嵌固深度 | B01 vs A01 vs A07 vs A10 | 规范底线 0.5H，实践 0.6~1.04H，专家建议 0.8H |
| 泥浆密度 | A09 vs A10 | PP 项目 ≤1.08，QQ 项目 1.15~1.25 |
| 成孔工艺 | A05 vs A08 vs A06 | 泥岩旋挖+冲击，灰岩全套管+冲击 |
| 检测比例 | B03 vs A08 vs A12 | 规范 ≥30%，专家论证→100%，实践 100% |
| 接头形式 | A01 vs A09 vs A10 | 工字钢 vs H 型钢+CWS vs 十字钢板 |

## 检索与 Rerank

### Rerank 级联逻辑

```
Amazon Rerank v1 (us-west-2)
  ├── 成功 → check_signals(top1, top2)
  │         ├── top1 < TAU_ABS  → 拒答
  │         ├── gap < TAU_GAP   → 矛盾分析模式
  │         └── 正常            → 生成
  └── 失败 → RRF fallback + BM25_FALLBACK_FLOOR 拒答
```

> `amazon.rerank-v1:0` 在 us-east-1 未上线，`retrieval/rerank.py` 为其单独创建 us-west-2 client。

### 幻觉防护三层

| 层 | 机制 |
|----|------|
| 检索闸 | BM25_FALLBACK_FLOOR / TAU_ABS 拒答低置信度召回 |
| Prompt 闸 | 强约束"只依据片段 + 必须标来源 + 允许拒答 + 禁止推断数字" |
| 展示闸 | 前端置信度进度条 + rerank score，演示时可解释可信度 |

## 配置参数（config.py）

### 连接

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `OPENSEARCH_HOST` | localhost | 环境变量 OPENSEARCH_HOST |
| `OPENSEARCH_INDEX` | construction-rag | 索引名 |
| `AWS_REGION` | us-east-1 | 主 Region |

### 模型

| 参数 | 当前值 | 说明 |
|------|--------|------|
| `EMBEDDING_MODEL` | amazon.titan-embed-text-v2:0 | 更换后**必须全量重建索引** |
| `EMBEDDING_DIM` | 1024 | Titan v2 支持 256/512/1024 |
| `RERANK_MODEL_ARN_AMAZON` | arn:aws:bedrock:us-west-2::foundation-model/amazon.rerank-v1:0 | **固定 us-west-2** |
| `RERANK_REGION_AMAZON` | us-west-2 | Amazon Rerank 专用 Region |
| `GENERATION_MODEL` | moonshotai.kimi-k2.5 | Bedrock 上的 Kimi，OpenAI 兼容 |

### 检索与阈值

| 参数 | 当前值 | 说明 |
|------|--------|------|
| `K_VECTOR` / `K_BM25` | 20 / 20 | 两路各召回 top-20 |
| `TOP_N` | 10 | RRF 后送 Rerank 的候选数 |
| `TOP_M` | 8 | 最终送生成层的片段数（前端可覆盖）|
| `TAU_ABS` | 0.1 | Rerank top1 低于此值拒答（calibrate_tau 标定，F1=0.966）|
| `TAU_GAP` | 0.15 | (top1-top2)/top1 低于此值触发矛盾分析 |
| `BM25_FALLBACK_FLOOR` | 8.0 | Fallback 拒答阈值（calibrate_tau 标定，F1=0.938）|

> 修改 EMBEDDING_MODEL / EMBEDDING_DIM / OPENSEARCH_INDEX 后需**全量重建索引**；其余参数重启应用即生效。

## 目录结构

```
rag-app/
├── config.py              全局配置
├── sampledata/            示例数据（19 份模拟施工资料）
│   ├── 历史方案/           A01–A12（PDF / DOCX）
│   ├── 现行规范/           B01–B04（PDF / DOCX）
│   └── 商务数据/           C01–C03（xlsx 原文件 + 转换后 txt）
├── scripts/
│   └── convert_xlsx.py    商务数据 xlsx → txt 转换工具
├── ingest/
│   ├── chunk.py           差异化分块，输出 chunks.jsonl
│   └── index.py           向量化 + 写入 OpenSearch（幂等 upsert）
├── retrieval/
│   ├── hybrid.py          向量 kNN + 中文 BM25 两路召回（支持文档类型筛选）
│   ├── fusion.py          应用层 RRF 融合
│   └── rerank.py          Rerank 级联 + 矛盾信号检测 + 拒答判定
├── app/
│   └── main.py            Streamlit 前端
├── eval/
│   ├── golden_set.jsonl   15 道在库题 + 5 道库外题
│   ├── eval.py            生成回答 + LLM-as-judge 评分
│   └── calibrate_tau.py   拒答阈值标定
├── infra/
│   ├── stack.py           CDK：OpenSearch + EC2 + ALB
│   ├── app.py             CDK 入口（RagInfraStack）
│   └── cdk.json           CDK 配置
└── docs/
    ├── step-by-step.md    面向非技术读者的搭建操作手册
    ├── architecture.md    本文档（架构 + 配置）
    ├── runbook.md         部署 + 运维 + 数据接入
    ├── evaluation.md      评测结果与调优
    └── lab-guide.md       客户操作实验手册
```

## 已知限制

| 限制 | 状态 | 说明 |
|------|------|------|
| Amazon Rerank 跨区调用 | ✅ 已解决 | 固定使用 us-west-2，延迟增加约 50ms |
| 知识库为模拟数据 | 已知 | 19 份模拟文件；接入真实方案库后重新评测 |
| DOCX 表格解析 | 有限 | python-docx 提取表格为文本行，行列语义依赖文本顺序 |
| Python 版本 | EC2 为 3.9 | 代码需保留 `from __future__ import annotations` |
