"""向量 kNN + 中文 BM25 两路召回，支持文档类型筛选"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import config

_clients: dict = {}


def _get_clients():
    if not _clients:
        creds = boto3.Session().get_credentials().get_frozen_credentials()
        auth = AWS4Auth(creds.access_key, creds.secret_key,
                        config.AWS_REGION, "es", session_token=creds.token)
        _clients["os"] = OpenSearch(
            hosts=[{"host": config.OPENSEARCH_HOST, "port": config.OPENSEARCH_PORT}],
            http_auth=auth,
            use_ssl=True,
            verify_certs=True,
            connection_class=RequestsHttpConnection,
            timeout=20,
        )
        _clients["bedrock"] = boto3.client("bedrock-runtime", region_name=config.AWS_REGION)
    return _clients["os"], _clients["bedrock"]


def embed_query(bedrock, query: str) -> list[float]:
    body = json.dumps({"inputText": query, "dimensions": config.EMBEDDING_DIM, "normalize": True})
    resp = bedrock.invoke_model(modelId=config.EMBEDDING_MODEL, body=body)
    return json.loads(resp["body"].read())["embedding"]


def _doc_type_filter(doc_types: list[str] | None) -> dict | None:
    if not doc_types or len(doc_types) == 3:
        return None
    return {"terms": {"doc_type": doc_types}}


def vector_search(os_client: OpenSearch, vector: list[float],
                  k: int = config.K_VECTOR,
                  doc_types: list[str] | None = None) -> list[dict]:
    ft = _doc_type_filter(doc_types)
    knn_clause = {"vector": {"vector": vector, "k": k}}
    if ft:
        body = {
            "size": k,
            "query": {"bool": {"must": {"knn": knn_clause}, "filter": ft}},
            "_source": {"excludes": ["vector"]},
        }
    else:
        body = {
            "size": k,
            "query": {"knn": knn_clause},
            "_source": {"excludes": ["vector"]},
        }
    resp = os_client.search(index=config.OPENSEARCH_INDEX, body=body)
    return [{**h["_source"], "_id": h["_id"], "_score": h["_score"]} for h in resp["hits"]["hits"]]


def bm25_search(os_client: OpenSearch, query: str,
                k: int = config.K_BM25,
                doc_types: list[str] | None = None) -> list[dict]:
    ft = _doc_type_filter(doc_types)
    match_clause = {"match": {"text": {"query": query, "analyzer": "zh"}}}
    if ft:
        body = {
            "size": k,
            "query": {"bool": {"must": match_clause, "filter": ft}},
            "_source": {"excludes": ["vector"]},
        }
    else:
        body = {
            "size": k,
            "query": match_clause,
            "_source": {"excludes": ["vector"]},
        }
    resp = os_client.search(index=config.OPENSEARCH_INDEX, body=body)
    return [{**h["_source"], "_id": h["_id"], "_score": h["_score"]} for h in resp["hits"]["hits"]]


def hybrid_search(query: str,
                  doc_types: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    os_client, bedrock = _get_clients()
    bm25_results = bm25_search(os_client, query, doc_types=doc_types)
    try:
        vector = embed_query(bedrock, query)
        vector_results = vector_search(os_client, vector, doc_types=doc_types)
    except Exception as e:
        print(f"[hybrid] embedding 失败，降级纯 BM25：{e}")
        vector_results = []
    return vector_results, bm25_results
