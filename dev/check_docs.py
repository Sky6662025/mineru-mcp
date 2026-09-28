"""校验文档内部链接与 TOC 锚点的一致性。"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = ["README.md", "docs/DEPLOYMENT.md", "docs/narrative.md", "docs/AGENT_DEPLOY_PROMPT.md"]


def slug(h: str) -> str:
    """GitHub 风格锚点：小写、去标点（保留连字符与下划线）、每个空格各替换为一个连字符。"""
    s = h.strip().lower()
    s = re.sub(r"[^\w\u4e00-\u9fff\s-]", "", s)
    return s.replace(" ", "-")


ok = True
for rel in DOCS:
    path = os.path.join(ROOT, rel)
    txt = open(path, encoding="utf-8").read()
    base = os.path.dirname(path)

    heads = {slug(m.group(2)) for m in re.finditer(r"^(#{1,6})\s+(.*)$", txt, re.M)}
    anchors = re.findall(r"\]\(#([^)]+)\)", txt)
    missing = [a for a in anchors if a not in heads]

    broken = []
    for m in re.finditer(r"\]\(([^)#]+?)(#[^)]*)?\)", txt):
        link = m.group(1).strip()
        if link.startswith(("http://", "https://", "mailto:")):
            continue
        if not os.path.exists(os.path.normpath(os.path.join(base, link))):
            broken.append(link)

    good = not missing and not broken
    ok = ok and good
    print(f"{'OK  ' if good else 'FAIL'} {rel}: anchors={len(anchors)} missing={missing} broken_files={broken}")

print()
print("ALL DOCS CONSISTENT" if ok else "HAS ISSUES")
sys.exit(0 if ok else 1)
