import os

OPENSEARCH_HOST  = os.environ.get("OPENSEARCH_HOST", "localhost")
OPENSEARCH_PORT  = int(os.environ.get("OPENSEARCH_PORT", "443"))
OPENSEARCH_INDEX = "construction-rag"
AWS_REGION       = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

UPLOAD_DIR       = os.environ.get("UPLOAD_DIR", "sampledata")

EMBEDDING_MODEL  = "amazon.titan-embed-text-v2:0"
EMBEDDING_DIM    = 1024

RERANK_MODEL_ARN_AMAZON = "arn:aws:bedrock:us-west-2::foundation-model/amazon.rerank-v1:0"
RERANK_REGION_AMAZON    = "us-west-2"   # amazon.rerank-v1:0 未在 us-east-1 上线
RERANK_MODEL_ARN_COHERE = f"arn:aws:bedrock:{AWS_REGION}::foundation-model/cohere.rerank-v3-5:0"
GENERATION_MODEL = "moonshotai.kimi-k2.5"

# Retrieval
K_VECTOR = 20
K_BM25   = 20
K_RRF    = 60
TOP_N    = 10
TOP_M    = 8

# Rejection / contradiction thresholds (calibrate with calibrate_tau.py)
TAU_ABS  = 0.1    # top1 rerank score below this → reject; calibrated 2026-06-22: F1=0.966
TAU_GAP  = 0.15   # (top1-top2)/top1 below this → contradiction mode
BM25_FALLBACK_FLOOR = 8.0   # calibrated 2026-06-22: F1=0.938

# Per-doc-type chunking
CHUNK_SETTINGS = {
    "历史方案": {"size": 600, "overlap": 200},
    "现行规范": {"size": 350, "overlap": 120},
    "商务数据": {"size": 250, "overlap": 50},
}
