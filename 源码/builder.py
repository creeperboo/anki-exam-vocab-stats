"""应试词汇统计 · 一键补漏制卡（纯逻辑）。

只负责「把未覆盖词 + 本机素材拼成卡片草稿」：算要建哪些牌组、每条卡片每个字段
写什么、去重与限量、确认框文案、导出清单。真正创建牌组 / 笔记类型 / 写媒体 /
落笔记由 ``__init__.py`` 接线（那边才有 Anki 的 API）。

默认口径（已和用户确认）：处理当前明细筛选结果里的「未覆盖」词——真未覆盖 + 歧义
待确认，两者界面都算未覆盖，所以都做卡；「识别失败」（词表里有、卡片也有，是我们
的匹配没认出）不重复制卡。新建独立
牌组 ``应试补漏::<词表>`` + 自带笔记类型「应试补漏卡」；中文释义优先；新牌组的
新卡上限设为 0（检查完自己手动放开）；同时打 ``应试::<词表>`` 标签。
"""

from __future__ import annotations

import os
import re

try:  # Anki 里是包内相对导入；单独跑测试时是平铺导入
    from . import vocab_logic as V
except ImportError:  # pragma: no cover
    import vocab_logic as V

try:  # analysis 里放着「真未覆盖」这类原因常量
    from . import analysis as A
except ImportError:  # pragma: no cover
    import analysis as A

# 自带笔记类型的字段顺序（改这个就要同步改导出的模板，模板里只用到前 6 个）
CARD_FIELDS = (
    "单词",
    "假名·音标",
    "释义",
    "例句",
    "例句译",
    "音频",
    "考试标签",
    "来源",
)

CARD_NOTETYPE_NAME = "应试补漏卡"
DECK_PREFIX = "应试补漏"

# 补漏牌组默认补这两类词；原因常量在 analysis 里，取不到就用中文兜底。
# 「真未覆盖」＝词表里根本没有这个词；「待确认」＝歧义（界面也算未覆盖）。
# 「识别失败」＝词表里有、卡片里也有，只是匹配没认出——做卡只会重复，仍然跳过。
DEFAULT_REASONS = (
    getattr(A, "GAP_MISSING", "真未覆盖"),
    getattr(A, "GAP_PENDING", "待确认"),
)

# 默认的新卡上限：0 = 先不让 Anki 自动出新卡，用户检查完再手动放开
DEFAULT_NEW_PER_DAY = 0
DEFAULT_LIMIT = 500

# 拼好的卡片模板：正面只放单词和音频，背面把释义/例句/来源铺开
FRONT_TEMPLATE = """<div class="ev-front">
  <div class="ev-word">{{单词}}</div>
  <div class="ev-reading">{{假名·音标}}</div>
  <div class="ev-audio">{{音频}}</div>
</div>"""

BACK_TEMPLATE = """{{FrontSide}}
<hr id=answer>
<div class="ev-back">
  <div class="ev-meaning">{{释义}}</div>
  {{#例句}}<div class="ev-example">{{例句}}</div>{{/例句}}
  {{#例句译}}<div class="ev-example-trans">{{例句译}}</div>{{/例句译}}
  <div class="ev-meta">{{考试标签}}</div>
  {{#来源}}<div class="ev-source">{{来源}}</div>{{/来源}}
</div>"""

CARD_CSS = """.card {
  font-family: system-ui, "Microsoft YaHei", sans-serif;
  font-size: 20px;
  text-align: center;
  color: #222;
  background: #fbfbfb;
}
.ev-word { font-size: 34px; font-weight: 600; margin-bottom: 6px; }
.ev-reading { color: #666; margin-bottom: 8px; }
.ev-meaning { text-align: left; margin: 10px 0; white-space: pre-wrap; }
.ev-example { text-align: left; color: #333; margin: 8px 0; }
.ev-example-trans { text-align: left; color: #666; margin: 4px 0 10px; }
.ev-meta { color: #8a6d3b; font-size: 15px; }
.ev-source { color: #999; font-size: 12px; margin-top: 10px; }
"""


