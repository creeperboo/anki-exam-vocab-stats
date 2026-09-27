"""应试词汇统计插件 · 纯逻辑层。

这个模块只做「字符串进来、结论出去」的活，**不 import 任何 Anki 的东西**，
所以可以直接用普通 Python 跑测试，也能被 工具\\构建词库索引.py 复用
（索引构建和运行期必须用同一套归一规则，否则匹配会对不上）。

包含四块：

1. 清洗：HTML / 音频标签 / 模板变量 / 实体 / 标点
2. 归一：英文小写化与英美拼写变体、日文假名统一
3. 还原：英文词形还原（children -> child、ran -> run、better -> good）
4. 匹配：英文考试词表（四六级/考研/雅思/托福/GRE）、日语 JLPT（N1-N5）
"""

from __future__ import annotations

import gzip
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional, Sequence

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# ---------------------------------------------------------------- 常量

# (词表代码, 中文名, 分组)：分组只是给界面分行用
ENGLISH_EXAMS = (
    ("cet4", "四级", "cet"),
    ("cet6", "六级", "cet"),
    ("ky", "考研", "kaoyan"),
    ("ielts", "雅思", "overseas"),
    ("toefl", "托福", "overseas"),
    ("gre", "GRE", "overseas"),
)
ENGLISH_EXAM_CODES = tuple(code for code, _, _ in ENGLISH_EXAMS)
ENGLISH_EXAM_LABELS = {code: label for code, label, _ in ENGLISH_EXAMS}

# 词库里有、但默认不显示的两张词表。设置页可以打开，明细里也能看到。
OPTIONAL_EXAMS = (
    ("zk", "中考", "extra"),
    ("gk", "高考", "extra"),
)
ALL_EXAM_CODES = ENGLISH_EXAM_CODES + tuple(code for code, _, _ in OPTIONAL_EXAMS)
ALL_EXAM_LABELS = {
    **ENGLISH_EXAM_LABELS,
    **{code: label for code, label, _ in OPTIONAL_EXAMS},
}

# 合并口径：四六级并集
MERGED_EXAMS = (
    ("cet46", "四六级并集", ("cet4", "cet6")),
    ("jlpt", "JLPT 并集", ("n5", "n4", "n3", "n2", "n1")),
)

JLPT_LEVELS = (
    ("n5", "N5"),
    ("n4", "N4"),
    ("n3", "N3"),
    ("n2", "N2"),
    ("n1", "N1"),
)
JLPT_CODES = tuple(code for code, _ in JLPT_LEVELS)
JLPT_LABELS = {code: label for code, label in JLPT_LEVELS}

# 学习状态：复习 > 学习 > 未学习（同一词多张卡时取进度最高的）
STATE_ORDER = ("review", "learn", "new", "suspended")
STATE_LABELS = {
    "new": "未学习",
    "learn": "学习中",
    "review": "复习中",
    "suspended": "暂停/埋葬",
}

# ---------------------------------------------------------------- 打标签

# 明细页可以给命中的笔记打标签，名字统一挂在这个前缀下，好处是：
# 1. 用户在卡片浏览器里一句 tag:应试:: 就能筛出所有打过标签的卡；
# 2. 来源页切到「标签」维度时可以一键把这些自家标签排除，不然打完标签
#    整个来源图就只剩「应试::四级」自己了。
EXAM_TAG_PREFIX = "应试"
OWN_TAG_PREFIX = EXAM_TAG_PREFIX + "::"


def exam_tag_for_code(code: str) -> str:
    """词表码 -> 要打上去的标签名。

    合并码（四六级并集 / JLPT 并集）与未知码返回空串——并集不是一个具体词表，
    给它打标签说不清到底属于哪张表，所以只在明细里能筛、不能打标签。
    """
    if code in ALL_EXAM_LABELS:
        return f"{EXAM_TAG_PREFIX}::{ALL_EXAM_LABELS[code]}"
    if code in JLPT_LABELS:
        # 光写「N3」太容易和别的牌库撞名，日语等级统一带 JLPT- 前缀
        return f"{EXAM_TAG_PREFIX}::JLPT-{JLPT_LABELS[code]}"
    return ""


def is_own_tag(tag: str) -> bool:
    """是不是插件自己打上去的应试标签。"""
    return (tag or "").startswith(OWN_TAG_PREFIX)


def exam_tag_confirm_text(note_count: int, tags: Sequence[str], remove: bool = False) -> str:
    """打标签前的确认文案（纯函数，方便单测和探针核对）。"""
    verb = "去掉" if remove else "添加"
    tail = (
        "\n\n只去掉这些标签，笔记上别的标签不动；Ctrl+Z 可撤销。"
        if remove
        else "\n\n只加标签，不改字段、不改排期；Ctrl+Z 可撤销。"
    )
    return f"将给 {note_count} 条笔记{verb}这些标签：\n" + "、".join(tags) + tail


# ---------------------------------------------------------------- 清洗

_RE_TAG = re.compile(r"<[^>]*>")
_RE_SOUND = re.compile(r"\[sound:[^\]]*\]")
_RE_MUSTACHE = re.compile(r"\{\{[^{}]*\}\}")
_RE_ENTITY = re.compile(r"&[a-zA-Z][a-zA-Z0-9]{1,10};|&#\d{1,6};")
_RE_WS = re.compile(r"[\s\u3000]+")

# 语言判定用：只有假名是可靠的「日语」信号
_RE_KANA = re.compile(r"[\u3041-\u309f\u30a0-\u30ff]")
_RE_HAN = re.compile(r"[\u4e00-\u9fff]")
_RE_LATIN = re.compile(r"[A-Za-z]")


def strip_markup(text: str) -> str:
    """去掉 HTML、音频标签、模板变量、HTML 实体。"""
    if not text:
        return ""
    # 快路：这几个字符是四类标记各自的入口，一个都没有就说明是纯文本，
    # 直接原样返回，省掉下面 6 次正则替换（占大头的开销就在这儿）。
    if "<" not in text and "[" not in text and "{" not in text and "&" not in text:
        return text
    text = _RE_SOUND.sub(" ", text)
    text = _RE_MUSTACHE.sub(" ", text)
    text = _RE_TAG.sub(" ", text)
    # 常见实体先还原成字符，再让正则把剩下不认识的实体统一替成空格。
    # 顺序不能反：反了的话 &amp; 会先被替成空格，还原规则就等于白写。
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
    text = text.replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    text = _RE_ENTITY.sub(" ", text)
    return text


