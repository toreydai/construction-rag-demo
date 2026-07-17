"""
创建 OpenSearch 索引并幂等 upsert 所有 chunks。
先运行 chunk.py，再运行本脚本。
用法：python3 ingest/index.py
"""
import json
import sys
from pathlib import Path

import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import config

INDEX_BODY = {
    "settings": {
        "index.knn": True,
        "index.knn.algo_param.ef_search": 512,
        "analysis": {
            "analyzer": {
                "zh": {"type": "smartcn"}
            }
        },
    },
    "mappings": {
        "properties": {
            "chunk_id": {"type": "keyword"},
            "source":   {"type": "keyword"},
            "doc_type": {"type": "keyword"},
            "text":     {"type": "text", "analyzer": "zh"},
            "vector":   {
                "type": "knn_vector",
                "dimension": config.EMBEDDING_DIM,
                "method": {
                    "name": "hnsw",
                    "space_type": "cosinesimil",
                    "engine": "nmslib",
                },
            },
        }
    },
}


def get_clients():
    credentials = boto3.Session().get_credentials()
    auth = AWS4Auth(region=config.AWS_REGION, service="es",
                    refreshable_credentials=credentials)
    os_client = OpenSearch(
        hosts=[{"host": config.OPENSEARCH_HOST, "port": config.OPENSEARCH_PORT}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=30,
    )
    bedrock = boto3.client("bedrock-runtime", region_name=config.AWS_REGION)
    return os_client, bedrock


def embed(bedrock, text: str) -> list[float]:
    body = json.dumps({"inputText": text, "dimensions": config.EMBEDDING_DIM, "normalize": True})
    resp = bedrock.invoke_model(modelId=config.EMBEDDING_MODEL, body=body)
    return json.loads(resp["body"].read())["embedding"]


def ensure_index(os_client: OpenSearch):
    if not os_client.indices.exists(index=config.OPENSEARCH_INDEX):
        os_client.indices.create(index=config.OPENSEARCH_INDEX, body=INDEX_BODY)
        print(f"✓ 索引 {config.OPENSEARCH_INDEX} 已创建")
    else:
        print(f"  索引 {config.OPENSEARCH_INDEX} 已存在")


def main():
    chunks_path = ROOT / "ingest" / "chunks.jsonl"
    if not chunks_path.exists():
        print("chunks.jsonl 不存在，请先运行 chunk.py")
        sys.exit(1)

    chunks = [json.loads(l) for l in chunks_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"共 {len(chunks)} chunks，开始向量化并写入索引...")

    os_client, bedrock = get_clients()
    ensure_index(os_client)

    for i, chunk in enumerate(chunks, 1):
        print(f"  [{i}/{len(chunks)}] {chunk['chunk_id']}", flush=True)
        vector = embed(bedrock, chunk["text"])
        os_client.index(
            index=config.OPENSEARCH_INDEX,
            id=chunk["chunk_id"],
            body={**chunk, "vector": vector},
        )

    print(f"\n✓ 完成：{len(chunks)} chunks 已写入 {config.OPENSEARCH_INDEX}")


if __name__ == "__main__":
    main()