def deck_name_for(code: str, language: str, label: str = "") -> str:
    """词表码 -> 补漏牌组名，例如 应试补漏::四级 / 应试补漏::JLPT-N3。

    ``label`` 是自定义词表自带的名字（内置词表不用传）。
    """
    if label:
        return f"{DECK_PREFIX}::{_safe_name(label)}"
    if language == "ja":
        got = V.JLPT_LABELS.get(code)
        if got:
            return f"{DECK_PREFIX}::JLPT-{got}"
    got = V.ALL_EXAM_LABELS.get(code)
    if got:
        return f"{DECK_PREFIX}::{got}"
    return f"{DECK_PREFIX}::{_safe_name(code)}"


def tag_for_code(code: str, label: str = "") -> str:
    """这个词表该打什么标签；自定义词表用 ``应试::自定义-xxx``。"""
    tag = V.exam_tag_for_code(code)
    if tag:
        return tag
    if label:
        return f"{V.EXAM_TAG_PREFIX}::自定义-{_safe_name(label)}"
    return ""


def notetype_spec() -> dict:
    """自带笔记类型的定义（Anki 那边照着建模型）。"""
    return {
        "name": CARD_NOTETYPE_NAME,
        "fields": list(CARD_FIELDS),
        "templates": [{"name": "补漏卡", "qfmt": FRONT_TEMPLATE, "afmt": BACK_TEMPLATE}],
        "css": CARD_CSS,
    }


def _safe_name(text: str) -> str:
    name = re.sub(r"[^0-9A-Za-z\u3041-\u30ff\u4e00-\u9fff]+", "_", text or "")
    return name.strip("_")[:60] or "exam"


def media_name_for(draft: dict) -> str:
    """音频写进 Anki 媒体库时用的文件名（带 exam_ 前缀，避免和别人撞名）。"""
    ext = ".mp3"
    source = draft.get("audio_path") or ""
    if source.lower().endswith(".wav"):
        ext = ".wav"
    return f"exam_{_safe_name(draft.get('key') or draft.get('surface') or '')}{ext}"


def note_fields(draft: dict) -> list[str]:
    """一条草稿 -> Anki 笔记的字段值（顺序必须是 CARD_FIELDS）。"""
    audio = draft.get("audio") or ""
    audio_field = f"[sound:{audio}]" if audio else ""
    return [
        draft.get("surface") or draft.get("key") or "",
        "　".join(x for x in (draft.get("reading", ""), draft.get("phonetic", "")) if x),
        draft.get("definition") or "",
        draft.get("example") or "",
        draft.get("example_translation") or "",
        audio_field,
        draft.get("exam_tag") or "",
        draft.get("source_line") or "",
    ]


def source_line(material: dict, code: str, language: str, label: str = "") -> str:
    """卡片底部那行来源标记（只写来源与许可，不写「官方」字样）。"""
    parts = list(dict.fromkeys(material.get("sources") or []))
    if not parts:
        parts = ["插件内置素材库"]
    if label:
        table = label
    elif language == "ja":
        table = V.JLPT_LABELS.get(code, code)
    else:
        table = V.ALL_EXAM_LABELS.get(code, code)
    prefix = f"词表：{table}（公开词表，非官方大纲）"
    return prefix + "；素材：" + "、".join(parts)


def _combine_definition(translation: str, definition: str) -> str:
    """把中文释义和原文释义拼成卡片「释义」字段的内容。

    两条内容完全一样（或只有一条）时只留一份，避免卡片上重复两遍。
    """
    first = (translation or "").strip()
    second = (definition or "").strip()
    if not first:
        return second
    if not second or second == first:
        return first
    return first + "\n" + second