def clean_field(text: str) -> str:
    """字段取值的标准清洗：去标记、去首尾空白、压缩连续空白。"""
    if not text:
        return ""
    text = strip_markup(text)
    if not text:
        return ""
    # 快路：没有连续空白、没有换行/制表/全角空格、首尾也干净时，
    # 压缩空白这一步是空操作，可以跳过正则。
    if (
        "  " not in text
        and "\n" not in text
        and "\t" not in text
        and "\r" not in text
        and "\u3000" not in text
        and text == text.strip()
    ):
        return text
    return _RE_WS.sub(" ", text).strip()


# ---------------------------------------------------------------- 归一：英文

_RE_EN_TRIM = re.compile(r"^[^0-9a-z'\-]+|[^0-9a-z'\-]+$")


def normalize_english(text: str) -> str:
    """英文归一：小写、全角转半角、统一引号、去首尾标点、压缩空格。"""
    text = clean_field(text)
    if not text:
        return ""
    # 快路：纯小写 ASCII 字母词已经是归一后的样子，直接返回，跳过 NFKC 与替换表。
    if text.isascii() and text.islower() and text.isalpha():
        return text
    text = unicodedata.normalize("NFKC", text)
    text = (
        text.replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u00b4", "'")
    )
    text = text.lower().strip()
    text = _RE_EN_TRIM.sub("", text)
    return _RE_WS.sub(" ", text)


# 英美拼写：成对的替换规则，双向都生成候选
_SPELLING_PAIRS = (
    ("our", "or"),          # colour / color
    ("ise", "ize"),         # organise / organize
    ("isation", "ization"),
    ("yse", "yze"),         # analyse / analyze
    ("re", "er"),           # centre / center（只在词尾生效）
    ("ogue", "og"),         # catalogue / catalog
    ("ae", "e"),            # encyclopaedia / encyclopedia
    ("oe", "e"),            # foetus / fetus
    ("ll", "l"),            # travelled / traveled
)

# 英式单写 l ↔ 美式双写 l：fulfil/fulfill、skilful/skillful、enrolment/enrollment。
# 上面那条 ("ll","l") 只负责「双写削成单写」，这里把反方向补上——不然卡片写
# fulfil、词表里只有 fulfill 时两边对不上，一个已经覆盖的词会被报成未覆盖。
_LL_INSERT = re.compile(r"(?<!l)l(?=(ments|ment|ers|er|ors|or|ing|ed)$)")


def _word_is_vowel(char: str) -> bool:
    return char in "aeiou"


def _same_word_spelling(a: str, b: str) -> bool:
    """两个候选词形像不像「同一个词的两种拼法」（fibre / fiber）。

    判据故意保守：两个词至少要 5 个字母，长度最多差 3，而且差别只允许出现在
    最后 3 个字母里。这样 fibre/fiber、humour/humor、litre/liter、realise/realize、
    program/programme 会被当成一个词；而 does → do / doe、zzz → aaa / bbb 这种
    真歧义（词太短，或差别不在词尾）不会被误并。
    """
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    if len(short) < 5 or len(long_) - len(short) > 3:
        return False
    head = len(short) - 3
    if head < 1 or a[:head] != b[:head]:
        return False
    return a[head:] != b[head:]


def _same_phrase(a: str, b: str) -> bool:
    """两个写法像不像「同一个短语的连字符/空格变体」（co-operative / cooperative）。

    判据只有一个：把连字符和空格全去掉之后完全一样。比英美拼写那种「词尾近似」
    严格得多，所以不会把 make up 跟 makeup 之外的词误并。
    """
    if not a or not b or a == b:
        return False
    return a.replace("-", "").replace(" ", "") == b.replace("-", "").replace(" ", "")


def spelling_variants(word: str) -> list[str]:
    """生成英美拼写变体（含原词）。只做保守替换，宁缺勿错。

    每条规则只对**原词**用一次，结果之间不再互相套用——套用会让
    ``apples`` 这种词滚出 ``appllaes`` 之类的一堆垃圾写法，长句上更是几十倍地
    拖慢匹配。英美拼写差异只出现在词尾附近，本来也不需要级联。
    """
    out = [word]
    # 超长串（例句、整段文本）里 `xx in word` 几乎必然命中，替换出来的东西没意义，
    # 直接跳过这一维度，省下的时间很可观。
    if len(word) > 24:
        return out
    for left, right in _SPELLING_PAIRS:
        for src, dst in ((left, right), (right, left)):
            # 每条规则都加了长度/邻近字符的护栏，专门挡 four->for、care->carer 这类误伤
            if len(word) < 5:
                continue
            if src == "re" and word.endswith("re") and word[-3] not in "aeiou":
                out.append(word[:-2] + "er")
            elif src == "er" and len(word) >= 5 and word.endswith("er") and word[-3] not in "aeiou":
                out.append(word[:-2] + "re")
            elif src == "our" and len(word) < 6:
                continue
            elif src == "ll" and "ll" in word:
                out.append(word.replace("ll", "l"))
            elif src in word:
                out.append(word.replace(src, dst))
    if word.endswith("l") and not word.endswith("ll"):
        out.append(word + "l")
    doubled = _LL_INSERT.search(word)
    if doubled:
        out.append(word[: doubled.start()] + "ll" + word[doubled.start() + 1 :])
    # 去重且保持顺序
    seen: list[str] = []
    for item in out:
        if item and item not in seen:
            seen.append(item)
    return seen


