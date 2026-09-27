"""应试词汇统计 · 补漏牌组自动清理（纯逻辑，不 import Anki）。

「一键生成补漏牌组」当时做的是：把**当时那个统计范围里未覆盖**的词做成卡片。
问题是这些词以后会被别的牌库覆盖——覆盖了还留在补漏牌组里，就成了重复内容。

这个模块负责两件事：

1. **登记**：记下每个补漏牌组是「用哪个范围」建出来的（``builder_decks.json``）。
   清理时必须按同一个范围重新算覆盖，不能换个范围乱删。
2. **判定**：给定「候选卡片」和「范围里已被词表认出来的写法」，决定哪些能删。

删除规则（已和用户确认）：

- 只有**全部卡片都还没进入学习**（Anki 的 ``type == 0``，含暂停/埋葬的新卡）
  的笔记才可删；只要有一张卡进过学习/复习（``type != 0``），整条笔记永久保留。
- 「已覆盖」＝被**任何一张具体词表**认出来（四级/六级/考研/雅思/托福/GRE、
  N5–N1 任一），不要求同一张表；合并码（四六级并集、JLPT 并集）只用于显示，
  不参与判定；跨语言不互相顶替。
- 删的是**笔记**（卡片随之消失），调用方把它们包成一个撤销步骤，Ctrl+Z 可撤。

文件读写也放在这里（只读写 JSON），所以这一层能脱离 Anki 直接跑测试。
"""

from __future__ import annotations

import json
import os
import time

try:  # Anki 里是包内相对导入；单独跑测试时是平铺导入
    from . import analysis as A
    from . import vocab_logic as V
except ImportError:  # pragma: no cover
    import analysis as A
    import vocab_logic as V


REGISTRY_VERSION = 1
LOG_VERSION = 1

# 用户配置目录里的两个文件名（不往 addons21 里写东西）
REGISTRY_NAME = "builder_decks.json"
LOG_NAME = "cleanup_log.json"

# 清理日志最多留多少条
LOG_KEEP = 20

# 保留原因（只进日志和探针，界面不显示中间态）
KEEP_STARTED = "已开始学习"
KEEP_NO_CARD = "没有卡片"
KEEP_NO_WORD = "没有词"
KEEP_UNCOVERED = "仍未覆盖"


# ---------------------------------------------------------------- 登记表


def empty_registry() -> dict:
    return {"version": REGISTRY_VERSION, "decks": {}}


def scope_snapshot(scope: dict | None) -> dict:
    """把一份统计范围压成可持久化的快照；「什么都没限」时返回 ``{}``。

    只留「真的把范围限小了」的字段：默认值（含子牌组、四个状态全要、搜索式为空）
    一律不写，快照为空就等于「全集合」。
    """
    scope = scope or {}
    out: dict = {}
    deck_ids = sorted(A.as_int_list(scope.get("deck_ids")))
    if deck_ids:
        out["deck_ids"] = deck_ids
    if scope.get("include_subdecks") is False:
        out["include_subdecks"] = False
    for key in ("tags_include", "tags_exclude"):
        values = [str(t).strip() for t in (scope.get(key) or ()) if str(t).strip()]
        if values:
            out[key] = values
    if scope.get("tag_mode") == "all":
        out["tag_mode"] = "all"
    notetype_ids = sorted(A.as_int_list(scope.get("notetype_ids")))
    if notetype_ids:
        out["notetype_ids"] = notetype_ids
    states = [str(s) for s in (scope.get("states") or ()) if s]
    if states and set(states) != set(V.STATE_ORDER):
        out["states"] = sorted(set(states))
    search = str(scope.get("search") or "").strip()
    if search:
        out["search"] = search
    return out


def scope_is_empty(snapshot: dict | None) -> bool:
    """快照是不是「等于不限」——这种快照按全集合算。"""
    return not scope_snapshot(snapshot)


def scope_from_snapshot(snapshot: dict | None, fallback: dict | None = None) -> dict:
    """快照 -> 完整范围；快照为空（或缺失）时用 ``fallback``（默认全集合）。"""
    base = {
        "deck_ids": [],
        "include_subdecks": True,
        "tags_include": [],
        "tags_exclude": [],
        "tag_mode": "any",
        "notetype_ids": [],
        "states": list(V.STATE_ORDER),
        "search": "",
    }
    if fallback:
        for key, value in fallback.items():
            base[key] = value
    snapshot = scope_snapshot(snapshot)
    if scope_is_empty(snapshot):
        return base
    return {
        "deck_ids": list(snapshot.get("deck_ids") or []),
        "include_subdecks": bool(snapshot.get("include_subdecks", True)),
        "tags_include": list(snapshot.get("tags_include") or []),
        "tags_exclude": list(snapshot.get("tags_exclude") or []),
        "tag_mode": str(snapshot.get("tag_mode") or "any"),
        "notetype_ids": list(snapshot.get("notetype_ids") or []),
        "states": list(snapshot.get("states") or V.STATE_ORDER),
        "search": str(snapshot.get("search") or ""),
    }