def build_drafts(rows, resources, options: dict | None = None) -> tuple[list[dict], list[dict]]:
    """把未覆盖词拼成卡片草稿。

    返回 ``(drafts, skipped)``：``skipped`` 里放被过滤掉的词及原因，确认框和
    生成后的报告都要显示「哪些词没做、为什么」。
    """
    options = dict(options or {})
    limit = int(options.get("limit") or DEFAULT_LIMIT)
    reasons = set(options.get("reasons") or DEFAULT_REASONS)
    # 歧义词（待确认）默认一起制卡；显式传 include_pending=False 才排除
    if options.get("include_pending") is False:
        reasons.discard(getattr(A, "GAP_PENDING", "待确认"))
    wanted_codes = set(options.get("codes") or ())

    drafts: list[dict] = []
    skipped: list[dict] = []
    seen: set = set()

    # 先把要查的词收集起来，一次性喂给本机词典（避免逐词重扫 zip）
    terms = []
    for row in rows:
        key = row.get("key") or row.get("surface") or ""
        if key:
            terms.append(key)
    dict_entries: dict = {}
    if resources is not None and hasattr(resources, "dictionary_lookup"):
        for language in {row.get("language") or "en" for row in rows}:
            try:
                dict_entries.update(
                    resources.dictionary_lookup(
                        [t for t in terms], language
                    )
                )
            except Exception:
                pass

    for row in rows:
        key = row.get("key") or row.get("surface") or ""
        language = row.get("language") or "en"
        code = row.get("code") or ""
        reason = row.get("reason") or ""
        if not key:
            skipped.append({"key": "", "reason": "没有词元"})
            continue
        if reason and reason not in reasons:
            skipped.append({"key": key, "reason": reason})
            continue
        if wanted_codes and code and code not in wanted_codes:
            skipped.append({"key": key, "reason": "不在勾选的词表里"})
            continue
        if not code:
            skipped.append({"key": key, "reason": "没指定词表（打标签/建牌组要靠它）"})
            continue
        marker = (language, key)
        if marker in seen:
            skipped.append({"key": key, "reason": "同一个词重复出现"})
            continue
        seen.add(marker)
        if len(drafts) >= limit:
            skipped.append({"key": key, "reason": f"超过本次上限 {limit} 个"})
            continue

        material = {}
        if resources is not None:
            try:
                material = resources.lookup(
                    key, language, row.get("reading", ""), dict_entries
                )
            except Exception as exc:  # noqa: BLE001
                material = {"error": str(exc)}

        # 素材齐全才进补漏队列：单词 / 释义 / 音频 一个都不能少，例句在构建期已经补满。
        # 用户的要求是「不要有的有有的没有」，所以宁可少做几个词，也不出半截卡片。
        missing = []
        if not (material.get("translation") or material.get("definition")):
            missing.append("释义")
        if not (material.get("audio_name") or material.get("audio_path")):
            missing.append("音频")
        if not material.get("example"):
            missing.append("例句")
        if missing and resources is not None:
            skipped.append({"key": key, "reason": "素材不全：" + "、".join(missing)})
            continue

        label = row.get("label") or ""
        # 中文释义优先，后面再跟上英文/日文原文释义（两者相同就只留一份）
        definition = _combine_definition(
            material.get("translation"), material.get("definition")
        ) or (row.get("meaning") or "")
        drafts.append(
            {
                "key": key,
                "surface": row.get("surface") or key,
                "language": language,
                "code": code,
                "label": label,
                "reading": material.get("reading") or row.get("reading") or "",
                "phonetic": material.get("phonetic") or "",
                "definition": definition,
                "example": material.get("example") or "",
                "example_translation": material.get("example_translation") or "",
                "audio_path": material.get("audio_path") or "",
                "audio_name": material.get("audio_name") or "",
                "audio_source": material.get("audio_source") or "",
                "audio": "",
                "exam_tag": tag_for_code(code, label),
                "deck": deck_name_for(code, language, label),
                "source_line": source_line(material, code, language, label),
                "has_definition": bool(definition),
                "has_example": bool(material.get("example")),
                "has_audio": bool(material.get("audio_name") or material.get("audio_path")),
                "audio_ready": bool(material.get("audio_path")),
            }
        )
    return drafts, skipped