def _dedupe(items) -> list[str]:
    """去重且保持顺序（空串丢掉）。"""
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def phrase_variants(word: str) -> list[str]:
    """含连字符 / 空格的短语变体（含原词）。

    词表里既有 ``makeup`` 也有 ``make-up``、既有 ``percent`` 也有 ``per cent``，
    卡片上写成哪种都算一个词。这里只做「连字符 ↔ 无连字符 ↔ 空格」三种写法互换，
    不做任何语序调整，所以不会把 ``a lot`` 和 ``lot a`` 混起来。
    """
    out = [word]
    if "-" in word:
        out.append(word.replace("-", ""))
        out.append(word.replace("-", " "))
    if " " in word:
        out.append(word.replace(" ", ""))
        out.append(word.replace(" ", "-"))
    return _dedupe(out)


def form_variants(word: str) -> list[str]:
    """一个英文表面形式的全部「同一个词」的写法（含原词）。

    英美拼写（colour/color）与短语连字符（make-up/makeup）两个维度都要展开，
    而且两个维度要互相组合——``colour-code`` 既可能是 ``color-code``，
    也可能是 ``colourcode`` 或 ``colorcode``。

    展开只做一轮笛卡尔组合：先给原词取拼写变体、再各自取连字符/空格变体，
    然后拿原词的短语变体各自取一次拼写变体。**不再拿结果互相当输入**，
    否则 ``apples`` 会被反复替换成 ``appllaes`` 之类的假写法，长文本上直接爆量。
    """
    out: list[str] = [word]
    for item in spelling_variants(word):
        out.append(item)
        out.extend(phrase_variants(item))
    for item in phrase_variants(word):
        out.extend(spelling_variants(item))
    return _dedupe(out)


# ---------------------------------------------------------------- 去变形：英文

# 英语去变形规则表。词尾替换的骨架直接整理自本机 fushi 应用的
# ``transforms/en.json``（Yomitan 规范的 16 组规则），再补进几条最常用的派生后缀
# （-ness / -ment / -tion / -able / -ible / -ful / -less / -or）。
#
# 每条 (词尾, 替换成)：把词尾换成替换串就得到一个候选词元。
# **顺序＝采纳优先级**：越靠前越可信，逐条试、第一个能命中词表的才被采纳。
# 只有「原词自己完全命不中」时才会走到这张表，所以放宽规则不会污染直接命中。
EN_SUFFIX_RULES = (
    # 名词复数
    ("ies", "y"),
    ("ves", "fe"),
    ("ves", "f"),
    ("es", ""),
    ("s", ""),
    # 所有格
    ("'s", ""),
    ("s'", "s"),
    # 过去式：双写辅音的先来，否则 -ed 会先把 stopped 削成 stoppe
    ("cked", "c"), ("gged", "g"), ("bbed", "b"), ("dded", "d"),
    ("kked", "k"), ("lled", "l"), ("mmed", "m"), ("nned", "n"),
    ("pped", "p"), ("rred", "r"), ("ssed", "s"), ("tted", "t"), ("zzed", "z"),
    ("laid", "lay"), ("paid", "pay"), ("said", "say"),
    ("ied", "y"),
    ("ed", "e"),
    ("ed", ""),
    # 现在分词
    ("cking", "c"), ("gging", "g"), ("bbing", "b"), ("dding", "d"),
    ("kking", "k"), ("lling", "l"), ("mming", "m"), ("nning", "n"),
    ("pping", "p"), ("rring", "r"), ("ssing", "s"), ("tting", "t"), ("zzing", "z"),
    ("ying", "ie"),
    ("ing", "e"),
    ("ing", ""),
    ("in'", "ing"),
    # 古体缩写
    ("'d", "ed"),
    # 副词
    ("ily", "y"),
    ("ally", "al"),
    ("lly", "ll"),
    ("lly", "l"),
    ("ly", "le"),
    ("ly", ""),
    # 比较级 / 最高级
    ("ier", "y"),
    ("iest", "y"),
    ("bber", "b"), ("gger", "g"), ("dder", "d"),
    ("mmer", "m"), ("nner", "n"), ("tter", "t"), ("rrer", "r"),
    ("bbest", "b"), ("ggest", "g"), ("ddest", "d"),
    ("mmest", "m"), ("nnest", "n"), ("ttest", "t"),
    ("est", "e"),
    ("est", ""),
    ("er", "e"),
    ("er", ""),
    # 派生后缀：形容词 / 名词 / 施事者
    ("fulness", "ful"),
    ("lessness", "less"),
    ("iness", "y"),
    ("ness", ""),
    ("ment", ""),
    ("ation", "e"),
    ("tion", "te"),
    ("sion", "de"),
    ("ably", "able"),
    ("ibly", "ible"),
    ("able", ""),
    ("ible", ""),
    ("ful", ""),
    ("less", ""),
    ("ical", "ic"),
    ("ator", "ate"),
    ("ctor", "ct"),
    ("ist", ""),
    ("ism", ""),
    ("or", "e"),
    ("or", ""),
)

# 否定 / 反向前缀。也要「剥掉之后真能命中词表」才采纳：
# dishonest -> honest、unacceptable -> acceptable -> accept、regain -> gain。
EN_PREFIX_RULES = (
    ("un", ""),
    ("in", ""),
    ("im", ""),
    ("dis", ""),
    ("non", ""),
    ("re", ""),
    ("mis", ""),
    ("over", ""),
    ("under", ""),
    ("pre", ""),
    ("post", ""),
    ("anti", ""),
    ("inter", ""),
    ("multi", ""),
    ("semi", ""),
    ("sub", ""),
    ("super", ""),
    ("trans", ""),
    ("out", ""),
)

# 剥离前缀后至少要留这么多字母，否则 in -> "" 之类会把 2 字母残留当词
_PREFIX_MIN_STEM = 3


def deform_candidates(word: str) -> list[str]:
    """一轮去变形：给一个词，列出所有「去掉一层词尾/前缀」的候选。"""
    out: list[str] = []
    length = len(word)
    for suffix, replacement in EN_SUFFIX_RULES:
        if length <= len(suffix) + 1 or not word.endswith(suffix):
            continue
        stem = word[: -len(suffix)]
        out.append(stem + replacement)
        # 双写辅音去重（stopped -> stop / hopping -> hop）：只当词尾是两个相同辅音时
        if len(stem) > 2 and stem[-1] == stem[-2] and not _word_is_vowel(stem[-1]):
            out.append(stem[:-1] + replacement)
    for prefix, replacement in EN_PREFIX_RULES:
        if length <= len(prefix) + _PREFIX_MIN_STEM or not word.startswith(prefix):
            continue
        out.append(replacement + word[len(prefix):])
    return [item for item in _dedupe(out) if item != word]


