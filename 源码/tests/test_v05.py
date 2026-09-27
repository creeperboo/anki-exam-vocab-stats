"""应试词汇统计插件 · v0.3「零漏识别 + 素材全内置」纯逻辑测试。

这一份盯本轮四件事：

1. 英文去变形规则表（整理自本机 fushi 应用的 ``transforms/en.json``）逐条生效，
   且「原词自己就在词表里」时绝不绕路去变形；
2. 短语 / 连字符归一（make-up ↔ makeup、per cent ↔ percent）、日语 ``;`` 并列写法拆分、
   读音命中去重键统一、接头词与 〜する / 活用还原；
3. 界面只给「已覆盖 / 未覆盖」两态，内部细分类照旧保留；
4. 内置素材库完整性（词表里每个词都必须有释义 / 例句 / 音频），
   以及音频包下载通道（``update_logic.materials_url`` / ``download_to_file``）。
"""

from __future__ import annotations

import gzip
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)
PROJECT = os.path.dirname(SRC)
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import analysis as A  # noqa: E402
import builder as B  # noqa: E402
import resources as R  # noqa: E402
import update_logic as U  # noqa: E402
import vocab_logic as V  # noqa: E402

DATA = os.path.join(SRC, "data")
TOOLS = os.path.join(PROJECT, "工具")


def load_gz(name: str) -> dict:
    with gzip.open(os.path.join(DATA, name), "rt", encoding="utf-8") as handle:
        return json.load(handle)


_MATCHERS = None


def matchers() -> dict:
    """真词库的匹配器，整个测试文件共用一份（加载一次约 0.2 秒）。"""
    global _MATCHERS
    if _MATCHERS is None:
        _MATCHERS = {
            "en": V.EnglishMatcher(
                load_gz("exam_index.json.gz"), load_gz("lemma_index.json.gz")
            ),
            "ja": V.JapaneseMatcher(load_gz("jlpt_index.json.gz")),
        }
    return _MATCHERS


def synthetic_english_matcher(words) -> V.EnglishMatcher:
    """只认识给定几个词的迷你英文匹配器，用来单独验规则表。"""
    word_exam = {word: "cet4" for word in words}
    index = {"meta": {}, "word_exam": word_exam, "group_exam": dict(word_exam), "freq": {}}
    return V.EnglishMatcher(index, {"lemma": {}}, merge_lemma=True)


