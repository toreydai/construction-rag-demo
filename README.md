# 建筑工程施工方案 RAG 助手

施工企业知识库问答系统。输入方案编制问题，从历史施工方案、现行规范、商务数据中检索并生成带引用来源的答案；多来源存在分歧时触发**矛盾分析模式**，置信度不足时**主动拒答**。

## 场景背景

施工企业编制方案时，最大的痛点不是"查规范"——规范条文有限，有经验的工程师基本能背。真正难的是**找到历史上地质条件、基坑深度、周边环境相似的参考项目**，看那些项目的关键参数怎么取的（嵌固深度、降水方案、支撑体系），以及历史造价水平是多少。

这个过程目前全靠"问老同事"、"翻硬盘"、"凭记忆"，知识既难找又无法沉淀。

本 Demo 基于 19 份模拟施工资料（历史方案 12 份、现行规范 4 份、商务数据 3 份）构建知识库，覆盖三类角色需求：

| 角色 | 典型问题 |
|------|---------|
| 总工 | 找类似地质条件的深基坑支护案例 |
| 技术员 | 规范与历史项目实际做法有出入，以哪个为准？|
| 商务 | 历史项目的地连墙造价是多少，我们的报价合理吗？|

数据中刻意埋入 **8 个矛盾点**（同一参数在不同项目/规范中取值不同），用于展示系统的多源分析与矛盾识别能力。

## 访问地址

部署后从 CDK 输出获取（ALB DNS）

## 技术栈

- **检索**：Amazon OpenSearch（向量 kNN + smartcn BM25 + 应用层 RRF）
- **精排**：Amazon Rerank v1（us-west-2）→ RRF fallback
- **生成**：Kimi K2.5（`moonshotai.kimi-k2.5`，Bedrock，流式，max_tokens=4096）
- **Embedding**：Titan Text Embeddings v2（1024 维）
- **前端**：Streamlit，ALB 公网访问

## 前端功能

| 功能 | 说明 |
|------|------|
| 文档类型筛选 | 只从历史方案/规范/商务数据中检索 |
| Rerank 开关 | 可关闭对比纯 RRF 效果 |
| TOP_M 滑块（4-16）| 调节送入 LLM 的 chunk 数 |
| 🎲 随机抽题 | 随机触发演示问题 |
| 📥 导出对话 | 下载 Markdown 格式对话记录 |
| 检索管线可视化 | 向量/BM25/RRF/Rerank 4 个 tab |
| 矛盾双列高亮 | 冲突来源蓝橙对比展示 |
| 置信度进度条 | Rerank top1 分数可视化 |

## 快速开始（本地开发）

```bash
export OPENSEARCH_HOST=<CDK 输出的 OpenSearch endpoint>
export AWS_DEFAULT_REGION=us-east-1

cd rag-app
pip3 install -r requirements.txt

# 建索引（首次 or 数据更新后）
python3 scripts/convert_xlsx.py  # 商务数据 xlsx → txt（首次必须）
python3 ingest/chunk.py
python3 ingest/index.py

# 启动
streamlit run app/main.py --server.port 8501 --server.address 0.0.0.0 \
  --server.enableCORS false --server.enableXsrfProtection false
```

## 评测

```bash
python3 eval/eval.py          # 完整评测（生成 + 评分）
python3 eval/calibrate_tau.py # 拒答阈值标定
```

**当前评测结果**（2026-06-22，模拟数据）：在库题均分 **8.5/10**，库外拒答 **5/5**。

## 文档

| 文件 | 内容 |
|------|------|
| [docs/step-by-step.md](docs/step-by-step.md) | 面向非技术读者的搭建操作手册（7 步）|
| [docs/lab-guide.md](docs/lab-guide.md) | 客户操作实验手册（6 个实验）|
| [docs/architecture.md](docs/architecture.md) | 架构设计 + 基础设施 + 配置参数 |
| [docs/deployment.md](docs/deployment.md) | 部署 + 运维 + 数据接入 |
| [docs/evaluation.md](docs/evaluation.md) | 评测结果 + 逐题得分 + 调优历史 |

## License

MIT - see the [LICENSE](LICENSE) file for details.

## 免责声明

本项目仅供学习与技术参考，不构成生产部署方案。运行过程中会创建 AWS 资源并调用 Amazon Bedrock 模型，产生费用，请在实验结束后及时清理。作者不对因使用本项目产生的任何费用或损失承担责任。本项目与 Amazon Web Services 无官方关联，相关服务的可用性与定价以 AWS 官方文档为准。知识库文件为模拟数据，不代表真实施工规范；生产环境使用前请根据实际需求进行安全评估与调整。