def english_deform_candidates(word: str, depth: int = 2, limit: int = 64) -> list[str]:
    """多层去变形（默认两层）。

    一层不够用：``unacceptable`` 先要剥掉 ``un`` 得到 ``acceptable``，
    再要剥掉 ``able`` 才得到 ``accept``；``incredibly`` -> ``incredible`` -> 无。
    这里按「层」推进，前一层的结果先入队，保证短路径优先被采纳。
    """
    out: list[str] = []
    frontier = [word]
    for _ in range(max(int(depth), 1)):
        nxt: list[str] = []
        for item in frontier:
            for candidate in deform_candidates(item):
                if candidate == word or candidate in out:
                    continue
                out.append(candidate)
                nxt.append(candidate)
                if len(out) >= limit:
                    return out
        frontier = nxt
        if not frontier:
            break
    return out


# ---------------------------------------------------------------- 归一：日文

_RE_JA_DROP = re.compile(r"[\u301c\uff5e~\u30fb\u309b\u309c\u2019\u2018 '\u3000]")
_RE_JA_PAREN = re.compile(r"[（(][^（()）]*[）)]")
_RE_JA_KEEP = re.compile(r"[^0-9a-z\u3041-\u309f\u30a0-\u30ff\u4e00-\u9fff\u3400-\u4dbf]")
_LONG_MARK = "\u30fc"  # ー


def kata_to_hira(text: str) -> str:
    """片假名转平假名（只动 U+30A1-U+30F6 这一段）。"""
    out = []
    for ch in text:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6:
            out.append(chr(code - 0x60))
        else:
            out.append(ch)
    return "".join(out)


def normalize_japanese(text: str, drop_long_mark: bool = False) -> str:
    """日文归一：半角转全角、片假名转平假名、去括号内容与装饰符号。

    默认保留长音符「ー」——因为 ビル（大楼）和 ビール（啤酒）只差这一个符号，
    删掉会直接造成误判。需要「忽略长音符」时由调用方显式传 drop_long_mark=True。
    """
    text = clean_field(text)
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    # 全角拉丁字母经 NFKC 会变成大写，这里统一压成小写，免得「ＡＢＣ」被当成非日文丢掉
    text = text.lower()
    text = _RE_JA_PAREN.sub("", text)
    text = _RE_JA_DROP.sub("", text)
    text = kata_to_hira(text)
    text = _RE_JA_KEEP.sub("", text)
    if drop_long_mark:
        text = text.replace(_LONG_MARK, "")
    return text


_JA_SURU_TAILS = ("します", "しました", "しない", "した", "する")

# 接头词：日语里 お/ご 是可加可不加的美化语，同一个词两种写法都常见。
# 只在「剥掉之后真能命中词表」时采纳，不做任何主动改写。
_JA_PREFIXES = ("お", "ご", "御")


def japanese_base_forms(word: str) -> list[str]:
    """给出「〜する」复合动词的词干候选（保守，只削尾巴）。"""
    out = [word]
    for tail in _JA_SURU_TAILS:
        if word.endswith(tail) and len(word) > len(tail):
            out.append(word[: -len(tail)])
    return out


def japanese_prefix_candidates(word: str) -> list[str]:
    """给出「去掉接头词 お/ご/御」之后的候选写法（含原词）。"""
    out = [word]
    for prefix in _JA_PREFIXES:
        if word.startswith(prefix) and len(word) > len(prefix):
            out.append(word[len(prefix):])
    return out


# ---------------------------------------------------------------- 去变形：日文

# 日语活用还原的规则表（片假名/平假名词尾替换），数据来自 fushi 的
# ``transforms/ja.json``（Yomitan 规范，去重后 820 条），构建期产出
# ``data/ja_deform.json.gz``。规则本身放宽没关系：只有候选**真能命中词表**时才采纳，
# 所以 わたし 这种本来就在词表里的词直接走命中分支，根本轮不到还原。
_JA_DEFORM_TABLE: Optional[dict] = None
_JA_DEFORM_LIMIT = 6  # 最长词尾长度，超过就不查表（表里最长的就是 6 个假名）


def ja_deform_table() -> dict:
    """加载日语去变形规则表：{词尾: [替换成, …]}。文件缺失时返回空表。"""
    global _JA_DEFORM_TABLE
    if _JA_DEFORM_TABLE is not None:
        return _JA_DEFORM_TABLE
    table: dict[str, list[str]] = {}
    try:
        blob = load_json_gz(os.path.join(DATA_DIR, "ja_deform.json.gz"))
        pairs = blob.get("pairs") or ()
    except Exception:
        pairs = ()
    for pair in pairs:
        if not pair:
            continue
        source = pair[0]
        target = pair[1] if len(pair) > 1 else ""
        if not source:
            continue
        table.setdefault(source, []).append(target)
    _JA_DEFORM_TABLE = table
    return table


def japanese_deform_candidates(word: str, limit: int = 48) -> list[str]:
    """按活用规则表给出候选词元（长的词尾优先，含原词之外的写法）。

    只做「词尾替换」，不做任何主动改写；调用方必须再用词表验证一遍。
    """
    table = ja_deform_table()
    if not table or not word:
        return []
    out: list[str] = []
    longest = min(_JA_DEFORM_LIMIT, len(word) - 1)
    for size in range(longest, 0, -1):
        for target in table.get(word[-size:], ()):
            candidate = word[:-size] + target
            if candidate and candidate != word and candidate not in out:
                out.append(candidate)
                if len(out) >= limit:
                    return out
    return out


# ---------------------------------------------------------------- 拆分

_RE_EN_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")