def register_deck(
    registry: dict | None,
    deck_name: str,
    *,
    code: str = "",
    language: str = "en",
    label: str = "",
    scope: dict | None = None,
    words=(),
    count: int = 0,
    now: float | None = None,
) -> dict:
    """把「这次建了哪些牌组、用的什么范围」记进登记表（返回新表，不改原对象）。

    同一个牌组被第二次生成时：**保留第一次那份范围**（那才是这个牌组里旧卡的
    判定口径），新范围另存到 ``scope_latest`` 备查。
    """
    registry = dict(registry or empty_registry())
    decks = dict(registry.get("decks") or {})
    if not deck_name:
        registry["decks"] = decks
        return registry
    stamp = float(now if now is not None else time.time())
    entry = dict(decks.get(deck_name) or {})
    entry["code"] = code or entry.get("code") or ""
    entry["language"] = language or entry.get("language") or "en"
    entry["label"] = label or entry.get("label") or ""
    snapshot = scope_snapshot(scope) if scope is not None else {}
    if snapshot:
        if not entry.get("scope"):
            entry["scope"] = snapshot
        entry["scope_latest"] = snapshot
    else:
        entry.setdefault("scope", {})
    entry.setdefault("created_at", stamp)
    entry["updated_at"] = stamp
    entry["created_notes"] = int(entry.get("created_notes") or 0) + int(count or 0)
    fresh = [str(w) for w in (words or ()) if str(w)]
    if fresh:
        known = list(entry.get("words") or [])
        entry["words"] = known + [w for w in fresh if w not in known]
    decks[deck_name] = entry
    registry["version"] = REGISTRY_VERSION
    registry["decks"] = decks
    return registry


def prune_registry(registry: dict | None, alive_deck_names) -> tuple[dict, list]:
    """牌组被用户删掉后，把登记表里对应那条摘掉；返回 ``(新表, 被摘掉的名字)``。"""
    registry = dict(registry or empty_registry())
    alive = {str(name) for name in (alive_deck_names or ())}
    decks = dict(registry.get("decks") or {})
    dropped = [name for name in decks if name not in alive]
    for name in dropped:
        decks.pop(name, None)
    registry["decks"] = decks
    registry["version"] = REGISTRY_VERSION
    return registry, sorted(dropped)


def registered_decks(registry: dict | None) -> dict:
    """登记表里的 {牌组名: 记录}（坏数据一律跳过，不让它把清理搞崩）。"""
    out: dict = {}
    for name, entry in ((registry or {}).get("decks") or {}).items():
        if isinstance(name, str) and isinstance(entry, dict):
            out[name] = entry
    return out


# ---------------------------------------------------------------- 覆盖判定


def covered_keys_all(summary: dict) -> dict:
    """范围里已经算到**任何一张具体词表**上的词元，按语言分开。"""
    out: dict = {"en": set(), "ja": set()}
    for language, codes in (("en", V.ENGLISH_EXAM_CODES), ("ja", V.JLPT_CODES)):
        for code in codes:
            out[language] |= A.covered_keys_for(summary, language, code)
    return out


def covered_forms_all(summary: dict) -> dict:
    """范围里已覆盖的词，摊成「所有会被试写的写法」，按语言分开。"""
    out: dict = {"en": set(), "ja": set()}
    for language, codes in (("en", V.ENGLISH_EXAM_CODES), ("ja", V.JLPT_CODES)):
        for code in codes:
            out[language] |= A.covered_form_set(summary, language, code)
    return out


def coverage_from_summary(summary: dict) -> dict:
    """清理判定要用的覆盖索引：``{keys: {...}, forms: {...}}``。"""
    return {"keys": covered_keys_all(summary), "forms": covered_forms_all(summary)}


def empty_coverage() -> dict:
    return {"keys": {"en": set(), "ja": set()}, "forms": {"en": set(), "ja": set()}}


def make_resolver(matchers: dict | None):
    """把词库匹配器包成 ``resolver(语言, 词, 读音) -> (词元, 别名)``。

    清理判定只想知道「这个词还原成哪个词元」，不需要别的。取不到匹配器就返回
    ``None``（那时只按写法比对，宁可少删）。
    """

    def resolve(language: str, word: str, reading: str = ""):
        matcher = (matchers or {}).get(language)
        if matcher is None:
            return "", ()
        try:
            if language == "ja":
                match = matcher.resolve(word, reading)
            else:
                match = matcher.resolve(word)
        except Exception:  # noqa: BLE001  匹配本身出错就当没认出，绝不因此删卡
            return "", ()
        return getattr(match, "key", "") or "", tuple(getattr(match, "alternatives", ()) or ())

    return resolve


def is_covered(
    language: str,
    word: str,
    reading: str = "",
    covered: dict | None = None,
    resolver=None,
) -> bool:
    """这个词现在算不算「已覆盖」。"""
    if not word:
        return False
    covered = covered or empty_coverage()
    forms = A.surface_forms(language, word)
    if forms & ((covered.get("forms") or {}).get(language) or set()):
        return True
    keys = (covered.get("keys") or {}).get(language) or set()
    if not keys:
        return False
    if resolver is None:
        return False
    key, alternatives = resolver(language, word, reading)
    if key and key in keys:
        return True
    return any(alt in keys for alt in alternatives)