def load_builder_tool():
    """按路径加载 ``工具\\构建词库索引.py``（它不 import anki，可以直接跑）。"""
    path = os.path.join(TOOLS, "构建词库索引.py")
    spec = importlib.util.spec_from_file_location("exam_index_builder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 英文去变形


class TestEnglishDeformRules(unittest.TestCase):
    """每条规则都要「真能命中词表」才被采纳，这里用迷你词表逐条验。"""

    def check(self, surface, base, words=None):
        matcher = synthetic_english_matcher(words or [base])
        got = matcher.resolve(surface)
        self.assertEqual(got.key, base, f"{surface} 应该还原成 {base}，实际 {got.key}")
        self.assertTrue(got.confident, f"{surface} 不该是待确认")
        self.assertEqual(got.exams, frozenset({"cet4"}))
        return got

    # 名词复数 ------------------------------------------------------------

    def test_plural_es(self):
        self.check("boxes", "box")

    def test_plural_s(self):
        self.check("dogs", "dog")

    def test_plural_ies_to_y(self):
        self.check("cities", "city")

    def test_plural_ves_to_fe(self):
        self.check("knives", "knife")

    def test_plural_ves_to_f(self):
        self.check("leaves", "leaf")

    # 所有格 --------------------------------------------------------------

    def test_possessive_s(self):
        self.check("dog's", "dog")

    def test_possessive_plural(self):
        # students' -> students -> student（两层）
        got = self.check("students'", "student")
        self.assertEqual(got.matched_by, "suffix")

    # 过去式 --------------------------------------------------------------

    def test_past_regular(self):
        self.check("walked", "walk")

    def test_past_double_consonant(self):
        self.check("stopped", "stop")

    def test_past_ied_to_y(self):
        self.check("tried", "try")

    # 现在分词 ------------------------------------------------------------

    def test_ing_y_rule_is_declared(self):
        # -ying -> -ie（dying -> die）在表里；因为要留够词干，规则对 dying 这种
        # 单字母词干不触发，实际由 ECDICT 词元表兜住（见 TestRealEnglishMatcher）
        self.assertIn(("ying", "ie"), V.EN_SUFFIX_RULES)

    def test_ing_double_consonant(self):
        self.check("sitting", "sit")

    def test_ing_keep_e(self):
        self.check("making", "make")

    # 第三人称 ------------------------------------------------------------

    def test_third_person_goes(self):
        self.check("goes", "go")

    def test_third_person_tries(self):
        self.check("tries", "try")

    # 比较级 / 最高级 -----------------------------------------------------

    def test_comparative(self):
        self.check("bigger", "big")

    def test_superlative(self):
        self.check("biggest", "big")

    def test_comparative_ier(self):
        self.check("happier", "happy")

    def test_superlative_iest(self):
        self.check("happiest", "happy")

    # 副词 ----------------------------------------------------------------

    def test_adverb_ily_to_y(self):
        self.check("happily", "happy")

    def test_adverb_ly_to_le(self):
        self.check("gently", "gentle")

    def test_adverb_ably_to_able(self):
        self.check("possibly", "possible")

    # 派生后缀 ------------------------------------------------------------

    def test_suffix_ful(self):
        self.check("useful", "use")

    def test_suffix_ness(self):
        self.check("happiness", "happy")

    def test_suffix_ment(self):
        self.check("movement", "move")

    def test_suffix_tion(self):
        self.check("creation", "create")

    # 前缀 ----------------------------------------------------------------

    def test_prefix_un(self):
        self.check("unhappy", "happy")

    def test_prefix_in(self):
        self.check("incorrect", "correct")

    def test_prefix_im(self):
        self.check("impossible", "possible")

    def test_prefix_dis(self):
        self.check("disagree", "agree")

    def test_prefix_needs_a_real_stem(self):
        # into 剥掉 in 只剩 to（太短），规则不许采纳，也不许凭空造词
        self.assertEqual(V.deform_candidates("into"), [])

    # 多层 ----------------------------------------------------------------

    def test_two_layers(self):
        chain = V.english_deform_candidates("unhappily")
        self.assertIn("unhappy", chain)
        self.assertIn("happy", chain)

    def test_candidate_never_equals_input(self):
        for word in ("stopped", "cities", "unhappy", "knives"):
            self.assertNotIn(word, V.deform_candidates(word))


class TestRealEnglishMatcher(unittest.TestCase):
    """真词库上的回归：之前报过的几个词不能又变成未覆盖。"""

    def key(self, word):
        return matchers()["en"].resolve(word)

    def test_children_to_child(self):
        self.assertEqual(self.key("children").key, "child")

    def test_ran_to_run(self):
        self.assertEqual(self.key("ran").key, "run")

    def test_better_hits_good_or_well(self):
        got = self.key("better")
        self.assertIn(got.key, {"good", "well"})
        self.assertTrue(got.confident)
        self.assertIn("cet4", got.exams)

    def test_stopped_to_stop(self):
        self.assertEqual(self.key("stopped").key, "stop")

    def test_dying_to_die(self):
        self.assertEqual(self.key("dying").key, "die")

    def test_studies_to_study(self):
        self.assertEqual(self.key("studies").key, "study")

    def test_sitting_to_sit(self):
        self.assertEqual(self.key("sitting").key, "sit")

    def test_biggest_to_big(self):
        self.assertEqual(self.key("biggest").key, "big")

    def test_does_is_not_pending(self):
        got = self.key("does")
        self.assertTrue(got.confident, "does 有 do/doe 多候选，但不该被判成待确认")
        self.assertEqual(got.key, "do")

    def test_original_word_still_wins(self):
        # 原词自己就在词表里时不许绕路：useful 就是 useful 这个词
        self.assertEqual(self.key("useful").key, "useful")


class TestPhraseForms(unittest.TestCase):
    """含空格 / 连字符的短语：两种写法必须是同一个词。"""

    def en(self, word):
        return matchers()["en"].resolve(word)

    def test_hyphen_and_plain_share_one_key(self):
        self.assertEqual(self.en("make-up").key, self.en("makeup").key)

    def test_space_and_plain_share_one_key(self):
        self.assertEqual(self.en("per cent").key, self.en("percent").key)

    def test_two_word_phrase(self):
        self.assertEqual(self.en("health care").key, self.en("healthcare").key)

    def test_prefix_phrase(self):
        self.assertEqual(self.en("post-war").key, self.en("postwar").key)

    def test_hyphenated_wordlist_word_is_not_torn_apart(self):
        got = self.en("co-operative")
        self.assertTrue(got.confident)
        self.assertTrue(got.exams, "co-operative 词表里本来就有，必须原样命中")

    def test_phrase_variants_shape(self):
        self.assertEqual(
            V.phrase_variants("make-up"), ["make-up", "makeup", "make up"]
        )
        self.assertEqual(
            V.phrase_variants("per cent"), ["per cent", "percent", "per-cent"]
        )

    def test_phrase_hits_the_same_exam_set(self):
        self.assertEqual(set(self.en("make-up").exams), set(self.en("makeup").exams))


# ---------------------------------------------------------------- 日语


class TestJapaneseForms(unittest.TestCase):
    def ja(self, word):
        return matchers()["ja"].resolve(word)

    def test_reading_and_kanji_share_one_key(self):
        self.assertEqual(self.ja("たべる").key, self.ja("食べる").key)

    def test_reading_hit_keeps_the_wordlist_spelling(self):
        # 卡片写假名、词表写汉字，去重键统一成词表那一份
        self.assertEqual(self.ja("たべる").key, "食べる")

    def test_polite_forms_restore_to_base(self):
        for surface in ("食べます", "食べた", "食べない"):
            self.assertEqual(self.ja(surface).key, "食べる", surface)

    def test_suru_compound(self):
        for surface in ("勉強する", "勉強します", "勉強した"):
            self.assertEqual(self.ja(surface).key, "勉強", surface)

    def test_prefix_candidates_cover_all_three(self):
        self.assertIn("金", V.japanese_prefix_candidates("お金"))
        self.assertIn("飯", V.japanese_prefix_candidates("御飯"))
        self.assertEqual(V.japanese_prefix_candidates("食べる"), ["食べる"])

    def test_prefix_word_itself_is_hit(self):
        for surface in ("お金", "御飯", "ご飯"):
            got = self.ja(surface)
            self.assertTrue(got.confident, surface)
            self.assertTrue(got.levels, surface)

    def test_long_mark_is_kept_by_default(self):
        self.assertEqual(V.normalize_japanese("ビール"), "びーる")
        self.assertEqual(V.normalize_japanese("ビール", drop_long_mark=True), "びる")

    def test_building_and_beer_are_two_words(self):
        building = self.ja("ビル")
        beer = self.ja("ビール")
        self.assertNotEqual(building.key, beer.key)
        self.assertEqual(building.key, "びる")
        self.assertEqual(beer.key, "びーる")

    def test_kata_and_hira_are_the_same(self):
        self.assertEqual(self.ja("キロ").key, self.ja("きろ").key)

    def test_ambiguous_reading_is_not_counted(self):
        got = self.ja("あし")
        self.assertFalse(got.confident, "あし 对应 足 / 脚 等多个词，只允许进待确认")
        self.assertFalse(got.hit)

    def test_kanji_forms_are_kept_separate(self):
        self.assertNotEqual(self.ja("足").key, self.ja("脚").key)

    def test_unknown_word_is_a_clean_miss(self):
        got = self.ja("について")
        self.assertEqual(got.exams, frozenset())
        self.assertEqual(got.levels, frozenset())
        self.assertFalse(got.alternatives)


class TestJapaneseMultiFormSplit(unittest.TestCase):
    """词表里同一格塞了多个写法（``キロ; キログラム``）时必须逐个拆开。"""

    def setUp(self):
        self.tool = load_builder_tool()

    def test_semicolon(self):
        self.assertEqual(
            self.tool._split_multi("キロ; キログラム"), ["キロ", "キログラム"]
        )

    def test_semicolon_kanji(self):
        self.assertEqual(self.tool._split_multi("足; 脚"), ["足", "脚"])

    def test_slash(self):
        self.assertEqual(
            self.tool._split_multi("キロ/キログラム"), ["キロ", "キログラム"]
        )

    def test_middle_dot(self):
        self.assertEqual(
            self.tool._split_multi("キロ・キログラム"), ["キロ", "キログラム"]
        )

    def test_lists_are_all_real_entries(self):
        index = load_gz("jlpt_index.json.gz")
        by_word = index["by_word"]
        for word in ("きろ", "きろぐらむ", "足", "脚"):
            self.assertIn(word, by_word, f"{word} 应该由并列写法拆出来")


# ---------------------------------------------------------------- 两态原因


class TestTwoStateReason(unittest.TestCase):
    def test_covered(self):
        self.assertEqual(A.display_reason(""), A.STATE_COVERED)

    def test_every_gap_kind_is_uncovered(self):
        for reason in (A.GAP_MISSING, A.GAP_UNRECOGNIZED, A.GAP_PENDING):
            self.assertEqual(A.display_reason(reason), A.STATE_UNCOVERED, reason)

    def test_only_two_labels_exist(self):
        self.assertEqual({A.STATE_COVERED, A.STATE_UNCOVERED}, {"已覆盖", "未覆盖"})

    def test_internal_kinds_survive(self):
        # 细分还在：只用于「不给其实有卡的词重复制卡」和探针断言
        self.assertEqual(A.GAP_MISSING, "真未覆盖")
        self.assertEqual(A.GAP_UNRECOGNIZED, "识别失败")
        self.assertEqual(A.GAP_PENDING, "待确认")

    def test_entry_reason(self):
        self.assertEqual(
            A.entry_gap_reason({"confident": True, "exams": {"cet4"}}, True), ""
        )
        self.assertEqual(A.entry_gap_reason({"confident": False}, True), A.GAP_PENDING)
        self.assertEqual(
            A.entry_gap_reason({"confident": True, "exams": set()}, True),
            A.GAP_UNRECOGNIZED,
        )
        self.assertEqual(
            A.entry_gap_reason({"confident": True, "exams": set()}, False),
            A.GAP_MISSING,
        )


# ---------------------------------------------------------------- 内置素材


class TestBuiltinMaterials(unittest.TestCase):
    def test_english_index_is_complete(self):
        index = load_gz("exam_index.json.gz")
        words = load_gz("en_materials.json.gz")["words"]
        missing = [w for w in index["word_exam"] if w not in words]
        self.assertEqual(missing[:10], [], f"英文词表有 {len(missing)} 个词没有内置素材")

    def test_english_every_word_has_meaning_example_audio(self):
        words = load_gz("en_materials.json.gz")["words"]
        no_meaning = [w for w, r in words.items() if not (r.get("t") or r.get("d"))]
        no_example = [w for w, r in words.items() if not r.get("ex")]
        no_audio = [w for w, r in words.items() if not r.get("a")]
        self.assertEqual(no_meaning[:10], [], f"缺释义 {len(no_meaning)}")
        self.assertEqual(no_example[:10], [], f"缺例句 {len(no_example)}")
        self.assertEqual(no_audio[:10], [], f"缺音频 {len(no_audio)}")

    def test_japanese_index_is_complete(self):
        index = load_gz("jlpt_index.json.gz")
        words = load_gz("ja_materials.json.gz")["words"]
        missing = [w for w in index["by_word"] if w not in words]
        self.assertEqual(missing[:10], [], f"日语词表有 {len(missing)} 个词没有内置素材")

    def test_japanese_every_word_has_meaning_example_audio(self):
        words = load_gz("ja_materials.json.gz")["words"]
        for field, label in (("m", "释义"), ("ex", "例句"), ("a", "音频")):
            gaps = [w for w, r in words.items() if not r.get(field)]
            self.assertEqual(gaps[:10], [], f"日语有 {len(gaps)} 个词缺{label}")

    def test_japanese_meaning_is_chinese_first(self):
        words = load_gz("ja_materials.json.gz")["words"]
        han = re.compile(r"[\u4e00-\u9fff]")
        chinese = sum(1 for r in words.values() if han.search(r.get("m") or ""))
        self.assertGreater(chinese / len(words), 0.9, "日语释义应该以中文为主")

    def test_manifest_matches_the_zip_on_disk(self):
        manifest = R.LocalResources().manifest()
        zip_path = os.path.join(TOOLS, "素材构建", U.MATERIALS_ASSET)
        self.assertTrue(os.path.isfile(zip_path), "音频包还没构建")
        self.assertEqual(len(manifest.get("sha256") or ""), 64)
        self.assertEqual(int(manifest.get("bytes") or 0), os.path.getsize(zip_path))
        self.assertEqual(manifest.get("file"), U.MATERIALS_ASSET)
        self.assertGreater(int(manifest.get("files") or 0), 20000)

    def test_lookup_returns_full_material(self):
        res = R.LocalResources(cache_dir=tempfile.mkdtemp())
        row = res.lookup("abandon", "en")
        self.assertTrue(row["phonetic"])
        self.assertTrue(row["translation"])
        self.assertTrue(row["example"])
        self.assertTrue(row["audio_name"].endswith(".mp3"))
        ja = res.lookup("食べる", "ja")
        self.assertTrue(ja["reading"])
        self.assertTrue(ja["translation"])
        self.assertTrue(ja["example"])
        self.assertTrue(ja["audio_name"].endswith(".mp3"))

    def test_dictionary_lookup_is_gone(self):
        # v0.3 起素材在构建期并进内置索引，运行期不再扫本机词典
        self.assertEqual(R.LocalResources().dictionary_lookup(["abandon"], "en"), {})

    def test_literal_escapes_are_cleaned(self):
        """ECDICT 的释义里带字面 ``\\n``（两个字符），落进卡片字段前必须还原。"""
        self.assertEqual(R._clean_text(r"基本操作系统\n牛属"), "基本操作系统\n牛属")
        self.assertEqual(R._clean_text(r"制表\t符"), "制表 符")
        self.assertEqual(R._clean_text("没有转义"), "没有转义")
        self.assertEqual(R._clean_text(""), "")
        self.assertEqual(R._clean_text("真\n换行"), "真\n换行")
        # 单个反斜杠不是转义，不能乱吃
        self.assertEqual(R._clean_text(r"C:\path"), r"C:\path")

    def test_lookup_never_returns_literal_escapes(self):
        """索引里允许带字面转义，但 lookup 吐出来的四个文本字段必须是干净的。"""
        words = load_gz("en_materials.json.gz")["words"]
        dirty = [
            w
            for w, r in words.items()
            if any("\\n" in (r.get(k) or "") or "\\t" in (r.get(k) or "")
                   for k in ("t", "d", "ex", "exz"))
        ]
        res = R.LocalResources(cache_dir=tempfile.mkdtemp())
        for word in dirty:
            row = res.lookup(word, "en")
            for key in ("translation", "definition", "example", "example_translation"):
                self.assertNotIn("\\n", row[key], f"{word} 的 {key} 还带字面转义")
                self.assertNotIn("\\t", row[key], f"{word} 的 {key} 还带字面制表符")


class FakeResources:
    """按词回素材，缺什么由测试指定。"""

    def __init__(self, table):
        self.table = table

    def lookup(self, word, language="en", reading="", dict_entries=None):
        return dict(self.table.get(word) or {})

    def dictionary_lookup(self, terms, language):
        return {}


class TestBuilderSkipsThinCards(unittest.TestCase):
    """素材不全（少释义 / 少例句 / 少音频）的词不进补漏队列。"""

    def build(self, material):
        rows = [
            {"key": "abandon", "language": "en", "code": "cet4", "reason": A.GAP_MISSING}
        ]
        return B.build_drafts(rows, FakeResources({"abandon": material}))

    def full(self):
        return {
            "translation": "放弃",
            "definition": "to give up",
            "example": "They abandoned the plan.",
            "audio_name": "en_1.mp3",
            "audio_path": "C:/media/en_1.mp3",
        }

    def test_full_material_becomes_a_card(self):
        drafts, skipped = self.build(self.full())
        self.assertEqual(len(drafts), 1)
        self.assertEqual(skipped, [])
        self.assertTrue(drafts[0]["has_definition"])
        self.assertTrue(drafts[0]["has_example"])
        self.assertTrue(drafts[0]["has_audio"])

    def test_missing_meaning_is_skipped(self):
        material = self.full()
        material["translation"] = ""
        material["definition"] = ""
        drafts, skipped = self.build(material)
        self.assertEqual(drafts, [])
        self.assertIn("释义", skipped[0]["reason"])

    def test_missing_example_is_skipped(self):
        material = self.full()
        material["example"] = ""
        drafts, skipped = self.build(material)
        self.assertEqual(drafts, [])
        self.assertIn("例句", skipped[0]["reason"])

    def test_missing_audio_is_skipped(self):
        material = self.full()
        material["audio_name"] = ""
        material["audio_path"] = ""
        drafts, skipped = self.build(material)
        self.assertEqual(drafts, [])
        self.assertIn("音频", skipped[0]["reason"])

    def test_pending_word_is_not_built(self):
        rows = [{"key": "x", "language": "en", "code": "cet4", "reason": A.GAP_PENDING}]
        drafts, skipped = B.build_drafts(rows, FakeResources({"x": self.full()}))
        self.assertEqual(drafts, [])
        self.assertEqual(skipped[0]["reason"], A.GAP_PENDING)


class TestBuilderAudioGate(unittest.TestCase):
    """「每张补漏卡都必须有音频」：音频没到本机的草稿不许落卡。"""

    def drafts(self, audio_paths):
        return [
            {"key": f"w{i}", "language": "en", "code": "cet4", "audio_path": path}
            for i, path in enumerate(audio_paths)
        ]

    def test_all_ready(self):
        ready, not_ready = B.split_by_audio(self.drafts(["a.mp3", "b.mp3"]))
        self.assertEqual([d["key"] for d in ready], ["w0", "w1"])
        self.assertEqual(not_ready, [])

    def test_none_ready(self):
        ready, not_ready = B.split_by_audio(self.drafts(["", ""]))
        self.assertEqual(ready, [])
        self.assertEqual([d["key"] for d in not_ready], ["w0", "w1"])

    def test_mixed_keeps_order(self):
        ready, not_ready = B.split_by_audio(self.drafts(["a.mp3", "", "c.mp3"]))
        self.assertEqual([d["key"] for d in ready], ["w0", "w2"])
        self.assertEqual([d["key"] for d in not_ready], ["w1"])

    def test_audio_name_alone_is_not_enough(self):
        """只有音频文件名（音频包还没解压）不算就绪。"""
        rows = [
            {
                "key": "abandon",
                "language": "en",
                "code": "cet4",
                "reason": A.GAP_MISSING,
                "audio_name": "en_1.mp3",
                "audio_path": "",
            }
        ]
        ready, not_ready = B.split_by_audio(rows)
        self.assertEqual(ready, [])
        self.assertEqual(len(not_ready), 1)

    def test_empty_input(self):
        self.assertEqual(B.split_by_audio([]), ([], []))


# ---------------------------------------------------------------- 素材包下载


class FakeResponse:
    def __init__(self, payload, fail_after=None):
        self._buffer = io.BytesIO(payload)
        self._fail_after = fail_after
        self._sends = 0
        self.headers = {"Content-Length": str(len(payload))}

    def read(self, size=-1):
        if self._fail_after is not None and self._sends >= self._fail_after:
            raise OSError("连接被重置")
        self._sends += 1
        return self._buffer.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestMaterialsDownload(unittest.TestCase):
    def setUp(self):
        self._real = urllib.request.urlopen
        self.addCleanup(self._restore)
        self.dir = tempfile.mkdtemp()

    def _restore(self):
        urllib.request.urlopen = self._real

    def patch(self, payload, fail_after=None):
        urllib.request.urlopen = lambda request, timeout=None: FakeResponse(
            payload, fail_after
        )

    def test_materials_url_is_a_release_asset(self):
        url = U.materials_url()
        self.assertTrue(url.startswith("https://github.com/"))
        self.assertIn("/releases/latest/download/", url)
        self.assertTrue(url.endswith(U.MATERIALS_ASSET))

    def test_download_writes_the_whole_file(self):
        payload = b"PK" + b"x" * (200 * 1024)
        self.patch(payload)
        seen = []
        path = os.path.join(self.dir, "audio.zip")
        wrote = U.download_to_file(
            U.materials_url(), path, progress=lambda done, total: seen.append(done)
        )
        self.assertEqual(wrote, len(payload))
        self.assertEqual(os.path.getsize(path), len(payload))
        self.assertTrue(seen, "进度回调应该被调用")
        self.assertEqual(seen[-1], len(payload))

    def test_download_failure_removes_the_half_file(self):
        self.patch(b"PK" + b"y" * (200 * 1024), fail_after=1)
        path = os.path.join(self.dir, "audio.zip")
        with self.assertRaises(Exception):
            U.download_to_file(U.materials_url(), path, retries=0)
        self.assertFalse(os.path.exists(path), "失败的半截包必须删掉")

    def test_download_retries_then_succeeds(self):
        payload = b"PK" + b"z" * (200 * 1024)
        calls = {"n": 0}

        def fake(request, timeout=None):
            calls["n"] += 1
            # 第一次传到一半断线，第二次完整传完
            return FakeResponse(payload, fail_after=1 if calls["n"] == 1 else None)

        urllib.request.urlopen = fake
        path = os.path.join(self.dir, "audio.zip")
        wrote = U.download_to_file(U.materials_url(), path, retries=1)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(wrote, len(payload))
        self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