def split_english_words(text: str) -> list[str]:
    """从长文本（例句）里分词。只取字母开头的英文词，避免把数字算进来。"""
    return _RE_EN_WORD.findall(clean_field(text))


def classify_language(values: Sequence[str]) -> str:
    """判断一组字段值主要是英语、日语还是别的。

    只有「假名」是可靠的日语信号。汉字既可能是中文也可能是日文，单看汉字定不了性
    ——否则像 English-CEFR 这类「英文单词 + 中文释义」的牌库会被整块判成日语，
    接着把真正的英语取词字段当成「日语卡里的英文释义」给关掉（实测踩过这个坑）。
    """
    kana = latin = han = other = 0
    for raw in values:
        text = clean_field(raw)
        if not text:
            continue
        if _RE_KANA.search(text):
            kana += 1
        elif _RE_LATIN.search(text):
            latin += 1
        elif _RE_HAN.search(text):
            han += 1
        else:
            other += 1
    if kana and kana * 2 >= latin:
        return "ja"
    if latin:
        return "en"
    if kana or han:
        return "ja"
    return "other"


# ---------------------------------------------------------------- 匹配结果


@dataclass
class Match:
    """一个词条的匹配结论。"""

    key: str = ""                       # 词元 / 词条（去重用的键）
    surface: str = ""                   # 卡片上原始的写法
    language: str = ""                  # en / ja
    exams: frozenset = frozenset()      # 命中哪些英文考试（cet4…）
    levels: frozenset = frozenset()     # 命中哪些日语等级（n1…）
    confident: bool = True              # False = 待确认（同形异义，未计入覆盖）
    matched_by: str = ""                # own / lemma / spelling / suffix / reading
    alternatives: tuple = ()            # 待确认时的候选
    meaning: str = ""                   # 日语词条释义（明细里好认）

    @property
    def hit(self) -> bool:
        """是否算「命中词表」。待确认不算命中，否则会虚高覆盖率。"""
        return bool(self.confident and (self.exams or self.levels))


NO_MATCH = Match()


def load_json_gz(path: str) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def _tags_to_set(value) -> frozenset:
    if not value:
        return frozenset()
    if isinstance(value, str):
        return frozenset(value.split())
    return frozenset(value)


def _freq_rank(value) -> int:
    """词频排名：越小越常见；0 / 缺失都当「没有数据」统一成 0。"""
    try:
        got = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return got if got > 0 else 0


# ---------------------------------------------------------------- 英文匹配


