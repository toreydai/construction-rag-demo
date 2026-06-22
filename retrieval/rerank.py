"""
Bedrock Rerank 级联 + 矛盾信号检测 + 拒答判定

级联策略：Amazon Rerank v1 → Cohere Rerank v3.5 → RRF fallback
矛盾信号：top1 与 top2 rerank score 之差过小 → 触发多源对比分析模式
"""
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import config

_client_amazon = None
_client_default = None
_MAX_RETRIES = 3

# Amazon Rerank 仅在 us-west-2 / ap-northeast-1 上线，需单独 client
_RERANK_CONFIGS = [
    (config.RERANK_MODEL_ARN_AMAZON, "amazon", config.RERANK_REGION_AMAZON),
    (config.RERANK_MODEL_ARN_COHERE, "cohere", config.AWS_REGION),
]


def _get_client(region: str):
    global _client_amazon, _client_default
    if region == config.RERANK_REGION_AMAZON:
        if _client_amazon is None:
            _client_amazon = boto3.client("bedrock-agent-runtime", region_name=region)
        return _client_amazon
    if _client_default is None:
        _client_default = boto3.client("bedrock-agent-runtime", region_name=region)
    return _client_default


def _call_rerank(client, model_arn: str, sources: list, query: str, top_m: int) -> list:
    n = min(top_m, len(sources))
    last_exc = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = client.rerank(
                rerankingConfiguration={
                    "type": "BEDROCK_RERANKING_MODEL",
                    "bedrockRerankingConfiguration": {
                        "modelConfiguration": {"modelArn": model_arn},
                        "numberOfResults": n,
                    },
                },
                sources=sources,
                queries=[{"type": "TEXT", "textQuery": {"text": query}}],
            )
            return resp.get("rerankingResults") or resp["results"]
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("ThrottlingException", "ServiceUnavailableException") and attempt < _MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
                last_exc = e
            else:
                raise
    raise last_exc


def rerank(query: str, candidates: list[dict], top_m: int = config.TOP_M) -> tuple[list[dict], str]:
    sources = [
        {"type": "INLINE", "inlineDocumentSource": {"type": "TEXT", "textDocument": {"text": c["text"]}}}
        for c in candidates
    ]
    for arn, label, region in _RERANK_CONFIGS:
        client = _get_client(region)
        try:
            results = _call_rerank(client, arn, sources, query, top_m)
            ranked = []
            for r in results:
                doc = dict(candidates[r["index"]])
                doc["rerank_score"] = round(r["relevanceScore"], 6)
                ranked.append(doc)
            return ranked, label
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("AccessDeniedException", "ValidationException", "ResourceNotFoundException"):
                print(f"[rerank] {label} 不可用（{code}），尝试下一个…")
                continue
            raise
    raise RuntimeError("所有 Rerank 模型均不可用")


def check_signals(ranked: list[dict]) -> tuple[bool, bool]:
    """
    返回 (should_reject, is_contradiction)。
    should_reject:    top1 分数过低，知识库无相关内容
    is_contradiction: top1 与 top2 差距过小，多来源存在分歧
    """
    if not ranked:
        return True, False
    top1 = ranked[0]["rerank_score"]
    if top1 < config.TAU_ABS:
        return True, False
    if len(ranked) >= 2:
        top2 = ranked[1]["rerank_score"]
        gap_ratio = (top1 - top2) / top1 if top1 > 0 else 1.0
        is_contradiction = gap_ratio < config.TAU_GAP
    else:
        is_contradiction = False
    return False, is_contradiction


def fallback_rejection(rrf_results: list[dict]) -> bool:
    if not rrf_results:
        return True
    return max(c.get("bm25_score", 0.0) for c in rrf_results) < config.BM25_FALLBACK_FLOOR


def rerank_and_check(
    query: str,
    rrf_results: list[dict],
    top_m: int = config.TOP_M,
) -> tuple[list[dict], bool, bool, str]:
    """
    返回 (chunks, should_reject, is_contradiction, mode)
    mode: "amazon" | "cohere" | "fallback"
    """
    try:
        ranked, label = rerank(query, rrf_results, top_m)
        should_reject, is_contradiction = check_signals(ranked)
        return ranked, should_reject, is_contradiction, label
    except Exception:
        fallback = rrf_results[:top_m]
        should_reject = fallback_rejection(rrf_results)
        return fallback, should_reject, False, "fallback"
