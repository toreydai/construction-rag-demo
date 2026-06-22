# 运维手册

## 环境信息

| 项目 | 值 |
|------|---|
| ALB 访问地址 | 部署后从 CDK 输出获取（ALB DNS）|
| OpenSearch Host | 部署后从 CDK 输出获取，设置为 `OPENSEARCH_HOST` 环境变量 |
| CDK Stack | `RagInfraStack`（us-east-1）|

## 环境变量

```bash
export OPENSEARCH_HOST=<CDK 输出的 OpenSearch endpoint>
export AWS_DEFAULT_REGION=us-east-1
```

---

## 部署

```bash
cd infra
cdk deploy RagInfraStack --require-approval never
```

> ⚠️ `cdk destroy` 会删除 OpenSearch 域，索引数据全部丢失。

---

## SSM 登录 EC2

```bash
aws ssm start-session --target <INSTANCE_ID> --region us-east-1
```

---

## Streamlit 管理

### 启动

```bash
export OPENSEARCH_HOST=<OpenSearch endpoint>
export AWS_DEFAULT_REGION=us-east-1
cd /opt/rag-app
nohup streamlit run app/main.py \
  --server.port 8501 --server.address 0.0.0.0 \
  --server.enableCORS false --server.enableXsrfProtection false \
  > /tmp/streamlit.log 2>&1 &
```

> `--enableCORS false --enableXsrfProtection false` 是 ALB 反向代理必须加的参数，否则 WebSocket 连接失败导致页面一直转圈。

### 状态 / 重启

```bash
curl -s http://localhost:8501/_stcore/health  # 200 = 正常
tail -20 /tmp/streamlit.log
pkill -f streamlit                            # 停止
```

---

## 代码更新

小改动直接 SSM patch，无需重新打包：

```bash
aws ssm send-command \
  --instance-ids <INSTANCE_ID> \
  --document-name "AWS-RunShellScript" \
  --region us-east-1 \
  --parameters '{"commands":[
    "sed -i \"s/TAU_ABS  = 0.1/TAU_ABS  = 0.2/\" /opt/rag-app/config.py",
    "pkill -f streamlit || true",
    "sleep 2",
    "export OPENSEARCH_HOST=<OpenSearch endpoint> && export AWS_DEFAULT_REGION=us-east-1 && cd /opt/rag-app && nohup streamlit run app/main.py --server.port 8501 --server.address 0.0.0.0 --server.enableCORS false --server.enableXsrfProtection false > /tmp/streamlit.log 2>&1 &"
  ]}'
```

---

## 索引管理

```bash
# 建索引（首次 or 数据更新后）
python3 ingest/chunk.py   # 差异化分块 → 82 chunks
python3 ingest/index.py   # 向量化 + 写入 OpenSearch（约 5 分钟）

# 全量重建
python3 -c "
import sys; sys.path.insert(0, '.')
import config
from ingest.index import get_clients
os_client, _ = get_clients()
os_client.indices.delete(index=config.OPENSEARCH_INDEX, ignore=[400, 404])
"
python3 ingest/chunk.py && python3 ingest/index.py
```

---

## 知识库更新

```
upload/
├── 历史方案/     ← PDF / DOCX
├── 现行规范/     ← PDF / DOCX
└── 商务数据/     ← TXT（xlsx 需先运行 python3 scripts/convert_xlsx.py）
```

新文件放入对应目录后：

```bash
python3 ingest/chunk.py && python3 ingest/index.py
python3 eval/eval.py          # 评测
python3 eval/calibrate_tau.py # 重新标定阈值，更新 config.py
```

---

## 常见问题

**Streamlit 浏览器一直转圈** — 启动命令缺少 `--server.enableCORS false --server.enableXsrfProtection false`。

**Amazon Rerank ValidationException** — `amazon.rerank-v1:0` 在 us-east-1 未上线，确认 `config.RERANK_REGION_AMAZON=us-west-2`。

**BM25 召回全部 0 分** — 索引建立时 smartcn 未生效，执行全量重建。

**OpenSearch 连接失败** — 检查 `echo $OPENSEARCH_HOST` 是否已设置。