class EnglishMatcher:
    """英文考试词表匹配 + 词形还原。"""

    def __init__(self, index: dict, lemma_index: dict, merge_lemma: bool = True):
        self.meta = index.get("meta", {})
        self.word_exam = index.get("word_exam", {})
        self.group_exam = index.get("group_exam", {})
        self.freq = index.get("freq", {})
        self.lemma = lemma_index.get("lemma", {})
        self.merge_lemma = merge_lemma
        self.exam_counts = index.get("exams", {})
        self._cache: dict[str, Match] = {}

    def merged_total(self, members) -> int:
        """若干考试并集的分母（按词元组去重，和单个考试的算法保持一致）。

        四六级并集不能用 一级 + 六级 相加：同一个词两个标签都有，会被算两遍。
        """
        wanted = set(members)
        total = sum(
            1 for tags in self.group_exam.values() if _tags_to_set(tags) & wanted
        )
        if total:
            return total
        # 索引里没有词元组数据时（理论上不会）只能退化成各分项相加，这个值偏大。
        return sum(int(self.exam_counts.get(code, 0)) for code in wanted)

    # -- 内部：一个表面形式的候选词元及其标签
    def _candidates(self, word: str) -> list[tuple[str, frozenset, int]]:
        """返回 [(词元, 标签, 词频), …]。

        merge_lemma=True（默认）：把词元组的标签并集一起算进来，
        所以「认识 child」就算「认识 children」。
        英美拼写变体（colour / color）与短语连字符变体（make-up / makeup）
        也一起当候选，两边归成同一个词。
        merge_lemma=False：只认原词自己带的标签，不做任何词形还原。
        """
        if not self.merge_lemma:
            return [(word, _tags_to_set(self.word_exam.get(word)), 0)]
        keys: list[str] = []
        for form in form_variants(word):
            keys.append(form)
            lemma = self.lemma.get(form)
            if isinstance(lemma, str):
                keys.append(lemma)
            elif isinstance(lemma, list):
                keys.extend(lemma)
        rows = []
        for key in dict.fromkeys(keys):
            tags = _tags_to_set(self.group_exam.get(key) or self.word_exam.get(key))
            rows.append((key, tags, int(self.freq.get(key, 0) or 0)))
        return rows

    def _pick(self, word: str) -> Optional[Match]:
        direct = set(form_variants(word))
        rows = self._candidates(word)
        with_tags = [r for r in rows if r[1]]
        if not with_tags:
            return None

        # 卡片上写的那个写法，只要它自己在词表里，它自己的标签也必须算进来。
        # 例：analyse 和 analyze 是同一个词的两种写法，analyse 自己是雅思词；
        # 归到 analyze 词元上时雅思标签不能跟着丢，否则这张雅思卡会被算成未覆盖。
        own_tags = frozenset().union(*(r[1] for r in with_tags if r[0] in direct))

        # 规则 1/2/3：候选里挑「带的考试标签最多」的；一样多再按词频。
        # 例：better 同时属于 good 和 well 两个词族，good 带的标签多，取 good，
        # 这样 better 才算覆盖到四六级（方案里「better 和 good 取并集」的意图）。
        with_tags.sort(key=lambda r: (-len(r[1]), r[2] if r[2] > 0 else 10**9, r[0]))
        best = with_tags[0]

        # 连字符 / 空格两种写法是同一个词（co-operative / cooperative、
        # make-up / makeup）：标签并起来当一个词算，去重键优先用卡片上的写法，
        # 免得「词表里写 co-operative、卡片写 cooperative」被算成两个词。
        same_phrase = [
            r for r in with_tags if r[0] != best[0] and _same_phrase(best[0], r[0])
        ]
        if same_phrase:
            merged = frozenset().union(*(r[1] for r in [best] + same_phrase), own_tags)
            if best[0] == word:
                return Match(key=best[0], language="en", exams=merged, matched_by="own")
            for row in same_phrase:
                if row[0] == word:
                    # 卡片写法就在词表里：用它当去重键，标签取两个写法的并集
                    return Match(key=word, language="en", exams=merged, matched_by="own")
            return Match(key=best[0], language="en", exams=merged, matched_by="spelling")

        if best[0] == word:
            how = "own"
        elif best[0] in direct:
            how = "spelling"
        else:
            how = "lemma"
        # 「标签数一样多」还不足以下「歧义」的结论：方案里词频是下一道消歧，
        # 只有标签数与词频都分不出高下，才算真歧义（不猜，进待确认）。
        # 少了词频这一道，better 这种「good / well 标签数打平」的词会被误判成
        # 待确认，明明词表里有它，明细里却显示未覆盖。
        tie = [
            r
            for r in with_tags[1:]
            if r[1] != best[1]
            and len(r[1]) == len(best[1])
            and _freq_rank(r[2]) == _freq_rank(best[2])
        ]
        # 英美拼写对（fibre/fiber、humour/humor、realise/realize、program/programme）
        # 是同一个词的两种写法：标签并起来当一个词算，不能当歧义丢掉——真丢了会
        # 让「认识 fibre 但词表里写 fiber」这种词白挂一条待确认，覆盖率也少算。
        same_spelling = [r for r in tie if _same_word_spelling(best[0], r[0])]
        real_tie = [r for r in tie if not _same_word_spelling(best[0], r[0])]
        if real_tie:
            # 标签数量并列但内容不同 → 无法确定，标成待确认，不计入覆盖
            return Match(
                key=best[0],
                language="en",
                exams=best[1],
                confident=False,
                matched_by=how,
                alternatives=tuple(sorted(r[0] for r in with_tags)),
            )
        exams = best[1]
        if same_spelling:
            exams = frozenset().union(best[1], *(r[1] for r in same_spelling))
        exams = frozenset().union(exams, own_tags)
        return Match(key=best[0], language="en", exams=exams, matched_by=how)

    def resolve(self, surface: str) -> Match:
        """把一个表面词解析成 match；解析不出就返回 NO_MATCH。"""
        word = normalize_english(surface)
        if not word:
            return NO_MATCH
        if word in self._cache:
            hit = self._cache[word]
            return Match(**{**hit.__dict__, "surface": surface})

        result = self._pick(word)
        if result is None and _deformable(word):
            # 规则 4：去变形规则表兜底（原词 -> 一层 -> 两层）。
            # 只有能解析出词表结果才采纳，绝不凭空造词。
            for base in _deform_targets(word):
                # 便宜的先筛一道：词库里连影子都没有的词干不值得再解析一次
                # （候选动辄上千个，每个都走完整解析会把长词拖到毫秒级）。
                if not self._plausible(base):
                    continue
                got = self._pick(base)
                if got is not None and (got.exams or got.confident):
                    result = Match(
                        key=got.key,
                        language="en",
                        exams=got.exams,
                        confident=got.confident,
                        matched_by="suffix",
                        alternatives=got.alternatives,
                    )
                    break
        if result is None:
            result = Match(key=word, language="en", matched_by="none")

        self._cache[word] = result
        return Match(**{**result.__dict__, "surface": surface})

    def _plausible(self, base: str) -> bool:
        """词干在词库里有没有影子（只做字典查找，不做解析）。"""
        for table in (self.group_exam, self.word_exam, self.lemma):
            if base in table:
                return True
        return any(
            form in self.group_exam or form in self.word_exam or form in self.lemma
            for form in spelling_variants(base)
            if form != base
        )


def _deform_targets(word: str) -> list[str]:
    """给一个英文词列出全部去变形候选——先把拼写/短语变体也铺开。"""
    bases: list[str] = []
    for form in form_variants(word):
        bases.append(form)
    for form in list(bases):
        bases.extend(english_deform_candidates(form))
    return [item for item in _dedupe(bases) if item != word]


def _deformable(word: str) -> bool:
    """去变形只对「单个英文词」有意义。

    例句、长短语既没有词形变化，又因为候选之间互相嵌套而极慢——一个句子
    要跑 25 毫秒，一个字段 60 条样本就是 1.5 秒。这里直接挡掉：带空格的、
    超长的都不走去变形这条兜底。
    """
    return " " not in word and len(word) <= 32


def suffix_candidates(word: str) -> list[str]:
    """去变形候选（保留旧名字，供外部/测试调用）。

    老实现里是一串手写的 if，现在统一走 ``EN_SUFFIX_RULES`` 规则表；
    行为上只多不少：老规则能出的候选，新表同样能出。
    """
    return english_deform_candidates(word)


# ---------------------------------------------------------------- 日语匹配


