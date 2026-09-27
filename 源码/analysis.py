"""应试词汇统计插件 · 查询与统计层。

分三块：

1. ``collect_entries``：按范围（牌组 / 标签 / 笔记类型 / 状态 / 浏览器搜索式）
   把笔记读出来、按配置的字段取词、交给 vocab_logic 匹配。
2. ``summarize``：**纯函数**，只吃 collect_entries 的输出，算数量、占比、覆盖率、
   来源归因、未覆盖清单。不碰 Anki，方便单测。
3. 缓存：落在用户配置目录里，按集合修改时间 + 笔记最大修改时间 + 配置 + 词库版本失效。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import defaultdict

try:  # 在 Anki 里是包的相对导入；单独跑测试时是平铺导入
    from . import vocab_logic as V
except ImportError:  # pragma: no cover
    import vocab_logic as V

# ---------------------------------------------------------------- 卡片状态

# 缺口原因。明细页与未覆盖清单共用这套说法：
#   真未覆盖 —— 词表里根本没有这个词（要补的就是这一类）
#   识别失败 —— 卡片里有这个写法，但我们的匹配没把它算进这张词表
#   待确认   —— 匹配出来是歧义（同形异义 / 假名对应多个词条），不猜
GAP_MISSING = "真未覆盖"
GAP_UNRECOGNIZED = "识别失败"
GAP_PENDING = "待确认"

# 范围里同一个写法可能出现多次，取「信息量更大」的那个状态
_RANGE_STATUS_RANK = {"": 0, "covered": 1, "unmatched": 2, "pending": 3}


def card_state(queue: int, card_type: int) -> str:
    """按 Anki 的 queue / type 判定学习状态。"""
    if queue in (-1, -2, -3):
        return "suspended"
    if card_type == 2:
        return "review"
    if card_type in (1, 3):
        return "learn"
    return "new"


def _better_state(current: str, candidate: str) -> str:
    """同一词多张卡时取进度更高的那个状态。"""
    order = V.STATE_ORDER
    if not current:
        return candidate
    return candidate if order.index(candidate) < order.index(current) else current


# ---------------------------------------------------------------- 范围查询


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def as_int(value):
    """尽量把 Anki 回传的牌组/笔记类型 ID 变成 int；变不了就返回 None。

    Anki 26.9 的 ``col.decks.children(did)`` 返回的是 ``[(名字, id), …]``，
    而同一个集合的 ``deck_and_child_ids(did)`` 返回的是 ``[id, …]``。
    旧版本里 children() 只返回 id。这里统一取「元组的第二项」再转 int，
    混进 int 列表里就再也不会出现 ``int < tuple`` 那种比较崩溃。
    """
    if isinstance(value, (tuple, list)):
        if len(value) == 1:
            # 有些接口会回传 [(名字, id)] 这种「外面又包了一层」的结构，
            # 递归拆到里面那一项，免得整份列表都判不出 ID 来。
            return as_int(value[0])
        if len(value) != 2:
            return None
        value = value[1]
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_int_list(values) -> list[int]:
    out: list[int] = []
    for item in values or ():
        number = as_int(item)
        if number is not None and number not in out:
            out.append(number)
    return out


def deck_and_child_ids(col, did) -> list[int]:
    """某个牌组 + 全部子牌组的 int ID 列表（顺序：先父后子）。

    优先用 ``deck_and_child_ids``：它是 Anki 官方接口，返回值就是 int 列表。
    老版本没有这个接口时才退回 ``children()``，并且对 ``(名字, id)`` 兼容。
    """
    root = as_int(did)
    if root is None:
        return []
    expanded: list[int] = []
    try:
        got = col.decks.deck_and_child_ids(root)
        expanded = as_int_list(got)
    except Exception:
        expanded = []
    if not expanded:
        expanded = [root]
        try:
            children = col.decks.children(root)
        except Exception:
            children = ()
        for child in as_int_list(children):
            if child not in expanded:
                expanded.append(child)
    if root not in expanded:
        expanded.insert(0, root)
    return expanded


def nodeck_scope(scope: dict, col) -> tuple[str, list]:
    """把范围条件翻成 SQL 的 where 片段（只针对 notes/cards 两表）。"""
    where: list[str] = []
    params: list = []

    deck_ids = as_int_list(scope.get("deck_ids"))
    if deck_ids:
        if scope.get("include_subdecks", True):
            expanded: list[int] = []
            for did in deck_ids:
                for child in deck_and_child_ids(col, did):
                    if child not in expanded:
                        expanded.append(child)
            deck_ids = sorted(set(expanded))
        marks = ",".join("?" * len(deck_ids))
        where.append(f"c.did in ({marks})")
        params.extend(deck_ids)

    notetype_ids = as_int_list(scope.get("notetype_ids"))
    if notetype_ids:
        marks = ",".join("?" * len(notetype_ids))
        where.append(f"n.mid in ({marks})")
        params.extend(notetype_ids)

    include = [t for t in (scope.get("tags_include") or ()) if t]
    exclude = [t for t in (scope.get("tags_exclude") or ()) if t]
    if include:
        clauses = []
        joiner = " and " if scope.get("tag_mode", "any") == "all" else " or "
        for tag in include:
            safe = _like_escape(tag)
            clauses.append("(n.tags like ? escape '\\' or n.tags like ? escape '\\')")
            params.extend([f"% {safe} %", f"% {safe}::%"])
        where.append("(" + joiner.join(clauses) + ")")
    for tag in exclude:
        safe = _like_escape(tag)
        where.append("not (n.tags like ? escape '\\' or n.tags like ? escape '\\')")
        params.extend([f"% {safe} %", f"% {safe}::%"])

    return (" and ".join(where), params)


def collect_entries(col, scope: dict, field_map: dict, matchers: dict) -> dict:
    """把范围内的笔记读出来并匹配词表。

    返回 {"entries": [...], "scanned_notes": n, "scanned_cards": n, "skipped": {...}}
    """
    where, params = nodeck_scope(scope, col)
    sql = (
        "select n.id, n.mid, n.flds, n.tags, c.id, c.did, c.type, c.queue "
        "from notes n join cards c on c.nid = n.id"
    )
    if where:
        sql += " where " + where
    rows = col.db.all(sql, *params)

    search = (scope.get("search") or "").strip()
    allowed_notes = None
    if search:
        try:
            allowed_notes = set(col.find_notes(search))
        except Exception:
            allowed_notes = None

    # 笔记类型字段名 -> 下标
    field_index: dict[int, dict[str, int]] = {}
    for ntid, cfg in (field_map or {}).items():
        model = col.models.get(ntid)
        if not model:
            continue
        index = {}
        for pos, fld in enumerate(model.get("flds", [])):
            index[fld["name"]] = pos
        field_index[ntid] = index

    notes: dict[int, dict] = {}
    seen_cards = 0
    state_filter = set(scope.get("states") or ()) or {"new", "learn", "review", "suspended"}

    for nid, mid, flds, tags, cid, did, ctype, queue in rows:
        if allowed_notes is not None and nid not in allowed_notes:
            continue
        seen_cards += 1
        deck_id = as_int(did)
        note = notes.get(nid)
        if note is None:
            note = {
                "note_id": nid,
                "notetype_id": mid,
                "fields": flds.split("\x1f"),
                "tags": [t for t in (tags or "").split(" ") if t],
                "deck_ids": set(),
                "cards": [],
            }
            notes[nid] = note
        if deck_id is not None:
            note["deck_ids"].add(deck_id)
        note["cards"].append((cid, did, card_state(queue, ctype)))

    entries: list[dict] = []
    skipped = defaultdict(int)
    for note in notes.values():
        states = {state for _cid, _did, state in note["cards"]}
        state = ""
        for item in states:
            state = _better_state(state, item)
        if state not in state_filter:
            continue

        cfg = (field_map or {}).get(note["notetype_id"])
        if not cfg:
            skipped["没有配置取词字段"] += 1
            continue
        surface = ""
        reading = ""
        index = field_index.get(note["notetype_id"], {})
        for name in cfg.get("fields", []):
            pos = index.get(name)
            if pos is None or pos >= len(note["fields"]):
                continue
            value = V.clean_field(note["fields"][pos])
            if value:
                surface = value
                break
        reading_field = cfg.get("reading_field")
        if reading_field:
            pos = index.get(reading_field)
            if pos is not None and pos < len(note["fields"]):
                reading = V.clean_field(note["fields"][pos])

        if not surface:
            skipped["取词字段是空的"] += 1
            continue

        language = cfg.get("language") or "en"
        if cfg.get("allow_sentence") and language == "en":
            words = V.split_english_words(surface) or [surface]
        else:
            words = [surface]

        # 一张笔记的多张卡：状态统计只跟笔记有关，挪到循环外算一次就够
        card_states: dict[str, int] = defaultdict(int)
        for _cid, _did, card_status in note["cards"]:
            card_states[card_status] += 1
        deck_ids_sorted = sorted(int(item) for item in note["deck_ids"])
        primary_deck = deck_ids_sorted[0] if deck_ids_sorted else -1

        for word in words:
            if language == "ja":
                match = matchers["ja"].resolve(word, reading) if matchers.get("ja") else V.NO_MATCH
            else:
                match = matchers["en"].resolve(word) if matchers.get("en") else V.NO_MATCH
            entries.append(
                {
                    "note_id": note["note_id"],
                    "notetype_id": note["notetype_id"],
                    "deck_ids": deck_ids_sorted,
                    "deck_id": primary_deck,
                    "tags": note["tags"],
                    "state": state,
                    "card_count": len(note["cards"]),
                    "card_states": dict(card_states),
                    "language": language,
                    "surface": surface,
                    "word": word,
                    "key": match.key or V.normalize_english(word),
                    "exams": sorted(match.exams),
                    "levels": sorted(match.levels),
                    "confident": match.confident,
                    "matched_by": match.matched_by,
                    "alternatives": list(match.alternatives),
                    "meaning": match.meaning,
                }
            )

    return {
        "entries": entries,
        "scanned_notes": len(notes),
        "scanned_cards": seen_cards,
        "skipped": dict(skipped),
    }


# ---------------------------------------------------------------- 汇总


def _pct(part: int, whole: int) -> float:
    return round(part * 100.0 / whole, 1) if whole else 0.0


def _merged_total(matcher, language: str, members) -> int:
    """「并集」那一行的分母：优先问 matcher 自己按词元组去重算，拿不到再退化。

    不能写成「各成员相加」——同一个词同时属于四级和六级时会被算两遍，
    四六级并集的分母就从 5731 虚高成 9155，覆盖率跟着被压低。
    """
    fn = getattr(matcher, "merged_total", None)
    if callable(fn):
        got = int(fn(members) or 0)
        if got:
            return got
    counts = getattr(matcher, "exam_counts" if language == "en" else "level_counts", {}) or {}
    return sum(int(counts.get(code, 0)) for code in members)


def _deck_name(col, did: int) -> str:
    try:
        return col.decks.name(did)
    except Exception:
        return str(did)


def _entry_notetypes(entry: dict) -> list:
    """词条自带的笔记类型 ID 列表；老数据只有单数字段时退回那一个。"""
    ids = entry.get("notetype_ids")
    if ids:
        return list(ids)
    single = entry.get("notetype_id")
    return [single] if single is not None else []


def _merge_words(entries: list[dict]) -> list[dict]:
    """按 (语言, 词元) 去重，合并状态、标签与来源。"""
    merged: dict[tuple, dict] = {}
    for entry in entries:
        key = (entry["language"], entry["key"])
        row = merged.get(key)
        if row is None:
            row = {
                "language": entry["language"],
                "key": entry["key"],
                "surface": entry["surface"],
                "surfaces": {entry["surface"]},
                "state": entry["state"],
                "states": {entry["state"]},
                "exams": set(entry["exams"]),
                "levels": set(entry["levels"]),
                "confident": entry["confident"],
                "alternatives": set(entry["alternatives"]),
                "meaning": entry["meaning"],
                "deck_ids": set(entry["deck_ids"]),
                # 一条词条理论上只来自一个笔记类型，但同词的其它卡片可能来自别的
                # 笔记类型；这里统一当列表处理，_merge_words 会把它们并起来。
                "notetype_ids": {int(item) for item in _entry_notetypes(entry)},
                "tags": set(entry["tags"]),
                "note_ids": {entry["note_id"]},
                "notes": 1,
                "cards": entry["card_count"],
            }
            merged[key] = row
            continue
        row["surfaces"].add(entry["surface"])
        row["states"].add(entry["state"])
        row["state"] = _better_state(row["state"], entry["state"])
        row["exams"] |= set(entry["exams"])
        row["levels"] |= set(entry["levels"])
        row["confident"] = row["confident"] and entry["confident"]
        row["alternatives"] |= set(entry["alternatives"])
        row["deck_ids"] |= set(entry["deck_ids"])
        row["notetype_ids"] |= {int(item) for item in _entry_notetypes(entry)}
        row["tags"] |= set(entry["tags"])
        if entry["note_id"] not in row["note_ids"]:
            row["note_ids"].add(entry["note_id"])
            row["notes"] += 1
            row["cards"] += entry["card_count"]
        if not row["meaning"] and entry["meaning"]:
            row["meaning"] = entry["meaning"]
    return list(merged.values())


def collect_words(col, scope: dict, field_map: dict, matchers: dict) -> list[dict]:
    """按范围取词并去重（自定义词表「拿某个牌组/标签当分母」要用它）。

    返回的就是 ``summarize`` 里那份词条结构，只是不带主来源、也不带排序。
    """
    collected = collect_entries(col, scope, field_map, matchers)
    return _merge_words(collected["entries"])

def _primary_source(col, word: dict, deck_names: dict, deck_depth: dict, deck_size: dict) -> str:
    """主来源：最具体的子牌组 -> 词数最多的牌组 -> 牌组 ID 最小的。"""
    best = None
    for raw in word["deck_ids"]:
        did = as_int(raw)
        if did is None:
            continue
        score = (
            -deck_depth.get(did, 0),
            -deck_size.get(did, 0),
            did,
        )
        if best is None or score < best[0]:
            best = (score, did)
    if best is None:
        return "（未知）"
    return deck_names.get(best[1], str(best[1]))


def source_rows(
    words: list[dict], dimension: str, notetype_names: dict | None = None
) -> list[dict]:
    """按来源维度聚合一批词条，返回 [{name, count, pct}]，占比一定加总 100%。

    - ``deck``：沿用老口径，一个词只算一次，归到 ``_primary_source`` 挑的最具体子牌组；
    - ``tag`` / ``notetype``：一个词有几个标签（或笔记类型）就在几行各算一次，
      分母是出现次数合计。没标签的归到「（无标签）」一行，保证占比凑得满 100%；
      插件自己打的「应试::…」标签排除掉，否则打完标签来源图就只剩自己了。

    纯函数：只吃 summary 里已经算好的词条行，方便单测和探针核对。
    """
    counts: dict[str, int] = defaultdict(int)
    if dimension == "tag":
        for word in words:
            tags = [t for t in (word.get("tags") or ()) if not V.is_own_tag(t)]
            if tags:
                for tag in tags:
                    counts[tag] += 1
            else:
                counts["（无标签）"] += 1
    elif dimension == "notetype":
        names = notetype_names or {}
        for word in words:
            ids = [int(item) for item in (word.get("notetype_ids") or ())]
            if ids:
                for ntid in ids:
                    counts[names.get(ntid, str(ntid))] += 1
            else:
                counts["（未知笔记类型）"] += 1
    else:
        for word in words:
            counts[word.get("source") or "（未知）"] += 1
    total = sum(counts.values())
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"name": name, "count": count, "pct": _pct(count, total)} for name, count in ordered]


def notetype_name_map(col, words: list[dict]) -> dict:
    """词条里出现过的笔记类型 ID -> 名字（取不到就退回 ID 字符串）。"""
    out: dict[int, str] = {}
    models = getattr(col, "models", None)
    for word in words:
        for raw in word.get("notetype_ids") or ():
            ntid = int(raw)
            if ntid in out:
                continue
            name = str(ntid)
            if models is not None:
                try:
                    model = models.get(ntid)
                except Exception:
                    model = None
                if model:
                    name = model["name"]
            out[ntid] = name
    return out


def matched_codes(word: dict, custom_keys: dict | None = None) -> set:
    """一个词条命中的全部词表码。（v0.3 起词表全部内置，参数 custom_keys 保留兼容。）"""
    return set(word.get("exams") or ()) | set(word.get("levels") or ())


def filter_words_by_codes(
    words: list[dict], selected, mode: str = "any", custom_keys: dict | None = None
) -> list[dict]:
    """按明细页勾选的词表过滤词条。

    ``selected`` 是 [(语言, 词表码), …]；空选择 = 不过滤。
    ``mode="any"`` 命中任一勾选项即可（并集），``"all"`` 要全部命中（交集）。
    跨语言的勾选对另一个语言的词不生效——英文词只看英文那几行。
    合并码按它自己的含义展开：cet46 = 四级或六级，jlpt = 任一等级。
    """
    groups: dict[str, list[set]] = defaultdict(list)
    for item in selected or ():
        try:
            language, code = item
        except (TypeError, ValueError):
            continue
        if code == "cet46":
            members = {"cet4", "cet6"}
        elif code == "jlpt":
            members = set(V.JLPT_CODES)
        else:
            members = {code}
        if members not in groups[language]:
            groups[language].append(members)
    if not groups:
        return list(words)

    out: list[dict] = []
    for word in words:
        wanted = groups.get(word.get("language"))
        if not wanted:
            continue
        codes = matched_codes(word, custom_keys)
        hits = [bool(members & codes) for members in wanted]
        if all(hits) if mode == "all" else any(hits):
            out.append(word)
    return out


def summarize(
    col, collected: dict, config: dict, matchers: dict, custom_lists: list | None = None
) -> dict:
    """算数量、占比、覆盖率、来源归因、未覆盖清单。

    ``custom_lists`` 是 v0.2 的自定义词表入口。v0.3 起词表全部内置，这个参数
    只是为了不破坏旧调用而保留，传进来也会被忽略。
    """
    entries = collected["entries"]
    custom_lists: list = []
    deck_names: dict[int, str] = {}
    for entry in entries:
        for did in entry["deck_ids"]:
            # 注意别写成 setdefault(did, _deck_name(col, did))：那样每次循环都会
            # 真去问一次 Anki 后端（取牌组名是跨进程调用），2 万条笔记白等 1 秒。
            if did not in deck_names:
                deck_names[did] = _deck_name(col, did)
    deck_depth = {did: name.count("::") for did, name in deck_names.items()}
    deck_size: dict[int, int] = defaultdict(int)
    for entry in entries:
        deck_size[entry["deck_id"]] += 1

    words = _merge_words(entries)
    for word in words:
        word["source"] = _primary_source(col, word, deck_names, deck_depth, deck_size)
        word["deck_names"] = sorted(deck_names.get(d, str(d)) for d in word["deck_ids"])
        # 词条行里攒的是 set（同一个词可能来自多张卡）。这里统一转成排好序的
        # 列表：一是给界面用，二是让整份 summary 能 json 序列化——
        # 缓存的 save_cache 是 json.dump，留着 set 会让它静默失败、缓存永远不生效。
        for key in ("surfaces", "states", "exams", "levels", "alternatives", "tags"):
            value = word.get(key)
            if isinstance(value, (set, frozenset)):
                word[key] = sorted(value)
        for key in ("deck_ids", "notetype_ids", "note_ids"):
            value = word.get(key)
            if isinstance(value, (set, frozenset)):
                word[key] = sorted(int(item) for item in value)

    # 「统计单位」下拉用的三份口径：唯一词 / 笔记 / 卡片。
    # 一条笔记可能产出多个词条（打开「例句分词」时才会），所以按笔记去重后再数。
    entries_by_note: dict[int, dict] = {}
    for entry in entries:
        entries_by_note.setdefault(entry["note_id"], entry)

    # 来源维度：牌组 / 标签 / 笔记类型。认不出来的值一律退回牌组，别让界面炸。
    dimension = str(config.get("source_dimension") or "deck")
    if dimension not in ("deck", "tag", "notetype"):
        dimension = "deck"
    notetype_names = notetype_name_map(col, words) if dimension == "notetype" else {}

    enabled_exams = [c for c in config.get("enabled_exams", list(V.ENGLISH_EXAM_CODES))]
    enabled_levels = [c for c in config.get("enabled_levels", list(V.JLPT_CODES))]
    en_meta = matchers["en"].exam_counts if matchers.get("en") else {}
    ja_meta = matchers["ja"].level_counts if matchers.get("ja") else {}

    summary = {
        "scanned_notes": collected["scanned_notes"],
        "scanned_cards": collected["scanned_cards"],
        "skipped": collected.get("skipped", {}),
        "source_dimension": dimension,
        "languages": {},
    }
    # 自定义词表要按「范围里有哪些词」算覆盖，所以这里先放一份未排序的词条，
    # 函数最后再换成排好序的那一份。
    summary["words"] = words

    for language in ("en", "ja"):
        subset = [w for w in words if w["language"] == language]
        if language == "en":
            codes = enabled_exams
            totals = en_meta
            labels = V.ENGLISH_EXAM_LABELS
            merged_defs = (("cet46", "四六级并集", ("cet4", "cet6")),)
        else:
            codes = enabled_levels
            totals = ja_meta
            labels = V.JLPT_LABELS
            merged_defs = (("jlpt", "JLPT 并集", tuple(V.JLPT_CODES)),)

        enabled = set(codes)
        matched = [
            w
            for w in subset
            if w["confident"] and (set(w["exams"]) | set(w["levels"])) & enabled
        ]
        words_with_code: dict[str, list[dict]] = {}
        for code in codes:
            field = "exams" if language == "en" else "levels"
            words_with_code[code] = [w for w in matched if code in w[field]]

        covered_keys = {w["key"] for w in matched}
        coverage = []
        for code in codes:
            total = int(totals.get(code, 0))
            covered = len(words_with_code[code])
            coverage.append(
                {
                    "code": code,
                    "label": labels.get(code, code),
                    "total": total,
                    "covered": covered,
                    "missing": max(total - covered, 0),
                    "rate": _pct(covered, total),
                    "share_of_range": _pct(covered, len(subset)),
                }
            )
        for code, label, members in merged_defs:
            field = "exams" if language == "en" else "levels"
            covered = len([w for w in matched if set(w[field]) & set(members)])
            total = _merged_total(matchers.get(language), language, members)
            coverage.append(
                {
                    "code": code,
                    "label": label,
                    "total": total,
                    "covered": covered,
                    "missing": max(total - covered, 0),
                    "rate": _pct(covered, total),
                    "share_of_range": _pct(covered, len(subset)),
                    "note": "并集；跨词表的词只算一次",
                }
            )

        # 来源归因：按「来源维度」聚合。decks 维度一个词只算一次，标签/笔记类型
        # 维度一个词按出现次数各算一次（multi 标签的词会在多行里都出现）。
        sources = {
            code: source_rows(words_with_code[code], dimension, notetype_names)
            for code in codes
        }

        subset_notes = [e for e in entries_by_note.values() if e["language"] == language]

        def _state_rows(counts: dict, whole: int) -> list:
            return [
                {
                    "code": code,
                    "label": V.STATE_LABELS[code],
                    "words": counts.get(code, 0),
                    "pct": _pct(counts.get(code, 0), whole),
                }
                for code in V.STATE_ORDER
            ]

        states: dict[str, int] = defaultdict(int)
        for word in subset:
            states[word["state"]] += 1

        note_states: dict[str, int] = defaultdict(int)
        card_states: dict[str, int] = defaultdict(int)
        for entry in subset_notes:
            note_states[entry["state"]] += 1
            per_card = entry.get("card_states") or {entry["state"]: entry["card_count"]}
            for code, count in per_card.items():
                card_states[code] += count

        cards_total = sum(e["card_count"] for e in subset_notes)
        notes_total = len(subset_notes)
        states_by_unit = {
            "word": _state_rows(states, len(subset)),
            "note": _state_rows(note_states, notes_total),
            "card": _state_rows(card_states, cards_total),
        }

        summary["languages"][language] = {
            "words": len(subset),
            "cards": cards_total,
            "notes": notes_total,
            "matched_words": len(matched),
            "unmatched_words": len(
                [
                    w
                    for w in subset
                    if w["confident"]
                    and not (set(w["exams"]) | set(w["levels"])) & enabled
                ]
            ),
            "pending_words": len([w for w in subset if not w["confident"]]),
            "states": states_by_unit["word"],
            "states_by_unit": states_by_unit,
            "unit_totals": {
                "word": len(subset),
                "note": notes_total,
                "card": cards_total,
            },
            "coverage": coverage,
            "sources": sources,
        }

    summary["words"] = sorted(
        words, key=lambda w: (w["language"], w["state"], -w["cards"], w["key"])
    )
    all_codes = sorted(
        {c for w in words for c in (set(w["exams"]) | set(w["levels"]))}
    )
    summary["all_codes"] = all_codes
    # v0.3 起词表全部内置，没有自定义词表了。这个键留成空列表，只是为了让
    # 旧缓存 / 旧界面代码读到它时不会崩。
    summary["custom"] = []
    enabled_all = set(enabled_exams) | set(enabled_levels)
    summary["totals"] = {
        "words": len(words),
        "notes": len(entries_by_note),
        "cards": sum(e["card_count"] for e in entries_by_note.values()),
        "matched_words": len(
            [
                w
                for w in words
                if w["confident"] and (set(w["exams"]) | set(w["levels"])) & enabled_all
            ]
        ),
    }
    return summary


_GAP_ORDER = {GAP_MISSING: 0, GAP_UNRECOGNIZED: 1, GAP_PENDING: 2}

def _lookup_forms(word: dict) -> list[str]:
    """一个词条在「范围索引」里会用到的全部写法。"""
    language = word.get("language") or "en"
    forms: list[str] = []

    def add(value: str) -> None:
        if value and value not in forms:
            forms.append(value)

    values = [word.get("key"), word.get("surface"), *(word.get("surfaces") or ())]
    for value in values:
        if not value:
            continue
        if language == "ja":
            add(V.normalize_japanese(value))
            add(V.kata_to_hira(V.normalize_japanese(value)))
        else:
            norm = V.normalize_english(value)
            for variant in V.spelling_variants(norm) if norm else ():
                add(variant)
    for alt in word.get("alternatives") or ():
        add(V.normalize_japanese(alt) if language == "ja" else V.normalize_english(alt))
    return forms


def range_gap_index(summary: dict) -> dict:
    """把统计范围里的词条摊成 {语言: {写法: 状态}}，用来判断一个缺口的原因。

    状态只有四种：``covered``（已命中某张词表）、``unmatched``（认出来了但没进
    任何词表）、``pending``（歧义待确认）、``""``（范围里没有这个写法）。
    """
    index: dict[str, dict[str, str]] = {"en": {}, "ja": {}}
    for word in summary.get("words") or ():
        language = word.get("language") or "en"
        if not word.get("confident"):
            status = "pending"
        elif word.get("exams") or word.get("levels"):
            status = "covered"
        else:
            status = "unmatched"
        bucket = index.setdefault(language, {})
        for form in _lookup_forms(word):
            current = bucket.get(form, "")
            if _RANGE_STATUS_RANK[status] > _RANGE_STATUS_RANK[current]:
                bucket[form] = status
    return index


def classify_gap(language: str, key: str, index: dict) -> str:
    """给一个「词表里有、范围里没覆盖」的词定原因。"""
    if language == "ja":
        forms = [
            V.normalize_japanese(key),
            V.kata_to_hira(V.normalize_japanese(key)),
        ]
    else:
        norm = V.normalize_english(key)
        forms = list(V.spelling_variants(norm)) if norm else [key]
    bucket = index.get(language) or {}
    best = ""
    for form in forms:
        status = bucket.get(form, "")
        if _RANGE_STATUS_RANK.get(status, 0) > _RANGE_STATUS_RANK.get(best, 0):
            best = status
    if best == "pending":
        # 卡片里有这个词，只是匹配出来是歧义：不猜，也不重复制卡
        return GAP_PENDING
    if best:
        return GAP_UNRECOGNIZED
    return GAP_MISSING


def entry_gap_reason(entry: dict, in_list: bool) -> str:
    """明细页里「没命中任何词表」的那一行到底是什么情况。

    ``in_list=True`` 表示这个词其实在词表里——那就是我们的匹配漏了它。
    返回值是**内部细分类**，界面只显示两态，见 :func:`display_reason`。
    """
    if not entry.get("confident"):
        return GAP_PENDING
    if entry.get("exams") or entry.get("levels"):
        return ""
    return GAP_UNRECOGNIZED if in_list else GAP_MISSING


# 界面上只给两态：识别失败、待确认、真没收录，对用户都是「未覆盖」。
# 细分留在这里，只服务两件事：不给「其实有卡但没认出来」的词重复制卡，以及测试断言。
STATE_COVERED = "已覆盖"
STATE_UNCOVERED = "未覆盖"


def display_reason(reason: str) -> str:
    """内部细分类 -> 界面显示的两态。"""
    return STATE_COVERED if not reason else STATE_UNCOVERED


def in_word_list(matchers: dict, language: str, key: str) -> bool:
    """这个词（去重后的词元）在对应词表里出现过吗。"""
    matcher = matchers.get(language)
    if matcher is None or not key:
        return False
    if language == "ja":
        return bool(matcher.by_word.get(key) or matcher.by_reading.get(key))
    return bool(matcher.group_exam.get(key) or matcher.word_exam.get(key))


def list_members(code: str, language: str) -> set:
    """一个词表码展开成它包含的原始码（合并码展开成成员）。"""
    if code == "cet46":
        return {"cet4", "cet6"}
    if code == "jlpt":
        return set(V.JLPT_CODES)
    return {code}


def covered_keys_for(summary: dict, language: str, code: str) -> set:
    """范围里已经算到这张词表上的词元。

    必须按「这张表自己的标签」算：老版本用「只要命中任何词表就算覆盖」，
    结果是「四级未覆盖」比覆盖页少报一批词，两处对不上。
    """
    field = "exams" if language == "en" else "levels"
    members = list_members(code, language)
    out = set()
    for word in summary.get("words") or ():
        if word.get("language") != language or not word.get("confident"):
            continue
        if set(word.get(field) or ()) & members:
            out.add(word["key"])
    return out


def surface_forms(language: str, value: str) -> set:
    """一个写法摊成「同一批写法」的集合（不含去变形，速度快）。

    词表清单里写的是表面词（``co-operative``），卡片里可能写成 ``cooperative``；
    这一步只做归一 + 英美拼写 + 连字符/空格三种互换，够用而且便宜——真正的
    词形还原交给匹配器（见 :func:`uncovered_words`）。
    """
    out: set = set()
    if not value:
        return out
    if language == "ja":
        norm = V.normalize_japanese(value)
        for form in (
            norm,
            V.kata_to_hira(norm) if norm else "",
        ):
            if form:
                out.add(form)
        return out
    norm = V.normalize_english(value)
    if norm:
        out.update(V.form_variants(norm))
    out.discard("")
    return out


def covered_form_set(summary: dict, language: str, code: str) -> set:
    """范围里「已算到这张词表上」的词，展开成所有会被试写的写法。

    判定一个词表词条是否其实已经被覆盖时用它：卡片写 ``analyse`` 会归到词元
    ``analyze``，两边写法不同，只比词元会漏。
    """
    members = list_members(code, language)
    field = "exams" if language == "en" else "levels"
    forms: set = set()
    for word in summary.get("words") or ():
        if word.get("language") != language or not word.get("confident"):
            continue
        if not (set(word.get(field) or ()) & members):
            continue
        forms.update(_lookup_forms(word))
        forms |= surface_forms(language, word.get("key") or "")
    forms.discard("")
    return forms


def _list_entries(matchers: dict, language: str, code: str):
    """一张内置词表里的 (词元, 释义) 全量清单。"""
    if language == "en":
        index = matchers.get("en")
        if index is None:
            return
        members = list_members(code, "en")
        for head, tags in index.group_exam.items():
            if set(tags.split()) & members:
                yield head, ""
    else:
        index = matchers.get("ja")
        if index is None:
            return
        members = list_members(code, "ja")
        for word, levels in index.by_word.items():
            if set(levels.split()) & members:
                yield word, index.meanings.get(word, "")


def uncovered_words(matchers: dict, summary: dict, language: str, code: str, limit: int = 5000):
    """某个词表里有、但当前范围里没覆盖的词，并给出「原因」。

    原因三选一（见文件头的 GAP_* 常量）：真未覆盖 / 识别失败 / 待确认。
    只有「真未覆盖」会被一键补漏制卡拿去用——另外两类是我们自己的匹配或词表
    本身的问题，硬造成卡片只会重复现有内容。
    """
    covered = covered_keys_for(summary, language, code)
    # 「其实已经有卡了、只是写法/词元不一样」的词要先排掉，否则会被误报成
    # 「识别失败」（user 的硬指标是识别失败 = 0，界面也只看两态）。
    covered_forms = covered_form_set(summary, language, code)
    matcher = matchers.get(language)
    index = range_gap_index(summary)
    out = []
    for key, meaning in _list_entries(matchers, language, code):
        if key in covered:
            continue
        if surface_forms(language, key) & covered_forms:
            continue
        if matcher is not None:
            # 词表表面词按匹配器的口径再解一次：children -> child、analyse -> analyze、
            # co-operative -> cooperative。能落到已覆盖词元上的，就不是缺口。
            got = matcher.resolve(key)
            if got.key in covered or (set(got.alternatives or ()) & covered):
                continue
        out.append(
            {
                "key": key,
                "meaning": meaning,
                "reason": classify_gap(language, key, index),
            }
        )
    out.sort(key=lambda r: (_GAP_ORDER.get(r["reason"], 9), r["key"]))
    return out[:limit]


def word_list_keys(matchers: dict, summary: dict, language: str, code: str) -> set:
    """一张词表的全部词元。"""
    return {key for key, _meaning in _list_entries(matchers, language, code)}


# ---------------------------------------------------------------- 字段识别


# 字段名里出现这些词，基本就是「给人看的读音」，不该当取词字段用；
# 但它正好可以当日语词的读音兜底。
READING_HINTS = ("furigana", "reading", "kana", "假名", "读音", "よみ", "yomi")

# 已在本机实测确认「取词字段是英文、但整张卡是日语学习牌库」的笔记类型。
DEFAULT_OFF_NOTETYPES = ("lapis",)


def looks_like_reading(name: str) -> bool:
    lowered = (name or "").lower()
    return any(hint in lowered for hint in READING_HINTS)


def sample_notes(col, notetype_ids, limit: int = 60) -> dict:
    """每个笔记类型抽 limit 条笔记，返回 {ntid: [[字段值, …], …]}。"""
    samples: dict[int, list] = defaultdict(list)
    for ntid in notetype_ids:
        model = col.models.get(ntid)
        if not model:
            continue
        try:
            nids = list(col.find_notes(f'note:"{model["name"]}"'))[:limit]
        except Exception:
            nids = []
        for nid in nids:
            note = col.get_note(nid)
            if note is not None:
                samples[ntid].append(list(note.fields))
    return dict(samples)


def detect_fields(col, matchers: dict, limit: int = 60) -> dict:
    """扫描每个笔记类型，推荐取词字段。

    纯逻辑，只要求传入的 col 提供 models.all()/models.get()/find_notes()/get_note()，
    所以既能跑在真 Anki 里，也能跑在「真实集合只读副本」那种轻量适配器上。
    """
    models = list(col.models.all())
    fields: dict[int, list] = {}
    for model in models:
        fields[int(model["id"])] = [f["name"] for f in model.get("flds", [])]
    samples = sample_notes(col, list(fields), limit)

    rows = V.recommend_fields(fields, samples, matchers, sample=limit)
    by_type: dict[int, list] = defaultdict(list)
    for row in rows:
        by_type[row["notetype_id"]].append(row)

    result = {}
    for ntid, names in fields.items():
        ranked = by_type.get(ntid) or []
        # 「假名字段」是给人读读音用的，不该当取词字段；但它正好可以当日语词的读音兜底
        reading_names = {r["field"] for r in ranked if looks_like_reading(r["field"])}
        # 再往下是按字段名一票否决：词性、音标、例句、释义、编号这些字段即便命中率
        # 100% 也不能当取词字段（ECDICT 里 noun / verb 自带 cet4 标签，SentType 里
        # 的「対」自带 JLPT 标签，只看分数拦不住）。误伤了可以在设置页手动指定。
        word_rows = [
            r
            for r in ranked
            if r["field"] not in reading_names and V.is_word_field_name(r["field"])
        ]
        reading_rows = [r for r in ranked if r["field"] in reading_names]
        best = word_rows[0] if word_rows else None
        # 没有合格字段时，仍然拿分数最高的一行喂给设置页显示说明和预览，
        # 但绝不自动勾选——否则「释义/例句」会被整列当成词表统计。
        shown = best or (ranked[0] if ranked else None)
        # 笔记类型整体像什么语言：看所有字段里有没有假名/汉字
        type_language = V.classify_language(
            [value for row in samples.get(ntid, []) for value in row]
        )
        language = shown["language"] if shown else "other"
        reason = ""

        # 取词字段：首选分最高的那个，再挑「确实也像单词字段」的当兜底（按分数排序）
        chosen: list[str] = []
        if best:
            chosen.append(best["field"])
            for row in word_rows[1:]:
                if row is best:
                    continue
                if (
                    row["language"] == language
                    and row["score"] >= max(40.0, best["score"] - 15.0)
                    and row["hit_rate"] >= 0.3
                ):
                    chosen.append(row["field"])

        reading_field = ""
        if language == "ja":
            for row in reading_rows:
                if row["field"] not in chosen:
                    reading_field = row["field"]
                    break

        enabled = bool(
            best
            and best["score"] >= 55
            and best["hit_rate"] >= 0.3
            and language in ("en", "ja")
        )
        model = col.models.get(ntid)
        lowered_name = (model["name"] if model else "").lower()
        if enabled and any(pattern in lowered_name for pattern in DEFAULT_OFF_NOTETYPES):
            enabled = False
            reason = "已实测为英文释义型牌库，默认不勾选（想统计就在设置页这一行打勾）"
        elif enabled and type_language == "ja" and language == "en":
            # 日语卡片里出现的英文单词通常是释义/翻译，不能算英文考试覆盖
            enabled = False
            reason = "疑似日语笔记类型里的英文释义，默认不勾选"
        elif enabled and type_language == "en" and language == "ja":
            enabled = False
            reason = "疑似英文笔记类型里的日文内容，默认不勾选"
        elif not best:
            reason = (
                "这个笔记类型里没有像「单词字段」的字段（候选都是释义、例句、音标、"
                "编号之类），默认不勾选；真要统计就在设置页自己指定字段"
            )
        elif not enabled:
            if shown.get("looks_like_sentence"):
                reason = reason or (
                    "这一列像例句/长文本，默认不勾选；真要统计就在设置页自己指定字段"
                )
            else:
                reason = reason or (
                    f"应试词命中率偏低（{best['hit_rate'] * 100:.0f}%），默认不勾选"
                )

        result[str(ntid)] = {
            "notetype_name": model["name"] if model else str(ntid),
            "fields": chosen,
            "fallback_fields": chosen[1:],
            "reading_field": reading_field,
            "language": language if language in ("en", "ja") else "none",
            "allow_sentence": False,
            "enabled": enabled,
            "score": shown["score"] if shown else 0,
            "hit_rate": shown["hit_rate"] if shown else 0,
            # 命中率的原始数字：设置页要写成「抽样 60 条，命中 25 条（42%）」
            "samples": (shown or {}).get("samples", 0),
            "hits": (shown or {}).get("hits", 0),
            "looks_like_sentence": bool((shown or {}).get("looks_like_sentence")),
            "reason": reason,
            "preview": (shown or {}).get("words", [])[:20],
            "status": "inferred",
        }
    return result


# ---------------------------------------------------------------- 缓存


def collection_signature(
    col, scope: dict, config: dict, index_meta: dict, custom_lists: list | None = None
) -> str:
    col_mod = int(col.db.scalar("select mod from col") or 0)
    note_mod = int(col.db.scalar("select max(mod) from notes") or 0)
    card_mod = int(col.db.scalar("select max(mod) from cards") or 0)
    raw = json.dumps(
        {
            "col": col_mod,
            "note": note_mod,
            "card": card_mod,
            "scope": _jsonable(scope),
            "field_map": _jsonable(config.get("field_map", {})),
            "exams": config.get("enabled_exams"),
            "levels": config.get("enabled_levels"),
            "merge_lemma": config.get("lemma_merge"),
            # 换来源维度必须换缓存键，否则会拿牌组的旧结果去画标签维度
            "source_dimension": config.get("source_dimension") or "deck",
            "index": index_meta.get("built"),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value


def cache_dir(col) -> str:
    """缓存放用户配置目录，不往 addons21 里写东西。"""
    try:
        base = col.mw.pm.profileFolder()
    except Exception:
        base = None
    if not base:
        import tempfile

        base = tempfile.gettempdir()
    path = os.path.join(base, "exam_vocab_stats")
    os.makedirs(path, exist_ok=True)
    return path


def load_cache(col, signature: str) -> dict | None:
    try:
        path = os.path.join(cache_dir(col), f"summary-{signature}.json")
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def save_cache(col, signature: str, payload: dict) -> None:
    try:
        path = os.path.join(cache_dir(col), f"summary-{signature}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"saved": time.time(), "payload": payload}, fh, ensure_ascii=False)
    except Exception:
        pass


def read_cached(col, signature: str) -> dict | None:
    data = load_cache(col, signature)
    if not data:
        return None
    return data.get("payload")


def clear_cache(col) -> int:
    path = cache_dir(col)
    removed = 0
    for name in os.listdir(path):
        if name.startswith("summary-") and name.endswith(".json"):
            try:
                os.remove(os.path.join(path, name))
                removed += 1
            except OSError:
                pass
    return removed
