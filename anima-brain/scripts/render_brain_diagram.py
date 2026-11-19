"""Render readable README SVGs using the atlas node IDs and localized labels.

The compact atlas coordinates serve an interactive canvas. This presentation layout
keeps text readable at GitHub's roughly 800-pixel content width.
"""

import argparse
import json
import textwrap
from html import escape
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--lang", choices=["en", "zh-CN"], default="en")
args = parser.parse_args()
zh = args.lang == "zh-CN"
flow = json.loads((ROOT / "docs/assets/brain-flow.json").read_text())
locale = json.loads((ROOT / "docs/assets/brain-flow.zh-CN.json").read_text())
nodes = {node["id"]: node for node in flow["views"][0]["nodes"]}
if zh:
    for node_id, node in nodes.items():
        node.update(locale["views"]["overview"]["nodes"][node_id])


def copy(en, cn):
    return cn if zh else en


parts = [
    '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="1150" '
    f'viewBox="0 0 1000 1150" lang="{args.lang}" role="img" aria-labelledby="title desc">',
    '<title id="title">' + copy("Inside Anima Brain", "Anima Brain 的记忆与情绪架构") + "</title>",
    '<desc id="desc">'
    + copy(
        "A message becomes retrieved context, a reply and a validated commit. Original evidence, "
        "semantic facts, cited episodes and core notes feed future turns. Consolidation updates "
        "derived memory; emotional state evolves separately. "
        "Evidence and archives use different rankings.",
        "消息经过上下文组装、模型回复与校验写入。原始证据、语义事实、事件摘要与核心笔记服务后续对话。"
        "巩固循环更新派生记忆，情绪状态单独演化。原始证据与记忆档案使用不同的排序公式。",
    )
    + "</desc>",
    '<rect width="1000" height="1150" rx="20" fill="#f8f7f2"/>',
    '<style>text{font-family:"Helvetica Neue",Arial,"PingFang SC","Microsoft YaHei",'
    "sans-serif;fill:#25382f}.muted{fill:#5e6c65}.label{font-size:15px;letter-spacing:1px}"
    ".heading{font-size:23px;font-weight:600}.body{font-size:18px}</style>",
    '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
    'markerWidth="6" markerHeight="6" orient="auto">'
    '<path d="M0 0L10 5L0 10" fill="#6a8274"/></marker></defs>',
]


def text(x, y, value, cls="body", extra=""):
    parts.append(f'<text x="{x}" y="{y}" class="{cls}" {extra}>{escape(value)}</text>')


def rect(x, y, w, h, fill="#fffefa", stroke="#d5ddd5"):
    parts.append(
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{fill}" stroke="{stroke}"/>'
    )


def path(d, dashed=False):
    dash = ' stroke-dasharray="6 5"' if dashed else ""
    parts.append(
        f'<path d="{d}" fill="none" stroke="#6a8274" '
        f'stroke-width="1.8"{dash} marker-end="url(#arrow)"/>'
    )


text(32, 38, "ANIMA / BRAIN ATLAS", "label muted")
text(
    32,
    85,
    copy("Memory that carries the conversation forward", "让每一次对话，都接得上"),
    extra='style="font-size:32px;font-weight:600"',
)
text(
    32,
    117,
    copy(
        "Source-backed memory. Changing knowledge. Emotional continuity.",
        "保留原始证据，更新长期知识，让情绪与关系延续",
    ),
    "body muted",
)

for i, node_id in enumerate(["input", "recall", "reply", "commit", "output"]):
    x, y, w = 32 + i * 190, 155, 176
    rect(x, y, w, 142)
    text(x + 14, y + 27, f"0{i + 1}", "label muted")
    title = nodes[node_id]["title"]
    lines = [title] if zh else textwrap.wrap(title, width=16)
    for j, line in enumerate(lines):
        text(x + 14, y + 59 + j * 24, line, extra='style="font-size:19px;font-weight:600"')
    subtitles = {
        "input": copy("Text / voice / image", "文字／语音／图片"),
        "recall": copy("Memory + state", "记忆＋当前状态"),
        "reply": copy("Your reply model", "接入你选择的模型"),
        "commit": copy("Quotes + revisions", "引用校验＋版本记录"),
        "output": copy("Text / speech / video", "文字／语音／视频"),
    }
    text(x + 14, y + 122, subtitles[node_id], extra='style="font-size:16px;fill:#5e6c65"')
    if i < 4:
        path(f"M{x + w} 222H{x + 189}")