class JapaneseMatcher:
    """日语 JLPT 词表匹配（保守归一，不做词干还原）。"""

    def __init__(self, index: dict):
        self.meta = index.get("meta", {})
        self.level_counts = index.get("levels", {})
        self.by_word = index.get("by_word", {})
        self.by_reading = index.get("by_reading", {})
        self.reading_word = index.get("reading_word", {})
        self.ambiguous_readings = set(index.get("ambiguous_readings", ()))
        self.meanings = index.get("meanings", {})
        self._cache: dict[tuple, Match] = {}

    def merged_total(self, members) -> int:
        """若干等级并集的分母（跨级词只算一次）。"""
        wanted = set(members)
        total = sum(
            1 for tags in self.by_word.values() if _tags_to_set(tags) & wanted
        )
        if total:
            return total
        return sum(int(self.level_counts.get(code, 0)) for code in wanted)

    def _levels_of_word(self, word: str) -> frozenset:
        return _tags_to_set(self.by_word.get(word))

    def _canonical_for_reading(self, reading: str, word: str) -> str:
        """读音命中时拿哪个写法当去重键。

        必须优先挑「词表里真有的写法」——不然同一个词从不同牌库进来时，一个给
        汉字、一个给假名，就会各算一份，覆盖率白白虚低。只有词表里都没有时，
        才退回索引里的标准写法、卡片写法、最后才用读音本身。
        """
        candidates = [self.reading_word.get(reading), word, reading]
        for candidate in candidates:
            if candidate and candidate in self.by_word:
                return candidate
        return self.reading_word.get(reading) or word or reading

    def _levels_of_reading(self, reading: str, word: str) -> Match | None:
        value = self.by_reading.get(reading)
        if not value:
            return None
        levels = _tags_to_set(value)
        if not levels:
            return None
        # 这个假名唯一对应一个词条时，用它当去重键（免得同一词从不同牌组算两份）
        canonical = self._canonical_for_reading(reading, word)
        if reading in self.ambiguous_readings:
            # 同一个假名对应多个词条，而且等级不一致 → 不猜，进「待确认」
            return Match(
                key=canonical,
                language="ja",
                levels=levels,
                confident=False,
                matched_by="reading",
            )
        return Match(key=canonical, language="ja", levels=levels, matched_by="reading")

    def resolve(self, surface: str, reading: str = "") -> Match:
        word = normalize_japanese(surface)
        read = normalize_japanese(reading)
        if not word and not read:
            return NO_MATCH
        cache_key = (word, read)
        if cache_key in self._cache:
            hit = self._cache[cache_key]
            return Match(**{**hit.__dict__, "surface": surface})

        result = self._resolve_once(word, read)
        if result is None and _LONG_MARK in word:
            # 「忽略长音符」只作为兜底，避免 ビル / ビール 这类词被硬合并
            result = self._resolve_once(word.replace(_LONG_MARK, ""), read.replace(_LONG_MARK, ""))
        if result is None:
            result = Match(key=word or read, language="ja", matched_by="none")
        else:
            result.meaning = self.meanings.get(result.key, "")

        self._cache[cache_key] = result
        return Match(**{**result.__dict__, "surface": surface})

    def _resolve_once(self, word: str, read: str) -> Match | None:
        def lookup(base: str) -> Match | None:
            """一个候选写法：先当汉字表记查，再当假名读音查。"""
            if not base:
                return None
            levels = self._levels_of_word(base)
            if levels:
                return Match(key=base, language="ja", levels=levels, matched_by="own")
            return self._levels_of_reading(base, base)

        # 1) 汉字表记直接命中
        if word:
            levels = self._levels_of_word(word)
            if levels:
                return Match(key=word, language="ja", levels=levels, matched_by="own")
        # 2) 假名读音兜底。卡片没有单独的假名字段时，把取词字段本身当读音试一次
        #    （Kaishi 这类牌库有些词条直接就是假名）。
        reading_key = read or word
        if reading_key:
            got = self._levels_of_reading(reading_key, word)
            if got is not None:
                return got
        # 3) 「〜する」复合动词削尾巴后再试
        for base in japanese_base_forms(word):
            if base == word:
                continue
            got = lookup(base)
            if got is not None:
                return got
        # 4) 接头词 お/ご/御 兜底：お金 -> 金、ご飯 -> 飯。只有剥掉后真能命中才采纳，
        #    否则原样返回「没命中」，不进待确认。
        for base in japanese_prefix_candidates(word):
            if base == word:
                continue
            got = lookup(base)
            if got is not None:
                return _relabel(got, "prefix")
        # 5) 活用还原（受控）：按 fushi 的活用规则表生成候选，**只采纳能命中词表的**。
        #    覆盖 読みます -> 読む、食べなかった -> 食べる、勉強しません -> 勉強する
        #    这一类「卡片上是活用形、词表里只有辞书形」的情况。
        for source in (word, read):
            for base in japanese_deform_candidates(source):
                got = lookup(base)
                if got is not None:
                    return _relabel(got, "deform")
        return None


def _relabel(match: Match, how: str) -> Match:
    """换一个 matched_by 标签，其余原样保留（活用在哪个分支命中要能看出来）。"""
    return Match(
        key=match.key,
        language=match.language,
        exams=match.exams,
        levels=match.levels,
        confident=match.confident,
        matched_by=how,
        alternatives=match.alternatives,
        meaning=match.meaning,
    )


# ---------------------------------------------------------------- 字段识别

_GOOD_NAME_HINTS = (
    "word", "vocab", "expression", "term", "单词", "词条", "生词", "词汇",
    "vocabkanji", "vocabfurigana", "spelling",
)
# 字段名里出现这些，就说明它是「给人看的辅助信息」，不是取词字段。
# 打分时统一扣 25 分，另外还会被 is_word_field_name 硬否决掉。
_BAD_NAME_HINTS = (
    "sentence", "sent", "example", "definition", "def", "meaning", "translation",
    "chinese", "audio", "sound", "pronunciation", "phonetic", "pitch", "accent",
    "partofspeech", "pos", "grade", "classification", "frequency", "order",
    "tag", "source", "usage", "grammar", "remark", "comment", "header",
    "occlusion", "image", "picture", "extra", "plus", "alt", "text",
    "learnable", "question", "option", "selection", "description",
    "例句", "释义", "词义", "意思", "翻译", "中文", "音标", "词性", "分类",
    "等级", "级别", "频率", "排序", "序号", "来源", "文法", "备注", "补充",
    "题目", "问题", "选项", "解析", "答案", "说明", "描述", "图片", "音频",
)

# 上面这些之外，还有「读音字段」也不能当取词字段；它们另有大用，见 analysis 的
# looks_like_reading：日语卡的假名读法正好靠它兜底。
_READING_NAME_HINTS = (
    "reading", "furigana", "kana", "假名", "读音", "よみ", "yomi",
)
_VETO_NAME_HINTS = _BAD_NAME_HINTS + _READING_NAME_HINTS
# 短名字只能靠全等匹配，否则 "id" 会误伤一大片长得像的字段名。
_VETO_NAME_EXACT = frozenset(
    {"id", "nid", "cid", "no", "num", "seq", "index", "type", "order", "pos",
     "level", "note", "notes", "tags"}
)


