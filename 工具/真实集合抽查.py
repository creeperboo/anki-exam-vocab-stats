"""开发用：在**真实集合的临时副本**上跑一遍统计，看看实际效果。

这个脚本只读你的集合：先把 collection.anki2 复制到临时目录，在副本上算，
算完把临时目录删掉——不碰原文件，也不经过 Anki 主程序。

它不需要 Anki 的 Python 环境（只用 sqlite3 + 插件自己的逻辑层），随时能跑。
跑完会打印：

  * 每个笔记类型被推荐成哪个取词字段、判成什么语言、默认勾没勾；
  * 英语轴 / 日语轴每个词表的「总词数 / 已覆盖 / 覆盖率 / 未覆盖」；
  * 学习状态分布、覆盖来源前几名、待确认的词、未覆盖示例；
  * 几条自检（分母是否与词库一致、未覆盖是否等于总数减已覆盖）。

用法（PowerShell）：

    $py = "C:\\Program Files\\Lenovo\\ModelMgr\\Plugins\\Image\\python.exe"
    & $py "工具\\真实集合抽查.py"                    # 自动取最近修改的那个用户配置
    & $py "工具\\真实集合抽查.py" --profile creeperboo
    & $py "工具\\真实集合抽查.py" --collection "D:\\某处\\collection.anki2"
    & $py "工具\\真实集合抽查.py" --limit 3000       # 只统计前 3000 条笔记，跑得快
    & $py "工具\\真实集合抽查.py" --deck 日语        # 只看名字含「日语」的牌组
    & $py "工具\\真实集合抽查.py" --builder-demo 20 # 另外拼 20 张四级补漏卡草稿（只读）
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "源码")
sys.path.insert(0, SRC)

import analysis  # noqa: E402
import vocab_logic as V  # noqa: E402


# --------------------------------------------------------------------- 只读适配器


class _Note:
    """只有 fields / id 的极简 note。"""

    __slots__ = ("fields", "id")

    def __init__(self, nid, fields):
        self.id = nid
        self.fields = fields


class _DeckRef:
    __slots__ = ("id", "name")

    def __init__(self, did, name):
        self.id = did
        self.name = name


class _DB:
    def __init__(self, con):
        self.con = con

    def all(self, sql, *params):
        return self.con.execute(sql, params).fetchall()

    def scalar(self, sql, *params):
        row = self.con.execute(sql, params).fetchone()
        return row[0] if row else None


class _Decks:
    def __init__(self, rows):
        self._by_id = {int(did): name for did, name in rows}

    def name(self, did):
        return self._by_id.get(int(did), str(did))

    def get(self, did):
        name = self._by_id.get(int(did))
        return {"id": int(did), "name": name} if name else None

    def children(self, did):
        prefix = self.name(did) + "::"
        return [key for key, name in self._by_id.items() if name.startswith(prefix)]

    def all_names_and_ids(self):
        return [_DeckRef(did, name) for did, name in self._by_id.items()]


class _Models:
    def __init__(self, models):
        self._by_id = models
        self._by_name = {m["name"]: m for m in models.values()}

    def get(self, ntid):
        return self._by_id.get(int(ntid))

    def all(self):
        return list(self._by_id.values())

    def by_name(self, name):
        return self._by_name.get(name)


_RE_NOTE_SEARCH = re.compile(r'^note:"(.+)"$')


class ReadOnlyCollection:
    """把真实集合包成 analysis 需要的接口，全程只读。"""

    def __init__(self, con):
        self.con = con
        self.mw = None
        notetypes = {}
        for mid, name in con.execute("select id, name from notetypes"):
            notetypes[int(mid)] = {"id": int(mid), "name": name, "flds": []}
        for ntid, _ord, name in con.execute(
            "select ntid, ord, name from fields order by ntid, ord"
        ):
            model = notetypes.get(int(ntid))
            if model is not None:
                model["flds"].append({"name": name})
        self.models = _Models(notetypes)
        # decks.name 这一列在 Anki 里是 collate unicase，本机 sqlite 不认识这个排序规则，
        # 所以既不能 order by name 也不能用它做比较——全读进内存按 id 排序后再用。
        self.decks = _Decks(list(con.execute("select id, name from decks order by id")))
        self.db = _DB(con)

    def find_notes(self, query):
        """只支持 note:"名称" 这一种搜索式（字段抽样要用）。"""
        match = _RE_NOTE_SEARCH.match((query or "").strip())
        if not match:
            raise ValueError(f'抽查脚本只支持 note:"名称" 形式的搜索：{query}')
        model = self.models.by_name(match.group(1))
        if not model:
            return []
        rows = self.con.execute(
            "select id from notes where mid = ? limit 500", (model["id"],)
        ).fetchall()
        return [int(row[0]) for row in rows]

    def get_note(self, nid):
        row = self.con.execute(
            "select id, flds from notes where id = ?", (int(nid),)
        ).fetchone()
        if not row:
            return None
        return _Note(int(row[0]), row[1].split("\x1f"))


# --------------------------------------------------------------------- 工具


def find_collection(profile: str | None) -> str:
    base = os.path.join(os.environ.get("APPDATA", ""), "Anki2")
    if profile:
        path = os.path.join(base, profile, "collection.anki2")
        if not os.path.isfile(path):
            raise SystemExit(f"找不到集合：{path}")
        return path
    candidates = []
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name, "collection.anki2")
            if os.path.isfile(path):
                candidates.append(path)
    if not candidates:
        raise SystemExit(f"{base} 下没有找到任何 collection.anki2")
    return max(candidates, key=os.path.getmtime)


def copy_collection(source: str) -> tuple:
    """把集合（含 wal/shm）复制到临时目录，返回 (临时目录, 副本路径)。"""
    workdir = tempfile.mkdtemp(prefix="evs_real_")
    target = os.path.join(workdir, "collection.anki2")
    shutil.copy2(source, target)
    for suffix in ("-wal", "-shm"):
        side = source + suffix
        if os.path.isfile(side):
            shutil.copy2(side, target + suffix)
    return workdir, target


def load_matchers():
    data = os.path.join(SRC, "data")
    exam = V.load_json_gz(os.path.join(data, "exam_index.json.gz"))
    lemma = V.load_json_gz(os.path.join(data, "lemma_index.json.gz"))
    jlpt = V.load_json_gz(os.path.join(data, "jlpt_index.json.gz"))
    return {
        "en": V.EnglishMatcher(exam, lemma, merge_lemma=True),
        "ja": V.JapaneseMatcher(jlpt),
        "en_meta": exam.get("meta", {}),
        "ja_meta": jlpt.get("meta", {}),
    }


def label_of(code: str) -> str:
    return V.ALL_EXAM_LABELS.get(code) or V.JLPT_LABELS.get(code) or code


def apply_limit(collected: dict, limit: int) -> dict:
    """只保留前 limit 条笔记（笔记可能产出多个词条，所以按笔记 id 截断）。"""
    if not limit:
        return collected
    keep: set = set()
    for entry in collected["entries"]:
        if len(keep) >= limit and entry["note_id"] not in keep:
            continue
        keep.add(entry["note_id"])
    entries = [e for e in collected["entries"] if e["note_id"] in keep]
    collected["entries"] = entries
    collected["scanned_notes"] = len(keep)
    collected["scanned_cards"] = sum(e["card_count"] for e in entries)
    return collected


# --------------------------------------------------------------------- 打印


def print_detected(detected: dict) -> None:
    print("\n【字段识别】")
    rows = sorted(detected.values(), key=lambda r: (not r["enabled"], r["notetype_name"]))
    for row in rows:
        mark = "✔ 统计" if row["enabled"] else "– 跳过"
        field = "、".join(row["fields"]) or "—"
        reading = f"（读音兜底 {row['reading_field']}）" if row.get("reading_field") else ""
        reason = f"　{row['reason']}" if row.get("reason") else ""
        samples = int(row.get("samples") or 0)
        hits = int(row.get("hits") or 0)
        rate = row["hit_rate"] * 100
        if row.get("looks_like_sentence"):
            rate_text = f"像例句/长文本（抽样 {samples} 条）"
        elif samples:
            rate_text = f"应试词命中率 {rate:.0f}%（抽样 {samples} 条，命中 {hits} 条）"
        else:
            rate_text = f"应试词命中率 {rate:.0f}%"
        print(
            f"  {mark:<6} {row['notetype_name']}：{field}{reading}"
            f"　[{rate_text}]{reason}"
        )


def print_axis(summary: dict, language: str, title: str) -> None:
    data = summary["languages"][language]
    print(
        f"\n【{title}】范围内唯一词 {data['words']} 个，"
        f"命中词表 {data['matched_words']} 个，未命中 {data['unmatched_words']} 个，"
        f"待确认 {data['pending_words']} 个"
    )
    print(f"  {'词表':<10}{'总词数':>9}{'已覆盖':>9}{'覆盖率':>9}{'未覆盖':>9}")
    for row in data["coverage"]:
        print(
            f"  {row['label']:<10}{row['total']:>9}{row['covered']:>9}"
            f"{str(row['rate']) + '%':>9}{row['missing']:>9}"
        )
    if not data["words"]:
        print("  （这个轴在范围内没有词，多半是字段没勾选或牌组选错了）")


def print_states(summary: dict) -> None:
    print("\n【学习状态分布】")
    for language, title in (("en", "英语轴"), ("ja", "日语轴")):
        data = summary["languages"][language]
        print(
            f"  {title}　唯一词："
            + "　".join(f"{r['label']} {r['words']}（{r['pct']}%）" for r in data["states"])
        )
        by_unit = data.get("states_by_unit") or {}
        if by_unit.get("note"):
            print(
                "　　　　　笔记：" + "　".join(f"{r['label']} {r['words']}" for r in by_unit["note"])
                + "　｜　卡片：" + "　".join(f"{r['label']} {r['words']}" for r in by_unit["card"])
            )


def print_sources(summary: dict) -> None:
    print("\n【覆盖来源前 5（按最具体的子牌组）】")
    for language, codes in (("en", ("cet46", "ky", "ielts", "toefl")), ("ja", ("jlpt", "n5", "n1"))):
        for code in codes:
            rows = (summary["languages"][language].get("sources") or {}).get(code) or []
            if not rows:
                continue
            top = "；".join(f"{r['name']} {r['count']} 个（{r['pct']}%）" for r in rows[:5])
            print(f"  {label_of(code)}：{top}")


def print_pending(summary: dict) -> None:
    pending = [w for w in summary["words"] if not w["confident"]]
    if not pending:
        return
    print(f"\n【待确认（{len(pending)} 个，未计入覆盖）】")
    for word in pending[:10]:
        alts = "、".join(word.get("alternatives") or [])[:60]
        print(f"  {word['key']}（{word['language']}，来自 {word['source']}）候选：{alts}")
    if len(pending) > 10:
        print(f"  …还有 {len(pending) - 10} 个")


def print_uncovered(matchers: dict, summary: dict) -> None:
    print("\n【未覆盖示例（各词表前 8 个）】")
    for language, codes in (
        ("en", ("cet4", "cet6", "ky", "ielts", "toefl", "gre")),
        ("ja", ("n5", "n4")),
    ):
        for code in codes:
            rows = analysis.uncovered_words(matchers, summary, language, code, limit=8)
            if not rows:
                continue
            print(f"  {label_of(code)}：" + "、".join(r["key"] for r in rows))


def print_gap_reasons(matchers: dict, summary: dict) -> dict:
    """把每个词表的未覆盖词按原因分一分：真未覆盖 / 识别失败 / 待确认。"""
    tally = {"真未覆盖": 0, "识别失败": 0, "待确认": 0}
    unrecognized: list[str] = []
    for language, codes in (
        ("en", V.ENGLISH_EXAM_CODES),
        ("ja", V.JLPT_CODES),
    ):
        for code in codes:
            for row in analysis.uncovered_words(matchers, summary, language, code):
                tally[row["reason"]] = tally.get(row["reason"], 0) + 1
                if row["reason"] == analysis.GAP_UNRECOGNIZED and len(unrecognized) < 20:
                    unrecognized.append(f"{language}:{row['key']}")
    print("\n【未覆盖的原因（按全部词表合计）】")
    print(
        f"  真未覆盖 {tally['真未覆盖']}（词表里没有这个词，该做的补漏就是这一类）"
        f"　｜　识别失败 {tally['识别失败']}（词表里有、卡片里也有，是我们的匹配漏了）"
        f"　｜　待确认 {tally['待确认']}（歧义，不猜，界面算未覆盖）"
    )
    if unrecognized:
        print("  识别失败示例：" + "、".join(unrecognized))
    return tally


def run_builder_demo(summary: dict, matchers: dict, count: int, code: str = "cet4") -> int:
    """在真实集合的临时副本上拼一遍补漏卡草稿（**只读**，不写集合、不建牌组）。

    拿当前范围里该词表「真未覆盖」的前 N 个词，用插件内置素材库拼草稿，逐条打印
    单词 / 音标 / 释义 / 例句 / 例句译 / 音频，并核对音频文件名真的在音频包里。
    返回失败条数（0 = 全过）。
    """
    import zipfile  # noqa: PLC0415

    import builder as B  # noqa: PLC0415
    import resources as RES  # noqa: PLC0415

    language = "ja" if code in set(V.JLPT_CODES) else "en"
    label = label_of(code)
    gaps = [
        row
        for row in analysis.uncovered_words(matchers, summary, language, code, limit=count * 5)
        if row["reason"] == analysis.GAP_MISSING
    ][:count]
    print(f"\n【补漏制卡抽查：{label} · {len(gaps)} 个真未覆盖词】只拼草稿，不写集合、不建牌组")
    if not gaps:
        print("  这个词表当前没有「真未覆盖」的词，跳过。")
        return 0

    rows = [{**row, "language": language, "code": code, "label": label} for row in gaps]
    res = RES.LocalResources({"language": language})
    drafts, skipped = B.build_drafts(rows, res, {"limit": count})

    audio_zip = os.path.join(HERE, "素材构建", "exam_materials_audio.zip")
    bundle_names: set = set()
    if os.path.isfile(audio_zip):
        with zipfile.ZipFile(audio_zip) as archive:
            bundle_names = {os.path.basename(n) for n in archive.namelist()}
    installed = res.media_installed()
    media_names: set = set()
    if installed.get("installed"):
        media_names = set(os.listdir(installed["dir"]))

    failures = 0
    for draft in drafts:
        reading = " ".join(
            x for x in (draft.get("reading", ""), draft.get("phonetic", "")) if x
        )
        audio = draft.get("audio_name") or os.path.basename(draft.get("audio_path") or "")
        example = (draft.get("example") or "").replace("\n", " ")
        trans = (draft.get("example_translation") or "").replace("\n", " ")
        print(
            f"  · {draft['surface']:<16} [{reading or '（无音标/假名）'}] "
            f"{(draft.get('definition') or '').splitlines()[0][:34] if draft.get('definition') else '（无释义）'}"
        )
        print(f"      例句：{example[:70] or '（无例句）'}")
        print(f"      译：　{trans[:60] or '（空）'}")
        print(f"      音频：{audio or '（无）'}　标签：{draft.get('exam_tag')}　牌组：{draft.get('deck')}")
        if not (draft.get("surface") and draft.get("definition") and example and audio):
            print("      ! 单词/释义/例句/音频 有缺项")
            failures += 1
        if audio and bundle_names and audio not in bundle_names:
            print("      ! 音频文件不在音频包里")
            failures += 1

    tally: dict = {}
    for row in skipped:
        tally[row.get("reason") or "?"] = tally.get(row.get("reason") or "?", 0) + 1
    print(
        f"  草稿 {len(drafts)} 张（失败 {failures}），跳过 {len(skipped)} 个"
        + ("：" + "、".join(f"{k} {v}" for k, v in tally.items()) if tally else "")
    )
    print(f"  音频包条目 {len(bundle_names)} 个　｜　本机已装音频 {installed.get('files', 0)} 个")
    return failures


def print_checks(summary: dict, matchers: dict) -> int:
    print("\n【自检】")
    failures = []
    for language, meta in (
        ("en", matchers["en"].exam_counts),
        ("ja", matchers["ja"].level_counts),
    ):
        for row in summary["languages"][language]["coverage"]:
            if row["code"] in ("cet46", "jlpt"):
                continue
            want = int(meta.get(row["code"], 0))
            if row["total"] != want:
                failures.append(f"{label_of(row['code'])} 分母 {row['total']} != 词库 {want}")
            if row["missing"] != max(row["total"] - row["covered"], 0):
                failures.append(f"{label_of(row['code'])} 未覆盖 != 总数 − 已覆盖")
            if row["covered"] > row["total"]:
                failures.append(f"{label_of(row['code'])} 已覆盖超过了词表总数")
    tally = print_gap_reasons(matchers, summary)
    if tally["识别失败"]:
        failures.append(f"识别失败 {tally['识别失败']} 个（应为 0）")
    if failures:
        for line in failures:
            print("  ✗ " + line)
        return 1
    print(
        "  ✓ 每个词表的分母与词库一致；未覆盖 = 总数 − 已覆盖；已覆盖没有超过分母；"
        "识别失败 = 0"
    )
    return 0


# --------------------------------------------------------------------- 主流程


def main() -> int:
    parser = argparse.ArgumentParser(description="在真实集合的副本上跑一遍应试词汇统计")
    parser.add_argument("--profile", help="Anki 用户配置名（默认取最近修改的那个）")
    parser.add_argument("--collection", help="直接指定 collection.anki2 路径")
    parser.add_argument("--limit", type=int, default=0, help="只统计前 N 条笔记（调试用）")
    parser.add_argument("--deck", default="", help="只看名字里含这个词的牌组")
    parser.add_argument(
        "--builder-demo",
        type=int,
        default=0,
        help="另外拿该词表「真未覆盖」的前 N 个词拼一遍补漏卡草稿（只读，不写集合）",
    )
    parser.add_argument("--builder-code", default="cet4", help="补漏抽查用哪个词表（默认 cet4）")
    parser.add_argument("--keep", action="store_true", help="保留临时副本")
    args = parser.parse_args()

    source = args.collection or find_collection(args.profile)
    print(f"集合来源：{source}")
    workdir, target = copy_collection(source)
    print(f"已复制到临时目录：{workdir}")
    con = sqlite3.connect(target)
    try:
        con.execute("pragma wal_checkpoint(truncate)")
        con.execute("pragma query_only=on")  # 从这里开始彻底只读
        col = ReadOnlyCollection(con)

        notes = int(con.execute("select count(*) from notes").fetchone()[0])
        cards = int(con.execute("select count(*) from cards").fetchone()[0])
        print(f"集合规模：{notes} 条笔记 / {cards} 张卡 / {len(col.models.all())} 个笔记类型")

        matchers = load_matchers()
        detected = analysis.detect_fields(col, matchers)
        print_detected(detected)

        config = {
            "field_map": detected,
            "enabled_exams": list(V.ENGLISH_EXAM_CODES),
            "enabled_levels": list(V.JLPT_CODES),
            "lemma_merge": True,
        }
        field_map = {
            int(key): value
            for key, value in detected.items()
            if value.get("enabled") and value.get("fields")
        }

        deck_ids = [d.id for d in col.decks.all_names_and_ids()]
        if args.deck:
            deck_ids = [d.id for d in col.decks.all_names_and_ids() if args.deck in d.name]
        scope = {
            "deck_ids": deck_ids,
            "include_subdecks": True,
            "tags_include": [],
            "tags_exclude": [],
            "tag_mode": "any",
            "notetype_ids": [],
            "states": ["new", "learn", "review", "suspended"],
            "search": "",
        }
        scope_text = "全部牌组" if not args.deck else f"名字含「{args.deck}」的牌组"

        started = time.time()
        collected = apply_limit(
            analysis.collect_entries(col, scope, field_map, matchers), args.limit
        )
        collect_seconds = time.time() - started
        started = time.time()
        summary = analysis.summarize(col, collected, config, matchers)
        summarize_seconds = time.time() - started

        print(
            f"\n统计范围：{scope_text}；扫了 {summary['scanned_notes']} 条笔记 / "
            f"{summary['scanned_cards']} 张卡（查库 {collect_seconds:.2f} 秒 + "
            f"汇总 {summarize_seconds:.2f} 秒）"
        )
        skipped = summary.get("skipped") or {}
        if skipped:
            print("  跳过的笔记：" + "、".join(f"{k} {v} 条" for k, v in skipped.items()))

        print_axis(summary, "en", "英语轴")
        print_axis(summary, "ja", "日语轴")
        print_states(summary)
        print_sources(summary)
        print_pending(summary)
        print_uncovered(matchers, summary)
        demo_failures = 0
        if args.builder_demo > 0:
            demo_failures = run_builder_demo(
                summary, matchers, args.builder_demo, args.builder_code
            )
        checks = print_checks(summary, matchers)
        return checks + demo_failures
    finally:
        try:
            con.close()
        except Exception:
            pass
        if args.keep:
            print(f"\n临时副本保留在：{workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
