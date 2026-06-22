"""应用层 RRF（Reciprocal Rank Fusion）融合向量和 BM25 两路结果"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import config


def rrf_merge(
    vector_hits: list[dict],
    bm25_hits: list[dict],
    k_rrf: int = config.K_RRF,
    top_n: int = config.TOP_N,
) -> list[dict]:
    rrf: dict[str, float] = {}
    bm25_raw: dict[str, float] = {}
    docs: dict[str, dict] = {}

    for rank, hit in enumerate(vector_hits, 1):
        cid = hit["_id"]
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (k_rrf + rank)
        docs[cid] = hit

    for rank, hit in enumerate(bm25_hits, 1):
        cid = hit["_id"]
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (k_rrf + rank)
        bm25_raw[cid] = hit["_score"]
        docs[cid] = hit

    ranked = sorted(rrf.items(), key=lambda x: x[1], reverse=True)[:top_n]
    result = []
    for cid, score in ranked:
        doc = dict(docs[cid])
        doc["rrf_score"] = round(score, 6)
        doc["bm25_score"] = bm25_raw.get(cid, 0.0)
        result.append(doc)
    return result