def plan_text(drafts, skipped, decks=None, media_count: int = 0) -> str:
    """生成前的确认文案：报清楚要动多少东西。"""
    decks = decks or sorted({d.get("deck") for d in drafts if d.get("deck")})
    not_ready = [d for d in drafts if not d.get("audio_ready")]
    lines = [
        f"将新增 {len(drafts)} 条笔记（只补界面显示「未覆盖」的词：词表里没有的 + 歧义词）。",
        f"新建/复用的牌组 {len(decks)} 个：{'、'.join(decks) if decks else '（无）'}",
        f"要写入的音频文件 {media_count or len([d for d in drafts if d.get('audio_path')])} 个。",
        "每条卡片都带：单词 / 音标·假名 / 中文释义 / 例句 / 例句译 / 音频（素材来自插件内置素材库）。",
        "笔记类型：应试补漏卡（自带模板，不动你现有的模板）。新牌组的新卡上限先设为 0，"
        "你检查完再手动放开。",
    ]
    if skipped:
        # 跳过的原因分种类报出来：「识别失败」是词表里有、你卡片里也有、只是匹配
        # 没认出（不重复制卡），和「素材不全」「超上限」是两码事。
        kinds: dict = {}
        for item in skipped:
            name = str(item.get("reason") or "其他")
            kinds[name] = kinds.get(name, 0) + 1
        detail = "、".join(
            f"{name} {count} 个"
            for name, count in sorted(kinds.items(), key=lambda pair: (-pair[1], pair[0]))
        )
        lines.append(f"跳过 {len(skipped)} 个词：{detail}。")
    if not_ready:
        lines.append(
            f"其中 {len(not_ready)} 条还没把音频包解压到本机"
            "：先去设置页点「一键构建素材库」，否则这些卡只有文字没有声音。"
        )
    lines.append("\n已经落地的卡片可以 Ctrl+Z 整组撤销。")
    return "\n".join(lines)


def group_by_deck(drafts) -> dict:
    """按补漏牌组名分组，返回 {牌组名: [草稿, …]}（保持原有顺序）。"""
    out: dict = {}
    for draft in drafts:
        out.setdefault(draft.get("deck") or DECK_PREFIX, []).append(draft)
    return out


def split_by_audio(drafts) -> tuple[list, list]:
    """按「音频文件是不是已经在本机」把草稿分成 ``(ready, not_ready)``。

    「每张补漏卡都必须有音频」是硬要求，所以音频包还没解压到本机时不许落半截卡片：
    界面拿这个结果决定是「先引导去构建素材库」还是「把缺音频的词跳过」。
    ``audio_path`` 只有素材文件真的在磁盘上时才有值（见 ``resources.lookup``）。
    """
    ready: list = []
    not_ready: list = []
    for draft in drafts or ():
        if draft.get("audio_path"):
            ready.append(draft)
        else:
            not_ready.append(draft)
    return ready, not_ready


def export_table(drafts, skipped=None) -> tuple[list[str], list[list]]:
    """导出 CSV 用的表头与数据。"""
    header = [
        "单词", "词表", "补漏牌组", "假名·音标", "释义", "例句", "例句译",
        "音频", "音频来源", "考试标签", "来源",
    ]
    rows = []
    for draft in drafts:
        rows.append(
            [
                draft.get("surface") or draft.get("key") or "",
                draft.get("code") or "",
                draft.get("deck") or "",
                " ".join(x for x in (draft.get("reading", ""), draft.get("phonetic", "")) if x),
                draft.get("definition") or "",
                draft.get("example") or "",
                draft.get("example_translation") or "",
                os.path.basename(draft.get("audio_path") or ""),
                draft.get("audio_source") or "",
                draft.get("exam_tag") or "",
                draft.get("source_line") or "",
            ]
        )
    for row in skipped or ():
        rows.append([row.get("key") or "", "（跳过）", "", "", "", "", "", "", "", "", row.get("reason") or ""])
    return header, rows
