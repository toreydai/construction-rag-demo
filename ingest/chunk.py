"""
从 upload/ 目录读取文档，按文档类型差异化分块，输出 chunks.jsonl。
用法：python3 ingest/chunk.py
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import config


def extract_pdf(path: Path) -> str:
    import pypdf
    reader = pypdf.PdfReader(str(path))
    return "\n".join((p.extract_text() or "").strip() for p in reader.pages)


def extract_docx(path: Path) -> str:
    from docx import Document
    from docx.oxml.ns import qn
    doc = Document(str(path))
    parts = []
    # 按 XML 顺序遍历，保留段落与表格的原始位置关系
    for child in doc.element.body:
        if child.tag == qn("w:p"):
            text = "".join(r.text for r in child.iter(qn("w:t"))).strip()
            if text:
                parts.append(text)
        elif child.tag == qn("w:tbl"):
            seen = set()
            for row in child.iter(qn("w:tr")):
                cells = []
                for cell in row.iter(qn("w:tc")):
                    cell_text = "".join(r.text for r in cell.iter(qn("w:t"))).strip()
                    if cell_text:
                        cells.append(cell_text)
                # 去重相邻重复单元格（python-docx 合并单元格会重复返回）
                deduped = [c for i, c in enumerate(cells) if i == 0 or c != cells[i - 1]]
                row_key = " | ".join(deduped)
                if deduped and row_key not in seen:
                    seen.add(row_key)
                    parts.append(row_key)
    return "\n".join(parts)


def extract_txt(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf(path)
    if suffix == ".docx":
        return extract_docx(path)
    if suffix == ".txt":
        return extract_txt(path)
    raise ValueError(f"不支持的文件类型: {suffix}")


def doc_type_of(path: Path, upload_root: Path) -> str:
    top = path.relative_to(upload_root).parts[0]
    return {"历史方案": "历史方案", "现行规范": "现行规范", "商务数据": "商务数据"}.get(top, "其他")


def sliding_chunks(text: str, size: int, overlap: int) -> list[str]:
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start += size - overlap
    return chunks


def chunk_file(path: Path, upload_root: Path) -> list[dict]:
    dtype = doc_type_of(path, upload_root)
    cfg = config.CHUNK_SETTINGS.get(dtype, {"size": 500, "overlap": 100})
    text = re.sub(r"\n{3,}", "\n\n", extract_text(path))
    return [
        {"chunk_id": f"{path.stem}_{i:04d}", "source": path.name, "doc_type": dtype, "text": chunk}
        for i, chunk in enumerate(sliding_chunks(text, cfg["size"], cfg["overlap"]))
    ]


def main():
    upload_root = (ROOT / config.UPLOAD_DIR).resolve()
    if not upload_root.exists():
        print(f"upload 目录不存在: {upload_root}")
        sys.exit(1)

    out_path = ROOT / "ingest" / "chunks.jsonl"
    total = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for fp in sorted(upload_root.rglob("*")):
            if not fp.is_file() or fp.suffix.lower() not in (".pdf", ".docx", ".txt"):
                continue
            print(f"  {fp.name}", flush=True)
            try:
                chunks = chunk_file(fp, upload_root)
                for c in chunks:
                    f.write(json.dumps(c, ensure_ascii=False) + "\n")
                print(f"    → {len(chunks)} chunks")
                total += len(chunks)
            except Exception as e:
                print(f"    ✗ {e}")

    print(f"\n完成：{total} chunks → {out_path}")


if __name__ == "__main__":
    main()