# ---------------------------------------------------------------- 判定


def note_deletable(card_types) -> bool:
    """全部卡片都还没进入学习（``type == 0``）才可删。

    暂停/埋葬的**新卡** type 也是 0，按「未学习」处理，可以删；只要有一张卡
    进过学习/复习（type 1/2/3），整条笔记永久保留。
    """
    types = []
    for value in card_types or ():
        number = A.as_int(value)
        if number is not None:
            types.append(number)
    if not types:
        return False
    return all(item == 0 for item in types)


def plan_cleanup(candidates, covered: dict | None = None, resolver=None) -> dict:
    """判定一批补漏卡：哪些删、哪些留、为什么。

    ``candidates`` 每条 = ``{"nid", "word", "reading", "language", "deck",
    "card_types": [...]}``（``card_types`` 是这张笔记全部卡片的 Anki ``type``）。

    返回 ``{"remove": [nid…], "keep": [{nid, word, why}…], "removed_words": […],
    "counts": {…}}``。这里只做判定，不碰集合。
    """
    covered = covered or empty_coverage()
    remove: list = []
    removed_words: list = []
    keep: list = []
    counts = {"已开始学习": 0, "没有卡片": 0, "没有词": 0, "仍未覆盖": 0}
    decks: set = set()

    for item in candidates or ():
        nid = A.as_int(item.get("nid"))
        if nid is None:
            continue
        word = str(item.get("word") or "").strip()
        language = str(item.get("language") or "en")
        if not note_deletable(item.get("card_types")):
            why = KEEP_STARTED if (item.get("card_types") or ()) else KEEP_NO_CARD
            keep.append({"nid": nid, "word": word, "why": why})
            counts[why] += 1
            continue
        if not word:
            keep.append({"nid": nid, "word": "", "why": KEEP_NO_WORD})
            counts[KEEP_NO_WORD] += 1
            continue
        if is_covered(language, word, str(item.get("reading") or ""), covered, resolver):
            remove.append(nid)
            removed_words.append(word)
            if item.get("deck"):
                decks.add(str(item["deck"]))
            continue
        keep.append({"nid": nid, "word": word, "why": KEEP_UNCOVERED})
        counts[KEEP_UNCOVERED] += 1

    return {
        "remove": remove,
        "keep": keep,
        "removed_words": removed_words,
        "decks": sorted(decks),
        "counts": counts,
        "candidates": len(remove) + len(keep),
    }


# ---------------------------------------------------------------- 日志


def format_time(stamp) -> str:
    try:
        value = float(stamp)
    except (TypeError, ValueError):
        return "（未知时间）"
    if value <= 0:
        return "（未知时间）"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))


def append_log(log: dict | None, entry: dict, keep: int = LOG_KEEP) -> dict:
    """把一次清理写进日志（最新的在最前面），最多留 ``keep`` 条。"""
    log = dict(log or {})
    if not isinstance(log.get("runs"), list):
        log["runs"] = []
    log["version"] = LOG_VERSION
    log["last"] = dict(entry)
    log["runs"] = [dict(entry)] + [r for r in log["runs"] if isinstance(r, dict)][: max(keep - 1, 0)]
    return log


def cleanup_status_text(log: dict | None) -> str:
    """设置页那行状态：上次自动清理干了什么。"""
    entry = (log or {}).get("last")
    if not isinstance(entry, dict):
        return "自动清理：还没跑过（打开 Anki 后会自动检查一次）。"
    when = format_time(entry.get("at"))
    if entry.get("error"):
        return f"上次自动清理：{when}，出错没删东西（{entry['error']}）。"
    candidates = int(entry.get("candidates") or 0)
    removed = int(entry.get("removed") or 0)
    kept = int(entry.get("kept") or 0)
    if not candidates:
        return f"上次自动清理：{when}，补漏牌组里没有卡片。"
    if not removed:
        return f"上次自动清理：{when}，没有需要清理的卡片（检查了 {candidates} 张）。"
    return (
        f"上次自动清理：{when}，删掉 {removed} 张已被覆盖的补漏卡"
        f"（检查 {candidates} 张，保留 {kept} 张）。"
    )


# ---------------------------------------------------------------- 文件读写


def registry_path(user_dir: str) -> str:
    return os.path.join(user_dir, REGISTRY_NAME)


def log_path(user_dir: str) -> str:
    return os.path.join(user_dir, LOG_NAME)


def read_json(path: str) -> dict:
    """读一个 JSON 文件；读不到或坏了都返回空 dict（清理程序不该因此崩）。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: str, payload: dict) -> bool:
    """原子写一个 JSON 文件（先写临时文件再替换），失败返回 False。"""
    folder = os.path.dirname(path) or "."
    try:
        os.makedirs(folder, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        return False
    return True