text(32, 342, copy("RETRIEVE FOR THE NEXT TURN", "检索并组装下一轮上下文"), "label muted")
text(
    32,
    373,
    copy(
        "Recent dialogue + recalled sources + character guidance, within a context budget",
        "近期对话＋检索到的证据＋角色指引，按上下文预算组装",
    ),
)
path("M968 392H986V318H310V298", dashed=True)

details = {
    "original": [
        copy("Exact messages, speakers, timestamps", "原话、说话者、时间与出处"),
        copy("Contextual embeddings + notes + source links", "上下文向量、检索注释与原文关联"),
    ],
    "semantic": [
        copy("Facts, preferences and boundaries", "事实、偏好与边界"),
        copy("Current / superseded / corrected revisions", "区分当前事实、历史变更与错误纠正"),
    ],
    "episodic": [
        copy("Cited summaries of shared events", "带来源引用的共同经历摘要"),
        copy("Original evidence stays available for checking", "摘要不替代原文，仍可回查证据"),
    ],
    "core": [
        copy("Merged user notes + separate character core", "合并用户笔记，保留独立的角色核心"),
        copy("Compact continuity, refreshed after corrections", "提供紧凑上下文，纠错后同步刷新"),
    ],
}
for i, node_id in enumerate(["original", "semantic", "episodic", "core"]):
    x, y = 32 + (i % 2) * 480, 404 + (i // 2) * 153
    rect(x, y, 456, 129, "#eff3ed")
    text(x + 20, y + 34, nodes[node_id]["title"], "heading")
    for j, line in enumerate(details[node_id]):
        text(x + 20, y + 69 + j * 28, line, "body muted")

text(32, 733, copy("TWO CONTINUITY LOOPS", "两条独立的连续性循环"), "label muted")
rect(32, 754, 576, 171, "#e7eee4")
text(52, 790, nodes["consolidate"]["title"], "heading")
text(52, 826, copy("Summarize → cite → reindex → refresh", "总结 → 校验引用 → 重建索引 → 刷新"))
text(
    52,
    857,
    copy(
        "Configured worker or host; original sources are retained",
        "由配置的后台任务或宿主触发，原始对话保留",
    ),
    "body muted",
)
text(
    52,
    888,
    copy("Corrections invalidate affected derived summaries", "事实纠正后，失效的派生摘要也会更新"),
    "body muted",
)
path("M32 469H20V840H31")
path("M585 754V710H730V687")
path("M585 710H400V687")
rect(632, 754, 336, 171, "#edf0f5", "#cdd6df")
text(652, 790, nodes["emotion"]["title"], "heading")
text(652, 826, copy("PAD + relationship dimensions", "PAD 情绪＋关系维度"))
text(652, 857, copy("Bounded updates + time decay", "有边界的更新＋时间衰减"), "body muted")
text(652, 888, copy("Separate from user facts", "与用户事实记忆分开保存"), "body muted")
path("M968 840H986V392", dashed=True)

parts.append('<path d="M32 955H968" stroke="#d5ddd5"/>')
text(
    32,
    989,
    copy("DIFFERENT RETRIEVAL PATHS, DIFFERENT SCORES", "不同检索对象，使用不同排序"),
    "label muted",
)
text(
    32,
    1023,
    copy(
        "Evidence: lexical + contextual vectors + note vectors → RRF, k = 40",
        "原始证据：关键词＋上下文向量＋注释向量 → RRF 融合，k = 40",
    ),
)
text(
    32,
    1055,
    copy(
        "Fact / episode archive: 0.60 relevance + 0.25 recency + 0.15 importance",
        "事实／事件档案：0.60 × 相关度＋0.25 × 时效性＋0.15 × 重要性",
    ),
)
text(
    32,
    1100,
    copy(
        "Provider-dependent features are configurable. "
        "Full formulas and limits: docs/brain-flow.md",
        "模型服务与后台能力按配置启用 · 详细公式和边界见 docs/brain-flow.md",
    ),
    extra='style="font-size:16px;fill:#5e6c65"',
)
parts.append("</svg>")
svg = "\n".join(parts) + "\n"
ET.fromstring(svg)
suffix = ".zh-CN" if zh else ""
target = ROOT / f"docs/assets/brain-flow{suffix}.svg"
target.write_text(svg)
print(target.relative_to(ROOT))