def is_word_field_name(name: str) -> bool:
    """字段名像不像「取词字段」。

    为什么非要按名字硬判：像 ECDICT 里 noun / verb / adverb 这些词本身就带
    cet4 / gk 标签，一个装着词性的字段（wordPartOfSpeech）会拿到 100% 命中率，
    一个装着「句子类型」的字段（SentType）同样能命中，光看分数根本拦不住。
    实测 English-CEFR 和 eggrolls-JLPT10k 都踩过这个坑，所以这里按字段名一票否决。
    真被误伤了也不要紧：设置页每个字段都能手动指定。
    """
    lowered = (name or "").strip().lower()
    if not lowered:
        return False
    if lowered in _VETO_NAME_EXACT or lowered.endswith("id"):
        return False
    return not any(hint in lowered for hint in _VETO_NAME_HINTS)


# 「像例句」的判据要看**原始值**：clean_field 会把 HTML / [sound:] / {{}} 洗掉，
# 洗完再看就什么都看不出来了，所以这里单独拿没清洗过的字符串判断。
# 注意：英文句点必须算进去。漏了它的话，'He abandoned the plan after the
# meeting ended.' 这种典型例句会被判成「不像例句」，整列例句就漏过兜底。
_RE_SENTENCE_END = re.compile(r"[.。！？!?…;；]")
_SENTENCE_MIN_CHARS = 25
_SENTENCE_LONG_CHARS = 60


def looks_like_sentence(values: Sequence[str]) -> bool:
    """这一列装的像不像整句/长文本（例句、解析、段落）。

    为什么需要它：例句里当然夹着应试词，命中率可能很高，但它不是取词字段。
    字段名能挡住大部分（Example / Sentence 之类），挡不住的那种（有些牌库把
    例句塞进 vocabulary 字段）就靠这里兜底，并且设置页会明说原因。
    """
    texts = [v.strip() for v in values if v and v.strip()]
    if not texts:
        return False
    hits = 0
    for text in texts:
        if "<" in text or "[sound:" in text or "{{" in text:
            hits += 1
        elif len(text) >= _SENTENCE_LONG_CHARS:
            hits += 1
        elif len(text) >= _SENTENCE_MIN_CHARS and _RE_SENTENCE_END.search(text):
            hits += 1
        elif len(text) >= 40 and ("," in text or "，" in text):
            hits += 1
    # 过半像句子才算：偶尔混进一条长词条，不该把整列判成例句
    return hits * 2 >= len(texts)


def score_field(name: str, values: Sequence[str], matchers: dict) -> dict:
    """给一个字段打分，判断它像不像「取词字段」。

    matchers: {"en": EnglishMatcher|None, "ja": JapaneseMatcher|None}
    返回 {"score", "language", "hit_rate", "hits", "samples",
          "looks_like_sentence", "words"}

    hit_rate 就是设置页上的「应试词命中率」：抽样的非空内容里有多少条命中了
    ECDICT / JLPT 词表。它只回答「这一列像不像应试词」，不进入任何统计数字。
    """
    raw = [v for v in values if v and v.strip()]
    samples = [clean_field(v) for v in values]
    samples = [sample for sample in samples if sample]
    sentence = looks_like_sentence(raw)
    if not samples:
        return {
            "score": -100.0,
            "language": "other",
            "hit_rate": 0.0,
            "hits": 0,
            "samples": 0,
            "looks_like_sentence": sentence,
            "words": [],
        }

    language = classify_language(samples)
    words: list[str] = []
    if language == "en":
        for sample in samples:
            words.extend(split_english_words(sample)[:3])
    else:
        words = list(samples)

    hits = 0
    for sample in samples:
        if language == "en":
            matcher = matchers.get("en")
            got = matcher.resolve(sample) if matcher else NO_MATCH
            if got.exams:
                hits += 1
        elif language == "ja":
            matcher = matchers.get("ja")
            got = matcher.resolve(sample) if matcher else NO_MATCH
            if got.levels:
                hits += 1
    hit_rate = hits / len(samples)

    score = hit_rate * 60.0
    lowered = (name or "").lower()
    if any(hint in lowered for hint in _GOOD_NAME_HINTS):
        score += 20.0
    if any(hint in lowered for hint in _BAD_NAME_HINTS):
        score -= 25.0
    elif not is_word_field_name(name):
        # 靠全等匹配才认出来的辅助字段（id / pos / type 之类），照样扣分
        score -= 25.0

    tokens = [t for t in words if t]
    if tokens:
        avg_len = sum(len(t) for t in tokens) / len(tokens)
        if avg_len <= 14:
            score += 10.0
        else:
            score -= 20.0
        avg_tokens = sum(len(split_english_words(s)) for s in samples) / len(samples)
        if language == "en" and avg_tokens <= 2:
            score += 10.0
        elif language == "en" and avg_tokens > 4:
            score -= 25.0
    else:
        score -= 30.0
    if sentence:
        # 例句/长文本：命中率可能很高（句子里当然有应试词），但整列不是取词字段
        score -= 30.0

    if language == "other":
        score -= 40.0

    return {
        "score": round(score, 2),
        "language": language,
        "hit_rate": round(hit_rate, 4),
        "hits": hits,
        "samples": len(samples),
        "looks_like_sentence": sentence,
        "words": tokens[:20],
    }


def recommend_fields(fields: dict, notes_by_type: dict, matchers: dict, sample: int = 60) -> list[dict]:
    """给每个笔记类型的每个字段打分，返回按分数从高到低排好的推荐表。

    fields: {notetype_id: [字段名, …]}
    notes_by_type: {notetype_id: [[字段值, …], …]}（已按字段顺序展开）
    """
    rows: list[dict] = []
    for ntid, names in fields.items():
        samples = notes_by_type.get(ntid, [])[:sample]
        for index, name in enumerate(names):
            values = [row[index] for row in samples if index < len(row)]
            info = score_field(name, values, matchers)
            rows.append({"notetype_id": ntid, "field": name, "index": index, **info})
    rows.sort(key=lambda r: (-r["score"], r["notetype_id"], r["index"]))
    return rows
