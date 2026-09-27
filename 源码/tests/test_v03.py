"""应试词汇统计插件 · v0.3 纯逻辑测试（不需要 Anki）。

这一份专门盯 v0.3 改的四处：

1. ``int < tuple`` 崩溃与父牌组范围（analysis 的 ID 归一 / 牌组展开）；
2. 识别矫正：含空格/连字符短语、日语读音去重键、接头词兜底、三类缺口原因；
3. 一键补漏制卡（builder 的草稿 / 去重 / 限量 / 字段 / 文案）；
4. 内置素材库（词表与素材全部打包，跑起来只读内置索引，不再扫本机词典）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import analysis  # noqa: E402
import builder as B  # noqa: E402
import resources as R  # noqa: E402
import vocab_logic as V  # noqa: E402

DATA = os.path.join(SRC, "data")


def load_matchers(merge: bool = True) -> dict:
    exam = V.load_json_gz(os.path.join(DATA, "exam_index.json.gz"))
    lemma = V.load_json_gz(os.path.join(DATA, "lemma_index.json.gz"))
    jlpt = V.load_json_gz(os.path.join(DATA, "jlpt_index.json.gz"))
    return {
        "en": V.EnglishMatcher(exam, lemma, merge_lemma=merge),
        "ja": V.JapaneseMatcher(jlpt),
        "en_meta": exam.get("meta", {}),
        "ja_meta": jlpt.get("meta", {}),
    }


MATCHERS = None


def matchers() -> dict:
    global MATCHERS
    if MATCHERS is None:
        MATCHERS = load_matchers()
    return MATCHERS


# ---------------------------------------------------------------- 假集合


class FakeDecks:
    """行为对齐本机 Anki 26.9.3：children() 回 [(名字, id), …]。"""

    def __init__(self, names=None, children=None, official=None):
        self._names = names or {}
        self._children = children or {}
        self._official = official

    def name(self, did):
        return self._names.get(did, str(did))

    def children(self, did):
        return list(self._children.get(did, ()))

    def deck_and_child_ids(self, did):
        if self._official is None:
            raise AttributeError("这个假对象没有 deck_and_child_ids")
        return list(self._official.get(did, ()))


class FakeDb:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def all(self, sql, *params):
        return list(self.rows)

    def scalar(self, sql):
        return 0


class FakeModels:
    def __init__(self, models=()):
        self._models = {int(m["id"]): m for m in models}

    def all(self):
        return list(self._models.values())

    def get(self, ntid):
        return self._models.get(int(ntid))


class FakeCol:
    def __init__(self, names=None, children=None, official=None, rows=None, models=()):
        self.decks = FakeDecks(names, children, official)
        self.db = FakeDb(rows)
        self.models = FakeModels(models)


def make_entry(
    key,
    language="en",
    exams=(),
    levels=(),
    state="new",
    deck_id=1,
    notetype_id=10,
    note_id=1,
    confident=True,
    surface=None,
    card_count=1,
    alternatives=(),
    meaning="",
    tags=(),
    notetype_ids=None,
):
    return {
        "note_id": note_id,
        "notetype_id": notetype_id,
        "notetype_ids": list(notetype_ids) if notetype_ids is not None else [notetype_id],
        "deck_ids": [deck_id],
        "deck_id": deck_id,
        "tags": list(tags),
        "state": state,
        "card_count": card_count,
        "language": language,
        "surface": surface if surface is not None else key,
        "word": surface if surface is not None else key,
        "key": key,
        "exams": list(exams),
        "levels": list(levels),
        "confident": confident,
        "matched_by": "own",
        "alternatives": list(alternatives),
        "meaning": meaning,
    }


CONFIG = {
    "enabled_exams": list(V.ENGLISH_EXAM_CODES),
    "enabled_levels": list(V.JLPT_CODES),
    "source_dimension": "deck",
}


def summarize(col, entries, config=None, custom_lists=None):
    collected = {
        "entries": entries,
        "scanned_notes": len({e["note_id"] for e in entries}),
        "scanned_cards": len(entries),
        "skipped": {},
    }
    return analysis.summarize(
        col, collected, config or dict(CONFIG), matchers(), custom_lists=custom_lists
    )


# ---------------------------------------------------------------- ① ID 归一


class TestIdNormalization(unittest.TestCase):
    """崩溃根因：把 (名字, id) 元组混进 int 列表，sorted() 立刻抛 TypeError。"""

    def test_as_int(self):
        self.assertEqual(analysis.as_int(("英语::四级", 17)), 17)
        self.assertEqual(analysis.as_int([("英语::四级", 17)]), 17)
        self.assertEqual(analysis.as_int("17"), 17)
        self.assertEqual(analysis.as_int(17), 17)
        self.assertIsNone(analysis.as_int(None))
        self.assertIsNone(analysis.as_int(""))
        self.assertIsNone(analysis.as_int(("a", "b", "c")))
        self.assertIsNone(analysis.as_int(("a", "b")))

    def test_as_int_list_dedupes_and_drops_junk(self):
        got = analysis.as_int_list([("a", 1), 1, "2", None, ("x", "y"), 3])
        self.assertEqual(got, [1, 2, 3])

    def test_deck_and_child_ids_prefers_official_api(self):
        col = FakeCol(official={1: [1, 11, 12]})
        self.assertEqual(analysis.deck_and_child_ids(col, 1), [1, 11, 12])

    def test_deck_and_child_ids_tolerates_tuple_children(self):
        col = FakeCol(children={1: [("子::A", 11), ("子::B", 12)]})
        self.assertEqual(analysis.deck_and_child_ids(col, 1), [1, 11, 12])

    def test_nodeck_scope_does_not_crash_on_tuple_children(self):
        # 这一条就是用户遇到的 `'<' not supported between instances of
        # 'int' and 'tuple'`：以前直接把元组塞进列表再 sorted()。
        col = FakeCol(children={1: [("子::A", 11), ("子::B", 12)]})
        where, params = analysis.nodeck_scope(
            {"deck_ids": [1], "include_subdecks": True}, col
        )
        self.assertIn("c.did in (?,?,?)", where)
        self.assertEqual(params, [1, 11, 12])

    def test_nodeck_scope_normalizes_tuple_deck_ids(self):
        where, params = analysis.nodeck_scope(
            {"deck_ids": [("父", 5)], "include_subdecks": False}, FakeCol()
        )
        self.assertEqual(params, [5])

    def test_primary_source_ignores_tuple_deck_ids(self):
        word = {"deck_ids": [("脏数据", 9), 3]}
        names = {3: "三级", 9: "九级"}
        got = analysis._primary_source(FakeCol(), word, names, {3: 0, 9: 0}, {3: 1, 9: 1})
        self.assertEqual(got, "三级")


class TestParentDeckScope(unittest.TestCase):
    def test_root_kept_when_official_api_empty(self):
        col = FakeCol(children={})
        self.assertEqual(analysis.deck_and_child_ids(col, 7), [7])

    def test_scope_expands_every_child_once(self):
        col = FakeCol(
            children={1: [("A", 11), ("B", 12)], 11: [("A::子", 111)]}
        )
        _where, params = analysis.nodeck_scope(
            {"deck_ids": [1, 11], "include_subdecks": True}, col
        )
        self.assertEqual(params, [1, 11, 12, 111])


# ---------------------------------------------------------------- ② 短语


class TestEnglishPhrases(unittest.TestCase):
    def test_hyphenated_phrase_hits(self):
        got = matchers()["en"].resolve("co-operative")
        self.assertEqual(got.key, "co-operative")
        self.assertTrue(got.hit)
        self.assertIn("ielts", got.exams)

    def test_hyphen_is_not_stripped_by_normalize(self):
        self.assertEqual(V.normalize_english("T-Shirt"), "t-shirt")
        self.assertEqual(V.normalize_english("x-ray"), "x-ray")

    def test_phrase_with_space_hits(self):
        got = matchers()["en"].resolve("account for")
        self.assertEqual(got.key, "account for")
        self.assertTrue(got.hit)


# ---------------------------------------------------------------- ③ 日语矫正


class TestJapaneseCanonical(unittest.TestCase):
    def test_reading_and_kanji_share_one_key(self):
        m = matchers()["ja"]
        # 同一份词表里 茶 是 n3；「ちゃ」是它的读音，「お茶」是带接头词的写法。
        kanji = m.resolve("茶")
        kana = m.resolve("ちゃ")
        self.assertEqual(kana.key, kanji.key, "读音命中要归到词表里的写法")
        self.assertTrue(kana.hit)

    def test_prefix_form_merges_to_wordlist_form(self):
        m = matchers()["ja"]
        prefixed = m.resolve("おちゃ")
        self.assertEqual(prefixed.key, "お茶")
        self.assertEqual(m.resolve("お茶").key, "お茶")

    def test_canonical_prefers_wordlist_form(self):
        m = matchers()["ja"]
        self.assertIn(m._canonical_for_reading("ちゃ", "ちゃ"), m.by_word)

    def test_prefix_candidates(self):
        self.assertEqual(V.japanese_prefix_candidates("ご飯"), ["ご飯", "飯"])
        self.assertEqual(V.japanese_prefix_candidates("御飯"), ["御飯", "飯"])
        self.assertEqual(V.japanese_prefix_candidates("食べる"), ["食べる"])

    def test_prefix_fallback_only_when_base_is_real(self):
        # 用一份合成词表：词表里只有「飯」，没有「ご飯」，才能确定性验证接头词兜底。
        index = {
            "by_word": {"飯": ["n5"]},
            "by_reading": {"めし": ["n5"]},
            "reading_word": {"めし": "飯"},
            "ambiguous_readings": [],
            "levels": {"n5": 1},
            "meanings": {},
        }
        m = V.JapaneseMatcher(index)
        got = m.resolve("ご飯")
        self.assertEqual(got.key, "飯")
        self.assertEqual(got.matched_by, "prefix")
        self.assertTrue(got.hit)
        # 剥掉之后词表里也没有的，不能硬猜
        miss = m.resolve("おぬぬめ")
        self.assertFalse(miss.hit)
        self.assertEqual(miss.matched_by, "none")


# ---------------------------------------------------------------- ④ 缺口原因


class TestGapReasons(unittest.TestCase):
    def build(self):
        col = FakeCol({1: "四级"})
        entries = [
            make_entry("abandon", exams=["cet4"], deck_id=1, note_id=1),
            # 认出来了但没进任何词表 → 识别失败（词表里确实有它的情况）
            make_entry("notaword", deck_id=1, note_id=2),
            # 歧义 → 待确认
            make_entry(
                "pending", exams=["cet4"], deck_id=1, note_id=3,
                confident=False, alternatives=["a", "b"],
            ),
        ]
        return summarize(col, entries)

    def test_range_index_statuses(self):
        summary = self.build()
        index = analysis.range_gap_index(summary)
        self.assertEqual(index["en"]["abandon"], "covered")
        self.assertEqual(index["en"]["notaword"], "unmatched")
        self.assertEqual(index["en"]["pending"], "pending")

    def test_classify_missing(self):
        index = analysis.range_gap_index(self.build())
        got = analysis.classify_gap("en", "ability", index)
        self.assertEqual(got, analysis.GAP_MISSING)

    def test_classify_unrecognized(self):
        index = analysis.range_gap_index(self.build())
        got = analysis.classify_gap("en", "notaword", index)
        self.assertEqual(got, analysis.GAP_UNRECOGNIZED)

    def test_classify_pending(self):
        index = analysis.range_gap_index(self.build())
        got = analysis.classify_gap("en", "pending", index)
        self.assertEqual(got, analysis.GAP_PENDING)

    def test_entry_gap_reason(self):
        self.assertEqual(
            analysis.entry_gap_reason({"confident": True}, in_list=True),
            analysis.GAP_UNRECOGNIZED,
        )
        self.assertEqual(
            analysis.entry_gap_reason({"confident": True}, in_list=False),
            analysis.GAP_MISSING,
        )
        self.assertEqual(
            analysis.entry_gap_reason({"confident": False}, in_list=True),
            analysis.GAP_PENDING,
        )

    def test_uncovered_words_have_reason(self):
        summary = self.build()
        rows = analysis.uncovered_words(matchers(), summary, "en", "cet4")
        self.assertTrue(rows)
        allowed = {analysis.GAP_MISSING, analysis.GAP_UNRECOGNIZED, analysis.GAP_PENDING}
        for row in rows:
            self.assertIn(row["reason"], allowed, row["key"])
        # 排序：真未覆盖在最前
        self.assertEqual(rows[0]["reason"], analysis.GAP_MISSING)

    def test_covered_keys_for_uses_this_list_only(self):
        col = FakeCol({1: "混"})
        entries = [
            make_entry("abandon", exams=["cet4"], note_id=1),
            make_entry("kaoyan", exams=["ky"], note_id=2),
        ]
        summary = summarize(col, entries)
        self.assertEqual(analysis.covered_keys_for(summary, "en", "cet4"), {"abandon"})
        self.assertEqual(
            analysis.covered_keys_for(summary, "en", "cet46"), {"abandon"}
        )


# ---------------------------------------------------------------- ⑤ 一键补漏


class FakeResources:
    """最小的素材替身：默认给一条「素材齐全」的素材，可按键覆盖成残缺版。"""

    def __init__(self, data=None, fail=False):
        self.data = data or {}
        self.fail = fail

    def dictionary_lookup(self, terms, language):
        return {}

    def lookup(self, word, language, reading="", dict_entries=None):
        if self.fail:
            raise RuntimeError("坏了")
        row = {
            "reading": "",
            "phonetic": "əˈbændən",
            "definition": "",
            "translation": "放弃",
            "example": "He abandoned it.",
            "example_translation": "他放弃了它。",
            "audio_name": "en_abandon.mp3",
            "audio_path": "/media/en_abandon.mp3",
            "audio_source": "内置音频包",
            "sources": ["ECDICT（MIT）"],
        }
        row.update(self.data.get(word) or {})
        return row


# 素材不全的一条：释义 / 音频 / 例句都空
BARE_MATERIAL = {
    "translation": "",
    "definition": "",
    "example": "",
    "audio_name": "",
    "audio_path": "",
}


def row(key, code="cet4", language="en", reason=analysis.GAP_MISSING, **extra):
    base = {
        "key": key,
        "surface": key,
        "language": language,
        "code": code,
        "label": "",
        "meaning": "",
        "reason": reason,
    }
    base.update(extra)
    return base


class TestBuilder(unittest.TestCase):
    def test_only_missing_reason_becomes_card(self):
        rows = [
            row("abandon"),
            row("notaword", reason=analysis.GAP_UNRECOGNIZED),
            row("pending", reason=analysis.GAP_PENDING),
        ]
        drafts, skipped = B.build_drafts(rows, FakeResources())
        self.assertEqual([d["key"] for d in drafts], ["abandon"])
        self.assertEqual({s["reason"] for s in skipped}, {analysis.GAP_UNRECOGNIZED, analysis.GAP_PENDING})

    def test_incomplete_material_is_skipped(self):
        """素材不全的词不进补漏队列：不出「有的有音频、有的没有」的半截卡片。"""
        rows = [row("abandon"), row("bare")]
        res = FakeResources({"bare": BARE_MATERIAL})
        drafts, skipped = B.build_drafts(rows, res)
        self.assertEqual([d["key"] for d in drafts], ["abandon"])
        self.assertTrue(skipped)
        self.assertIn("素材不全", skipped[0]["reason"])
        for piece in ("释义", "音频", "例句"):
            self.assertIn(piece, skipped[0]["reason"])

    def test_draft_carries_full_material(self):
        drafts, _skipped = B.build_drafts([row("abandon")], FakeResources())
        draft = drafts[0]
        self.assertTrue(draft["has_definition"])
        self.assertTrue(draft["has_example"])
        self.assertTrue(draft["has_audio"])
        self.assertTrue(draft["audio_ready"])
        self.assertEqual(draft["audio_name"], "en_abandon.mp3")

    def test_dedupe_and_limit(self):
        rows = [row("a"), row("a", code="cet6"), row("b"), row("c")]
        drafts, skipped = B.build_drafts(rows, FakeResources(), {"limit": 2})
        self.assertEqual([d["key"] for d in drafts], ["a", "b"])
        self.assertTrue(any("上限" in s["reason"] for s in skipped))
        self.assertTrue(any("重复" in s["reason"] for s in skipped))

    def test_row_without_code_is_skipped(self):
        drafts, skipped = B.build_drafts([row("a", code="")], FakeResources())
        self.assertEqual(drafts, [])
        self.assertEqual(skipped[0]["reason"], "没指定词表（打标签/建牌组要靠它）")

    def test_deck_name_for_builtin_and_label(self):
        self.assertEqual(B.deck_name_for("cet4", "en"), "应试补漏::四级")
        self.assertEqual(B.deck_name_for("n3", "ja"), "应试补漏::JLPT-N3")
        self.assertEqual(
            B.deck_name_for("custom:x", "en", "我的词表"), "应试补漏::我的词表"
        )

    def test_tag_for_code(self):
        self.assertEqual(B.tag_for_code("cet4"), "应试::四级")
        self.assertEqual(B.tag_for_code("n1"), "应试::JLPT-N1")
        # 合并码不生成标签
        self.assertEqual(B.tag_for_code("cet46"), "")
        self.assertEqual(B.tag_for_code("cet456"), "")

    def test_note_fields_and_media_name(self):
        draft = {
            "key": "abandon",
            "surface": "abandon",
            "reading": "",
            "phonetic": "əˈbændən",
            "definition": "放弃",
            "example": "He abandoned it.",
            "example_translation": "他放弃了它。",
            "audio": "exam_abandon.mp3",
            "exam_tag": "应试::四级",
            "source_line": "词表：四级",
        }
        fields = B.note_fields(draft)
        self.assertEqual(len(fields), len(B.CARD_FIELDS))
        self.assertEqual(fields[0], "abandon")
        self.assertEqual(fields[1], "əˈbændən")
        self.assertEqual(fields[5], "[sound:exam_abandon.mp3]")
        self.assertEqual(B.media_name_for({"key": "abandon", "audio_path": "x/a.mp3"}), "exam_abandon.mp3")
        self.assertEqual(B.media_name_for({"key": "walk", "audio_path": "x/a.wav"}), "exam_walk.wav")

    def test_combine_definition_prefers_chinese(self):
        self.assertEqual(B._combine_definition("放弃", "to give up"), "放弃\nto give up")
        self.assertEqual(B._combine_definition("放弃", "放弃"), "放弃")
        self.assertEqual(B._combine_definition("", "to give up"), "to give up")

    def test_plan_text_reports_counts(self):
        drafts, skipped = B.build_drafts(
            [row("abandon"), row("ability", reason=analysis.GAP_PENDING)], FakeResources()
        )
        text = B.plan_text(drafts, skipped)
        self.assertIn("1", text)
        self.assertIn("应试补漏", text)
        self.assertIn("新卡上限", text)

    def test_export_table_has_header_and_skipped_rows(self):
        drafts, skipped = B.build_drafts([row("abandon")], FakeResources())
        header, rows = B.export_table(drafts, skipped)
        self.assertEqual(len(header), 11)
        self.assertEqual(rows[0][0], "abandon")

    def test_resources_failure_skips_all(self):
        """素材层挂了 → 一个都不做（宁可少做，也不出半截卡片）。"""
        drafts, skipped = B.build_drafts([row("abandon")], FakeResources(fail=True))
        self.assertEqual(drafts, [])
        self.assertTrue(skipped)
        self.assertIn("素材不全", skipped[0]["reason"])

    def test_source_line_marks_non_official(self):
        line = B.source_line({"sources": ["ECDICT（MIT）"]}, "cet4", "en")
        self.assertIn("ECDICT", line)
        self.assertIn("非官方", line)


# ---------------------------------------------------------------- ⑦ 本机素材


class TestResources(unittest.TestCase):
    """v0.3：素材全部内置在 ``data\\*_materials.json.gz``，不再扫本机词典。"""

    def res(self):
        return R.LocalResources({}, cache_dir=tempfile.mkdtemp())

    def test_english_word_has_builtin_material(self):
        got = self.res().lookup("abandon", "en")
        self.assertTrue(got["translation"], "四级词应该有内置中文释义")
        self.assertTrue(got["example"], "英语例句在构建期补齐，必须非空")
        self.assertTrue(got["audio_name"], "每条词都要有内置音频名")
        self.assertTrue(got["sources"])

    def test_japanese_word_has_builtin_material(self):
        got = self.res().lookup("食べる", "ja")
        self.assertTrue(got["translation"], "日语词应该有内置中文释义")
        self.assertTrue(got["example"])
        self.assertTrue(got["audio_name"])

    def test_lookup_unknown_english_word_is_safe(self):
        got = self.res().lookup("zzznotarealwordzzz", "en")
        self.assertEqual(got["translation"], "")
        self.assertEqual(got["audio_name"], "")
        self.assertEqual(got["audio_path"], "")

    def test_lookup_unknown_japanese_word_is_safe(self):
        got = self.res().lookup("ぬぬぬぬ", "ja")
        self.assertEqual(got["audio_path"], "")
        self.assertEqual(got["definition"], "")
        self.assertEqual(got["translation"], "")

    def test_audio_path_only_when_media_unpacked(self):
        """没解压音频包时只有音频名、没有本地路径；解压后才给路径。"""
        folder = tempfile.mkdtemp()
        res = R.LocalResources({}, cache_dir=folder)
        before = res.lookup("abandon", "en")
        self.assertEqual(before["audio_path"], "")
        media = res.media_dir()
        os.makedirs(media, exist_ok=True)
        with open(os.path.join(media, before["audio_name"]), "wb") as handle:
            handle.write(b"\x00")
        after = R.LocalResources({}, cache_dir=folder).lookup("abandon", "en")
        self.assertTrue(after["audio_path"])
        self.assertEqual(after["audio_source"], "内置音频包")

    def test_dictionary_lookup_is_always_empty(self):
        """v0.3 不再扫本机词典，这个接口保留只为兼容旧签名。"""
        self.assertEqual(self.res().dictionary_lookup(["abandon"], "en"), {})

    def test_describe_reports_builtin_index(self):
        describe = self.res().describe()
        self.assertGreater(describe["en_words"], 10000)
        self.assertGreater(describe["ja_words"], 5000)
        self.assertEqual(describe["errors"], {})

    def test_media_install_extracts_mp3(self):
        folder = tempfile.mkdtemp()
        media = tempfile.mkdtemp()
        pack = os.path.join(media, "pack.zip")
        with zipfile.ZipFile(pack, "w") as zf:
            zf.writestr("en_x.mp3", b"\x00\x01")
            zf.writestr("readme.txt", "x")
        res = R.LocalResources({}, cache_dir=folder)
        got = res.install_media(pack)
        self.assertEqual(got["files"], 1)
        self.assertTrue(os.path.isfile(os.path.join(res.media_dir(), "en_x.mp3")))
        self.assertTrue(res.media_installed()["installed"])

    def test_sha256_of_matches_hashlib(self):
        path = os.path.join(tempfile.mkdtemp(), "x.bin")
        with open(path, "wb") as handle:
            handle.write(b"hello")
        import hashlib

        self.assertEqual(R.LocalResources.sha256_of(path), hashlib.sha256(b"hello").hexdigest())


# ---------------------------------------------------------------- ⑧ 取词


class TestCollectWords(unittest.TestCase):
    def test_collect_words_dedupes_by_lemma(self):
        rows = [
            (1, 10, "child\x1f", "", 100, 1, 2, 2),
            (1, 10, "child\x1f", "", 101, 1, 2, 2),
            (2, 10, "children\x1f", "", 102, 1, 2, 2),
        ]
        col = FakeCol(
            names={1: "英语"},
            rows=rows,
            models=[{"id": 10, "name": "CEFR", "flds": [{"name": "Word"}]}],
        )
        field_map = {10: {"fields": ["Word"], "language": "en"}}
        words = analysis.collect_words(
            col, {"deck_ids": [1], "include_subdecks": True}, field_map, matchers()
        )
        self.assertEqual([w["key"] for w in words], ["child"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
