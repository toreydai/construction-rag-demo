# 运维手册

## 环境信息

| 项目 | 值 |
|------|---|
| ALB 访问地址 | 部署后从 CDK 输出获取（ALB DNS）|
| OpenSearch Host | 部署后从 CDK 输出获取，设置为 `OPENSEARCH_HOST` 环境变量 |
| CDK Stack | `RagInfraStack`（us-east-1）|

## 服务配置

生产环境由 systemd 服务 `construction-rag.service` 管理。服务单元位于
`/etc/systemd/system/construction-rag.service`，其中必须配置以下环境变量：

```ini
Environment=OPENSEARCH_HOST=<CDK 输出的 OpenSearch endpoint，不含 https://>
Environment=OPENSEARCH_PORT=443
Environment=AWS_DEFAULT_REGION=us-east-1
```

修改服务单元后，执行 `sudo systemctl daemon-reload && sudo systemctl restart construction-rag`。

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

### 安装 / 启动

```bash
sudo systemctl enable --now construction-rag
```

服务单元的 `ExecStart` 必须保留 `--server.enableCORS false` 和
`--server.enableXsrfProtection false`，否则 ALB 反向代理的 WebSocket 可能无法建立。

### 状态 / 重启

```bash
sudo systemctl status construction-rag
sudo systemctl restart construction-rag
sudo journalctl -u construction-rag -f
curl -fsS http://localhost:8501/_stcore/health  # 输出 ok = 正常
```

服务已设置为开机自启，并在非正常退出后 5 秒自动重启。

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
    "sudo systemctl restart construction-rag",
    "sudo systemctl is-active --quiet construction-rag",
    "curl -fsS http://localhost:8501/_stcore/health"
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

**OpenSearch 连接失败** — 检查 `sudo systemctl show construction-rag -p Environment`；确认 `OPENSEARCH_HOST` 为 CDK 输出的域名（不含 `https://`），然后执行 `sudo systemctl restart construction-rag`。
