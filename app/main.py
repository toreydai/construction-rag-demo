"""
施工方案知识库助手 — Streamlit 前端
运行：streamlit run app/main.py --server.port 8501 --server.address 0.0.0.0
"""
from __future__ import annotations
import json
import random
import re
import sys
from datetime import datetime
from pathlib import Path

import boto3
import streamlit as st

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import config
from retrieval.hybrid import hybrid_search
from retrieval.fusion import rrf_merge
from retrieval.rerank import rerank_and_check, fallback_rejection

# ── 常量 ─────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是一个建筑工程施工方案编制助手，服务于施工企业的总工、技术员和商务人员。

回答规则：
1. 引用来源：每个技术参数或结论必须标注来源，格式为【文件名】
2. 参数对比：多个文件对同一参数有不同记录时，用表格并列所有值，不要只返回第一个
3. 区分规范与实践：明确区分"规范底线要求"和"工程实践取值"
4. 矛盾说明：来源差异时分析原因（地质条件、环境等级、特殊要求），给综合建议
5. 造价回答：提供区间范围，对比历史项目实际数据
6. 不确定时：说明"现有资料未涵盖该情况"，不编造数据
7. 禁止推断数字：所有数字参数（深度、比例、价格、密度等）必须原文出现在参考片段中，不得根据常识或上下文推算未明确出现的数值"""

CONTRADICTION_EXTRA = """
[矛盾信号已触发] 检测到多个相关度相近的文档，请逐一列出各来源的取值，分析差异原因，最后给出综合判断。"""

FOLLOW_UP_RE = re.compile(r"(那|这|它|呢|怎么样|还有|另外|再|此外|同理|同样|其他|其余|剩余)")

DOC_TYPE_COLOR = {"历史方案": "🔵", "现行规范": "🟢", "商务数据": "🟠"}
DOC_TYPES_ALL = ["历史方案", "现行规范", "商务数据"]

QUICK_QUESTIONS = {
    "L1 基础检索": [
        "有没有类似深度15m以上、周边有地铁的基坑支护方案？",
        "有没有直径1200mm以上、入岩的桩基施工方案？",
        "之前有没有做过大体积混凝土夏季施工的项目？怎么控制温度的？",
        "高支模搭设完成后需要做什么试验才能浇筑混凝土？",
        "基坑深度超过10m时，监测频率应该是多少？",
    ],
    "L2 跨文档综合": [
        "基坑深度18m，临近地铁，地连墙嵌固深度至少要多少？",
        "φ1200灌注桩入泥岩用旋挖钻能打下去吗？有没有实际案例？",
        "800mm地连墙现在北京什么价格？我们报了3,500元/m²合理吗？",
        "帮我总结一下之前基坑项目的降水方案都是怎么做的",
        "地连墙灌注前泥浆密度应该控制在多少？",
    ],
    "L3 矛盾分析": [
        "嵌固深度做0.7H够不够？还是要做到0.8H？",
        "φ1200入泥岩旋挖能打下去吗？还是必须用冲击钻？",
        "声波透射做到30%规范是满足了，但够不够？",
        "14m基坑止水用高压旋喷够吗？还是必须三轴搅拌桩？",
        "我的基坑16m深，选地连墙还是灌注桩好？有没有实际案例对比？",
    ],
}

ALL_QUESTIONS = [q for qs in QUICK_QUESTIONS.values() for q in qs]

# ── 客户端 ────────────────────────────────────────────────────────────────────

@st.cache_resource
def get_bedrock():
    return boto3.client("bedrock-runtime", region_name=config.AWS_REGION)


# ── 推理函数 ──────────────────────────────────────────────────────────────────

def _invoke(messages: list[dict], max_tokens: int = 256) -> str:
    bedrock = get_bedrock()
    resp = bedrock.invoke_model(
        modelId=config.GENERATION_MODEL,
        body=json.dumps({"messages": messages, "max_tokens": max_tokens}),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(resp["body"].read())["choices"][0]["message"]["content"].strip()


def _invoke_stream(messages: list[dict], max_tokens: int = 4096):
    bedrock = get_bedrock()
    resp = bedrock.invoke_model_with_response_stream(
        modelId=config.GENERATION_MODEL,
        body=json.dumps({"messages": messages, "max_tokens": max_tokens}),
        contentType="application/json",
        accept="application/json",
    )
    for event in resp["body"]:
        raw = event.get("chunk", {}).get("bytes", b"")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        text = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
        if text:
            yield text


def rewrite_query(query: str, history: list[dict]) -> str:
    if not history:
        return query
    if len(query.strip()) >= 20 and not FOLLOW_UP_RE.search(query):
        return query
    recent = history[-4:]
    ctx = "\n".join(
        f"{'用户' if m['role']=='user' else '助手'}: {m['content'][:200]}"
        for m in recent
    )
    prompt = (
        f"对话历史：\n{ctx}\n\n当前提问：{query}\n\n"
        "如果是追问请改写为独立完整的问题，否则原样输出。只输出改写后的问题。"
    )
    try:
        return _invoke([{"role": "user", "content": prompt}], max_tokens=80)
    except Exception:
        return query


def build_context(chunks: list[dict]) -> str:
    lines = []
    for c in chunks:
        source = c.get("source", c["chunk_id"])
        lines.append(f"【来源：{source}】\n{c['text']}")
    return "\n\n---\n\n".join(lines)


def generate_answer(query: str, chunks: list[dict], is_contradiction: bool) -> str:
    system = SYSTEM_PROMPT + (CONTRADICTION_EXTRA if is_contradiction else "")
    context = build_context(chunks)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"[参考片段]\n{context}\n\n[用户问题]\n{query}"},
    ]
    return st.write_stream(_invoke_stream(messages))


# ── 渲染组件 ──────────────────────────────────────────────────────────────────

def render_confidence_bar(chunks: list[dict], mode: str):
    if mode == "fallback" or not chunks:
        return
    top_score = chunks[0].get("rerank_score", 0)
    col1, col2 = st.columns([1, 4])
    with col1:
        st.caption("置信度")
    with col2:
        color = "green" if top_score >= 0.7 else "orange" if top_score >= 0.3 else "red"
        st.markdown(
            f'<div style="background:#eee;border-radius:4px;height:10px;margin-top:8px">'
            f'<div style="width:{int(top_score*100)}%;background:{color};height:10px;border-radius:4px"></div>'
            f'</div><small style="color:gray">{top_score:.3f}</small>',
            unsafe_allow_html=True,
        )


def render_contradiction_highlight(chunks: list[dict]):
    if len(chunks) < 2:
        return
    st.markdown("#### ⚡ 多来源差异对比")
    c1, c2 = st.columns(2)
    colors = ["#e8f4fd", "#fff3e0"]
    icons = ["🔵", "🟠"]
    for i, (col, chunk) in enumerate(zip([c1, c2], chunks[:2])):
        with col:
            src = chunk.get("source", chunk["chunk_id"])
            score = chunk.get("rerank_score", 0)
            st.markdown(
                f'<div style="background:{colors[i]};padding:12px;border-radius:8px;'
                f'border-left:4px solid {"#1976d2" if i==0 else "#f57c00"}">'
                f'<b>{icons[i]} {src}</b><br>'
                f'<small style="color:gray">rerank: {score:.4f}</small><br><br>'
                f'{chunk["text"][:400]}{"…" if len(chunk["text"])>400 else ""}'
                f'</div>',
                unsafe_allow_html=True,
            )


def render_pipeline(v_hits: list[dict], b_hits: list[dict],
                    rrf_results: list[dict], chunks: list[dict], mode: str):
    with st.expander("🔍 检索管线详情", expanded=False):
        tab1, tab2, tab3, tab4 = st.tabs(["向量召回", "BM25 召回", "RRF 合并", "Rerank 精排"])

        with tab1:
            st.caption(f"向量召回 Top-{len(v_hits)} 条")
            for h in v_hits[:5]:
                st.markdown(f"**{h.get('source', h.get('chunk_id',''))}** `{h['_score']:.4f}`")
                st.caption(h["text"][:120] + "…")

        with tab2:
            st.caption(f"BM25 召回 Top-{len(b_hits)} 条")
            for h in b_hits[:5]:
                st.markdown(f"**{h.get('source', h.get('chunk_id',''))}** `{h['_score']:.4f}`")
                st.caption(h["text"][:120] + "…")

        with tab3:
            st.caption(f"RRF 合并 Top-{len(rrf_results)} 条")
            for r in rrf_results[:8]:
                st.markdown(
                    f"**{r.get('source', r.get('chunk_id',''))}** "
                    f"rrf={r.get('rrf_score',0):.4f} "
                    f"bm25={r.get('bm25_score',0):.2f}"
                )

        with tab4:
            label = f"Rerank 精排（{mode}）Top-{len(chunks)} 条"
            st.caption(label)
            for c in chunks:
                score = c.get("rerank_score", c.get("rrf_score", 0))
                score_key = "rerank" if "rerank_score" in c else "rrf"
                st.markdown(
                    f"**{c.get('source', c.get('chunk_id',''))}** "
                    f"`{score_key}={score:.4f}`  {DOC_TYPE_COLOR.get(c.get('doc_type',''),'📄')}"
                )


def render_sources(chunks: list[dict], mode: str):
    scores_label = "rerank_score" if mode != "fallback" else "rrf_score"
    with st.expander(f"📎 引用来源（{len(chunks)} 条，{mode}）", expanded=True):
        for c in chunks:
            icon = DOC_TYPE_COLOR.get(c.get("doc_type", ""), "📄")
            score = c.get("rerank_score", c.get("rrf_score", 0))
            st.markdown(
                f"{icon} **{c.get('source', c['chunk_id'])}**  \n"
                f"<small style='color:gray'>{scores_label}: {score:.4f}</small>",
                unsafe_allow_html=True,
            )
            with st.container():
                st.caption(c["text"][:200] + ("…" if len(c["text"]) > 200 else ""))


def export_conversation(messages: list[dict]) -> str:
    lines = [f"# 知识库问答记录\n\n导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]
    for m in messages:
        role = "**用户**" if m["role"] == "user" else "**助手**"
        lines.append(f"\n{role}\n\n{m['content']}\n\n---")
    return "\n".join(lines)


# ── 查询处理 ──────────────────────────────────────────────────────────────────

def handle_query(query: str, doc_types: list[str], use_rerank: bool, top_m: int):
    st.session_state.messages.append({"role": "user", "content": query})
    with st.chat_message("user"):
        st.write(query)

    rewritten = rewrite_query(query, st.session_state.messages[:-1])
    if rewritten != query:
        st.caption(f"🔄 改写为：{rewritten}")

    with st.spinner("检索中…"):
        active_types = doc_types if doc_types else None
        v_hits, b_hits = hybrid_search(rewritten, doc_types=active_types)
        rrf_results = rrf_merge(v_hits, b_hits)

        if use_rerank:
            chunks, should_reject, is_contradiction, mode = rerank_and_check(
                rewritten, rrf_results, top_m=top_m
            )
        else:
            chunks = rrf_results[:top_m]
            should_reject = fallback_rejection(rrf_results)
            is_contradiction = False
            mode = "rrf-only"

    # 管线可视化
    render_pipeline(v_hits, b_hits, rrf_results, chunks, mode)

    with st.chat_message("assistant"):
        if should_reject:
            answer = "当前知识库未涵盖该问题，建议补充相关资料后重试。"
            st.write(answer)
        else:
            if is_contradiction:
                st.warning("⚠️ 检测到多来源差异，已切换至矛盾分析模式")
                render_contradiction_highlight(chunks)
            render_confidence_bar(chunks, mode)
            answer = generate_answer(rewritten, chunks, is_contradiction)

        render_sources(chunks, mode)

    st.session_state.messages.append({"role": "assistant", "content": answer})


# ── 主页面 ────────────────────────────────────────────────────────────────────

def main():
    st.set_page_config(page_title="施工方案知识库助手", page_icon="🏗️", layout="wide")

    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "pending_question" not in st.session_state:
        st.session_state.pending_question = None

    # ── 侧边栏 ──
    with st.sidebar:
        st.header("🏗️ 知识库助手")

        st.subheader("🎛️ 检索参数")

        doc_types = st.multiselect(
            "文档类型筛选",
            DOC_TYPES_ALL,
            default=DOC_TYPES_ALL,
            help="只从选中的文档类型中检索"
        )

        use_rerank = st.toggle("启用 Rerank 精排", value=True,
                               help="关闭后直接用 RRF 排序，可对比效果差异")

        top_m = st.slider("召回数量 TOP_M", min_value=4, max_value=16,
                          value=config.TOP_M, step=1,
                          help="最终传入 LLM 的 chunk 数量")

        if not use_rerank:
            st.info("Rerank 已关闭，当前使用 RRF 排序")

        st.divider()
        st.subheader("💬 快捷问题")

        if st.button("🎲 随机抽一题", use_container_width=True):
            st.session_state.pending_question = random.choice(ALL_QUESTIONS)

        for level, questions in QUICK_QUESTIONS.items():
            with st.expander(level, expanded=False):
                for q in questions:
                    if st.button(q[:30] + ("…" if len(q) > 30 else ""), key=q):
                        st.session_state.pending_question = q

        st.divider()

        col1, col2 = st.columns(2)
        with col1:
            if st.button("🗑️ 清空", use_container_width=True):
                st.session_state.messages = []
                st.rerun()
        with col2:
            if st.session_state.messages:
                md = export_conversation(st.session_state.messages)
                st.download_button(
                    "📥 导出",
                    data=md,
                    file_name=f"rag-chat-{datetime.now().strftime('%Y%m%d-%H%M')}.md",
                    mime="text/markdown",
                    use_container_width=True,
                )

    # ── 主区域标题 ──
    st.title("🏗️ 施工方案知识库助手")
    rerank_badge = "✅ Rerank" if use_rerank else "⚡ RRF-only"
    filter_badge = "、".join(doc_types) if len(doc_types) < 3 else "全部文档"
    st.caption(f"当前模式：{rerank_badge} | 文档范围：{filter_badge} | TOP_M={top_m}")

    # ── 历史消息 ──
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])

    # ── 快捷问题触发 ──
    if st.session_state.pending_question:
        q = st.session_state.pending_question
        st.session_state.pending_question = None
        handle_query(q, doc_types, use_rerank, top_m)
        st.rerun()

    # ── 输入框 ──
    if query := st.chat_input("输入问题，例如：嵌固深度做0.7H够不够？"):
        handle_query(query, doc_types, use_rerank, top_m)


if __name__ == "__main__":
    main()
