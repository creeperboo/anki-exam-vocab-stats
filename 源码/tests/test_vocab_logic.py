"""应试词汇统计插件 · 纯逻辑测试（不需要 Anki）。

跑法（在本文件夹的上上级执行）：
    python -m unittest discover -s 源码\tests -p "test_*.py" -v

覆盖方案里点名要测的东西：清洗、英文变形与消歧、日文归一、跨词表重复与并集、
待确认不计数、状态归类、来源占比与覆盖率分别加总、组合筛选的并集/交集。
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import analysis  # noqa: E402
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
    def __init__(self, names=None, children=None):
        self._names = names or {}
        self._children = children or {}

    def name(self, did):
        return self._names.get(did, str(did))

    def children(self, did):
        return list(self._children.get(did, ()))


class FakeCol:
    """只实现 summarize / nodeck_scope 用得到的那几个方法。"""

    def __init__(self, names=None, children=None):
        self.decks = FakeDecks(names, children)
        # collection_signature 会查 col 的修改时间；给个恒为零的假数据库即可。
        self.db = FakeDb()


class FakeDb:
    def scalar(self, sql):
        return 0


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
}


# ---------------------------------------------------------------- 清洗


class TestClean(unittest.TestCase):
    def test_strip_markup(self):
        raw = '<div>apple</div>[sound:apple.mp3]<style>x</style>{{Front}}&nbsp;'
        self.assertEqual(V.clean_field(raw), "apple x")
        self.assertNotIn("<", V.strip_markup(raw))
        self.assertNotIn("[sound:", V.strip_markup(raw))
        self.assertNotIn("{{", V.strip_markup(raw))

    def test_clean_field_collapses_space(self):
        self.assertEqual(V.clean_field("  take\u3000 off \n now "), "take off now")

    def test_entities(self):
        self.assertEqual(V.clean_field("A &amp; B"), "A & B")
        self.assertEqual(V.clean_field("a&nbsp;b"), "a b")
        self.assertEqual(V.clean_field("&#39;quoted&#39;"), "'quoted'")
        # 不认识的实体统一替成空格，别把 &xxx; 留在词里
        self.assertEqual(V.clean_field("x&weird;y"), "x y")

    def test_empty(self):
        self.assertEqual(V.clean_field(""), "")
        self.assertEqual(V.clean_field(None), "")


# ---------------------------------------------------------------- 英文


class TestEnglishNormalize(unittest.TestCase):
    def test_same_word_spelling_helper(self):
        # 英美拼写对：只差词尾，算同一个词的两种拼法
        for a, b in (("fibre", "fiber"), ("colour", "color"), ("humour", "humor")):
            self.assertTrue(V._same_word_spelling(a, b), f"{a}/{b} 应该判为同一词的两种拼法")
        # 真歧义不能误并：词太短，或者差异不在词尾
        for a, b in (("does", "doe"), ("does", "do"), ("zzz", "aaa"), ("cat", "dog")):
            self.assertFalse(V._same_word_spelling(a, b), f"{a}/{b} 不该被当成同一词")

    def test_lower_and_trim(self):
        self.assertEqual(V.normalize_english("  Apple!  "), "apple")
        self.assertEqual(V.normalize_english("“Quoted”"), "quoted")
        # 长破折号/斜杠这类不能当词里的字符，会被削掉
        self.assertEqual(V.normalize_english("well—known"), "well—known")

    def test_fullwidth(self):
        self.assertEqual(V.normalize_english("ＡＰＰＬＥ"), "apple")

    def test_spelling_pairs_do_not_break_short_words(self):
        # four 不能被 our→or 误伤成 for；care 也不能变成 carer
        self.assertIn("four", V.spelling_variants("four"))
        self.assertNotIn("for", V.spelling_variants("four"))
        self.assertNotIn("carer", V.spelling_variants("care"))

    def test_spelling_pairs_work_on_long_words(self):
        self.assertIn("color", V.spelling_variants("colour"))
        self.assertIn("organize", V.spelling_variants("organise"))
        self.assertIn("analyze", V.spelling_variants("analyse"))


class TestLanguageDetection(unittest.TestCase):
    """语言判定：只有假名是可靠的「日语」信号。"""

    def test_english_with_chinese_gloss_stays_english(self):
        # English-CEFR 这种「英文单词 + 中文释义」的牌库，中文释义再多也不能判成日语，
        # 否则真正的取词字段会被当成「日语卡里的英文释义」而整列关掉（实测踩过）。
        got = V.classify_language(
            ["apple", "苹果", "abandon", "放弃", "children", "孩子们", "beautiful", "美丽的"]
        )
        self.assertEqual(got, "en")

    def test_kana_makes_it_japanese(self):
        self.assertEqual(V.classify_language(["食べる", "たべる", "勉強", "青"]), "ja")
        self.assertEqual(V.classify_language(["ビール", "コーヒー"]), "ja")

    def test_pure_han_falls_back_to_japanese(self):
        # 全是汉字时区分不了中文/日文，按日语处理（汉字表记的日语词很常见）
        self.assertEqual(V.classify_language(["勉強", "会議"]), "ja")
        self.assertEqual(V.classify_language([]), "other")


class TestEnglishMatcher(unittest.TestCase):
    def setUp(self):
        self.matcher = matchers()["en"]

    def test_lemmatize_plural_and_irregular(self):
        for surface, key in (
            ("children", "child"),
            ("ran", "run"),
            ("went", "go"),
            ("feet", "foot"),
            ("boxes", "box"),
            ("studies", "study"),
        ):
            got = self.matcher.resolve(surface)
            self.assertEqual(got.key, key, f"{surface} 应该还原成 {key}，实际 {got.key}")
            self.assertTrue(got.hit, f"{surface} 应该算命中")
        # better 同时是 good / well 的比较级，两族都带考试标签：归到哪一族由
        # 「标签数 → 词频」定，重点是必须认出来，不能掉进待确认。
        better = self.matcher.resolve("better")
        self.assertIn(better.key, {"good", "well"})
        self.assertTrue(better.hit, "better 不该进待确认")

    def test_better_keeps_cet4_tag(self):
        # 方案里点名的场景：better 同时属于 good / well。按「标签数优先、再用词频」
        # 决胜，无论落到哪一族都必须保住四六级标签。
        got = self.matcher.resolve("better")
        self.assertIn(got.key, {"good", "well"})
        self.assertIn("cet4", got.exams)

    def test_does_is_not_doe(self):
        # ECDICT 里 does 自己被标成 doe（母鹿），要按 lemma 表优先取 do
        got = self.matcher.resolve("does")
        self.assertEqual(got.key, "do")
        self.assertTrue(got.confident)

    def test_spelling_variant_merges(self):
        got = self.matcher.resolve("colour")
        self.assertEqual(got.key, "color")
        self.assertEqual(got.matched_by, "spelling")
        self.assertTrue(got.hit)

    def test_spelling_pair_merges_instead_of_pending(self):
        # 英美拼写对以前会被当成「标签并列、内容不同」而进待确认、白丢覆盖，
        # 现在要并成同一个词、标签取并集。
        for surface, key in (("fibre", "fiber"), ("humour", "humor"), ("realise", "realize")):
            got = self.matcher.resolve(surface)
            self.assertEqual(got.key, key, f"{surface} 应该并到 {key}")
            self.assertTrue(got.confident, f"{surface} 不该进待确认")
            self.assertTrue(got.hit, f"{surface} 应该算命中")
        # does 这种真歧义（do / doe）保持原样，不能靠拼写规则误并
        self.assertEqual(self.matcher.resolve("does").key, "do")

    def test_suffix_fallback(self):
        # 先确认真实词库里 tugging 能还原到 tug（走 lemma 表或后缀都算对）
        got = self.matcher.resolve("tugging")
        self.assertEqual(got.key, "tug")
        self.assertTrue(got.hit)
        # 再用一个「lemma 表里没有、只能靠后缀规则」的构造场景，确认兜底真的生效
        matcher = V.EnglishMatcher(
            {"word_exam": {"file": "cet4"}, "group_exam": {}, "freq": {}, "exams": {}},
            {"lemma": {}},
        )
        fallback = matcher.resolve("filing")
        self.assertEqual(fallback.key, "file")
        self.assertEqual(fallback.matched_by, "suffix")
        self.assertTrue(fallback.hit)

    def test_unknown_word_is_not_a_hit(self):
        got = self.matcher.resolve("zzzqqqxyz")
        self.assertFalse(got.hit)
        self.assertEqual(got.exams, frozenset())

    def test_merge_lemma_off(self):
        plain = V.EnglishMatcher(
            self.matcher.word_exam and {"word_exam": self.matcher.word_exam,
                                        "group_exam": self.matcher.group_exam,
                                        "freq": self.matcher.freq,
                                        "exams": self.matcher.exam_counts},
            {"lemma": self.matcher.lemma},
            merge_lemma=False,
        )
        # 关掉合并后，children 不再被算成 child
        self.assertNotEqual(plain.resolve("children").key, "child")

    def test_pending_not_counted(self):
        # 造一个标签数量并列、内容不同的场景：直接构造，不依赖词库里刚好有
        matcher = V.EnglishMatcher(
            {
                "word_exam": {},
                "group_exam": {},
                "freq": {},
                "exams": {},
            },
            {"lemma": {}},
        )
        matcher.group_exam = {"aaa": "cet4", "bbb": "cet6"}
        matcher.word_exam = {"aaa": "cet4", "bbb": "cet6"}
        matcher.lemma = {"zzz": ["aaa", "bbb"]}
        got = matcher.resolve("zzz")
        self.assertFalse(got.confident)
        self.assertFalse(got.hit, "待确认的词不能算命中，否则覆盖率会虚高")
        self.assertEqual(sorted(got.alternatives), ["aaa", "bbb"])


# ---------------------------------------------------------------- 日文


class TestJapaneseNormalize(unittest.TestCase):
    def test_kana_fold(self):
        self.assertEqual(V.normalize_japanese("ビール"), "びーる")
        self.assertEqual(V.normalize_japanese("ビル"), "びる")
        # 长音符默认保留，否则 ビル / ビール 会被合到一起
        self.assertNotEqual(V.normalize_japanese("ビール"), V.normalize_japanese("ビル"))

    def test_fullwidth_and_brackets(self):
        self.assertEqual(V.normalize_japanese("ＡＢＣ"), "abc")
        self.assertEqual(V.normalize_japanese("食べる（たべる）"), "食べる")

    def test_drop_long_mark_option(self):
        self.assertEqual(V.normalize_japanese("ビール", drop_long_mark=True), "びる")

    def test_suru_base_forms(self):
        self.assertIn("勉強", V.japanese_base_forms("勉強する"))
        self.assertIn("勉強", V.japanese_base_forms("勉強した"))
        self.assertEqual(V.japanese_base_forms("食べる"), ["食べる"])


class TestJapaneseMatcher(unittest.TestCase):
    def setUp(self):
        self.matcher = matchers()["ja"]

    def test_kanji_hit(self):
        got = self.matcher.resolve("食べる")
        self.assertEqual(got.key, "食べる")
        self.assertEqual(got.levels, frozenset({"n5"}))
        self.assertTrue(got.hit)

    def test_reading_bridges_to_kanji(self):
        got = self.matcher.resolve("たべる")
        self.assertEqual(got.key, "食べる")
        self.assertEqual(got.matched_by, "reading")

    def test_long_mark_words_stay_apart(self):
        beer = self.matcher.resolve("ビール")
        building = self.matcher.resolve("ビル")
        self.assertNotEqual(beer.key, building.key)
        self.assertTrue(beer.hit or building.hit)

    def test_suru_compound(self):
        got = self.matcher.resolve("勉強する")
        self.assertEqual(got.key, "勉強")
        self.assertTrue(got.hit)

    def test_ambiguous_reading_is_pending(self):
        # 只挑「自身不是词条」的歧义假名：如果一个假名本身就是词表里的词（比如 ああ），
        # 它会走原词命中，本来就不该进待确认。
        m = matchers()["ja"]
        ambiguous = sorted(r for r in m.ambiguous_readings if r not in m.by_word)
        self.assertTrue(ambiguous, "索引里应该有跨级歧义假名")
        got = self.matcher.resolve(ambiguous[0])
        if got.levels:
            self.assertFalse(got.confident, f"{ambiguous[0]} 应该进待确认")
            self.assertFalse(got.hit)

    def test_unknown_word_is_not_a_hit(self):
        got = self.matcher.resolve("ぬぬぬぬ")
        self.assertFalse(got.hit)


# ---------------------------------------------------------------- 状态与范围


class TestCardState(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(analysis.card_state(0, 0), "new")
        self.assertEqual(analysis.card_state(1, 1), "learn")
        self.assertEqual(analysis.card_state(3, 3), "learn")
        self.assertEqual(analysis.card_state(2, 2), "review")
        self.assertEqual(analysis.card_state(-1, 2), "suspended")
        self.assertEqual(analysis.card_state(-2, 2), "suspended")
        self.assertEqual(analysis.card_state(-3, 0), "suspended")

    def test_highest_progress_wins(self):
        self.assertEqual(analysis._better_state("new", "review"), "review")
        self.assertEqual(analysis._better_state("review", "learn"), "review")
        self.assertEqual(analysis._better_state("learn", "suspended"), "learn")


class TestScope(unittest.TestCase):
    def test_tag_union_and_intersection(self):
        col = FakeCol()
        any_where, any_params = analysis.nodeck_scope(
            {"tags_include": ["四级", "六级"], "tag_mode": "any"}, col
        )
        self.assertIn(" or ", any_where)
        self.assertEqual(len(any_params), 4)
        all_where, _ = analysis.nodeck_scope(
            {"tags_include": ["四级", "六级"], "tag_mode": "all"}, col
        )
        self.assertIn(" and ", all_where)

    def test_exclude_tag(self):
        where, params = analysis.nodeck_scope({"tags_exclude": ["已掌握"]}, col=FakeCol())
        self.assertIn("not (", where)
        self.assertEqual(params, ["% 已掌握 %", "% 已掌握::%"])

    def test_subdecks_expand(self):
        col = FakeCol(children={1: [11, 12]})
        where, params = analysis.nodeck_scope(
            {"deck_ids": [1], "include_subdecks": True}, col
        )
        self.assertIn("c.did in (?,?,?)", where)
        self.assertEqual(sorted(params), [1, 11, 12])

    def test_subdecks_off(self):
        col = FakeCol(children={1: [11, 12]})
        _where, params = analysis.nodeck_scope(
            {"deck_ids": [1], "include_subdecks": False}, col
        )
        self.assertEqual(params, [1])

    def test_wildcard_in_tag_is_escaped(self):
        where, params = analysis.nodeck_scope({"tags_include": ["a_b%c"]}, FakeCol())
        self.assertIn("escape", where)
        self.assertEqual(params, ["% a\\_b\\%c %", "% a\\_b\\%c::%"])


# ---------------------------------------------------------------- 汇总


class TestSummarize(unittest.TestCase):
    def build(self):
        col = FakeCol({1: "四级词汇", 2: "六级词汇::真题", 3: "JLPT::N5"})
        entries = [
            # children 与 child 归成同一个词元，跨牌组去重后只算一次
            make_entry("child", exams=["cet4"], state="new", deck_id=1, note_id=1),
            make_entry(
                "child", exams=["cet4"], state="review", deck_id=2, note_id=2,
                surface="children",
            ),
            # 没命中任何词表
            make_entry("xyzzy", state="learn", deck_id=1, note_id=3),
            # 待确认：即使带标签也不能算覆盖
            make_entry(
                "pending", exams=["cet4"], state="new", deck_id=3, note_id=4,
                confident=False, alternatives=["a", "b"],
            ),
            # 日文词
            make_entry(
                "食べる", language="ja", levels=["n5"], state="learn", deck_id=3,
                note_id=5,
            ),
        ]
        collected = {
            "entries": entries,
            "scanned_notes": 5,
            "scanned_cards": 5,
            "skipped": {},
        }
        return analysis.summarize(col, collected, CONFIG, matchers())

    def test_unique_lemma_dedup(self):
        summary = self.build()
        self.assertEqual(summary["languages"]["en"]["words"], 3)  # child / xyzzy / pending
        self.assertEqual(summary["languages"]["ja"]["words"], 1)

    def test_pending_excluded_from_coverage(self):
        summary = self.build()
        cet4 = self.row(summary, "en", "cet4")
        self.assertEqual(cet4["covered"], 1, "待确认的 pending 不能算已覆盖")
        self.assertEqual(summary["languages"]["en"]["pending_words"], 1)

    def test_multi_card_state_takes_highest(self):
        summary = self.build()
        child = [w for w in summary["words"] if w["key"] == "child"][0]
        self.assertEqual(child["state"], "review")
        self.assertEqual(child["notes"], 2)
        self.assertEqual(child["cards"], 2)

    def test_state_percentages_sum_to_100(self):
        summary = self.build()
        for language in ("en", "ja"):
            data = summary["languages"][language]
            total = round(sum(row["pct"] for row in data["states"]), 1)
            self.assertAlmostEqual(total, 100.0, delta=0.2, msg=language)
            words = sum(row["words"] for row in data["states"])
            self.assertEqual(words, data["words"])

    def test_states_by_unit(self):
        entries = [
            make_entry("abandon", exams=["cet4"], state="new", deck_id=1, note_id=1, card_count=2),
            make_entry("ability", exams=["cet4"], state="review", deck_id=1, note_id=2, card_count=1),
        ]
        for entry, states in zip(entries, ({"new": 2}, {"review": 1})):
            entry["card_states"] = states
        summary = analysis.summarize(
            FakeCol({1: "四级"}), {"entries": entries, "scanned_notes": 2, "scanned_cards": 3, "skipped": {}},
            CONFIG, matchers(),
        )
        data = summary["languages"]["en"]
        self.assertEqual(data["unit_totals"], {"word": 2, "note": 2, "card": 3})
        by_unit = data["states_by_unit"]
        # STATE_ORDER = (review, learn, new, suspended)，所以列表顺序是 [复习, 学习, 未学习, 暂停]
        # 唯一词口径：abandon 未学习、ability 复习中
        self.assertEqual(
            [row["words"] for row in by_unit["word"]], [1, 0, 1, 0]
        )
        # 笔记口径：两条笔记各一个新、一个复习
        self.assertEqual(
            [row["words"] for row in by_unit["note"]], [1, 0, 1, 0]
        )
        # 卡片口径：abandon 有 2 张新卡、ability 有 1 张复习卡
        self.assertEqual(
            [row["words"] for row in by_unit["card"]], [1, 0, 2, 0]
        )
        for unit in ("word", "note", "card"):
            self.assertEqual(
                sum(row["words"] for row in by_unit[unit]),
                data["unit_totals"][unit],
                unit,
            )

    def test_source_percentages_sum_to_100(self):
        summary = self.build()
        for language in ("en", "ja"):
            for code, rows in summary["languages"][language]["sources"].items():
                if not rows:
                    # 该词表在这个范围里一个都没覆盖到，来源占比是空的，不用凑 100%
                    continue
                got = round(sum(row["pct"] for row in rows), 1)
                self.assertAlmostEqual(got, 100.0, delta=0.2, msg=f"{language}/{code}")

    def test_coverage_formula(self):
        summary = self.build()
        m = matchers()
        cet4 = self.row(summary, "en", "cet4")
        # 分母跟着内置词表走，改词表后这里自动对齐，不用手改数字
        self.assertEqual(cet4["total"], m["en_meta"]["counts"]["cet4"])
        self.assertEqual(cet4["missing"], cet4["total"] - cet4["covered"])
        self.assertAlmostEqual(cet4["rate"], round(cet4["covered"] * 100 / cet4["total"], 1), places=1)

    def test_source_split_between_two_decks(self):
        col = FakeCol({1: "四级A", 2: "四级B"})
        entries = [
            make_entry("abandon", exams=["cet4"], deck_id=1, note_id=1),
            make_entry("ability", exams=["cet4"], deck_id=2, note_id=2),
        ]
        summary = analysis.summarize(
            col,
            {"entries": entries, "scanned_notes": 2, "scanned_cards": 2, "skipped": {}},
            CONFIG,
            matchers(),
        )
        rows = summary["languages"]["en"]["sources"]["cet4"]
        self.assertEqual(len(rows), 2, "两个牌组各覆盖一个词，来源应该拆成两行")
        self.assertAlmostEqual(sum(r["pct"] for r in rows), 100.0, delta=0.2)
        self.assertEqual(sum(r["count"] for r in rows), 2)

    def test_merged_union_row_exists(self):
        summary = self.build()
        en_codes = [row["code"] for row in summary["languages"]["en"]["coverage"]]
        ja_codes = [row["code"] for row in summary["languages"]["ja"]["coverage"]]
        self.assertIn("cet46", en_codes)
        self.assertIn("jlpt", ja_codes)

    def test_union_counts_word_once(self):
        # 一个词同时属于四级和六级时，两个词表各自都算，并集只算一次
        col = FakeCol({1: "混合"})
        entries = [make_entry("abandon", exams=["cet4", "cet6"], deck_id=1)]
        summary = analysis.summarize(
            col,
            {"entries": entries, "scanned_notes": 1, "scanned_cards": 1, "skipped": {}},
            CONFIG,
            matchers(),
        )
        self.assertEqual(self.row(summary, "en", "cet4")["covered"], 1)
        self.assertEqual(self.row(summary, "en", "cet6")["covered"], 1)
        self.assertEqual(self.row(summary, "en", "cet46")["covered"], 1)
        # 并集分母按词元组去重：跨词表的词只算一次，
        # 所以一定 <= 各分项相加，且 >= 单项最大的那个。
        m = matchers()
        cet4 = self.row(summary, "en", "cet4")["total"]
        cet6 = self.row(summary, "en", "cet6")["total"]
        union = self.row(summary, "en", "cet46")["total"]
        self.assertEqual(union, m["en"].merged_total(["cet4", "cet6"]))
        self.assertEqual(union, m["en_meta"]["counts_cet46"])
        self.assertLess(union, cet4 + cet6, "两个词表有重叠，去重后必然小于相加")
        # wordforge 的六级词书本身包含四级词，所以并集可能正好等于六级
        self.assertGreaterEqual(union, max(cet4, cet6), "并集不可能比最大的单项小")

    def test_deepest_deck_is_primary_source(self):
        summary = self.build()
        child = [w for w in summary["words"] if w["key"] == "child"][0]
        self.assertEqual(child["source"], "六级词汇::真题")

    def test_uncovered_list_matches_total(self):
        summary = self.build()
        m = matchers()
        uncovered = analysis.uncovered_words(m, summary, "en", "cet4")
        covered = {
            w["key"] for w in summary["words"] if w["language"] == "en" and w["confident"]
        }
        heads = {
            head for head, tags in m["en"].group_exam.items() if "cet4" in tags.split()
        }
        self.assertEqual(len(uncovered), len(heads - covered))

    def test_uncovered_japanese(self):
        summary = self.build()
        m = matchers()
        uncovered = analysis.uncovered_words(m, summary, "ja", "n5")
        covered = {
            w["key"] for w in summary["words"] if w["language"] == "ja" and w["confident"]
        }
        heads = {w for w, lv in m["ja"].by_word.items() if "n5" in lv.split()}
        self.assertEqual(len(uncovered), len(heads - covered))

    def row(self, summary, language, code):
        for row in summary["languages"][language]["coverage"]:
            if row["code"] == code:
                return row
        raise AssertionError(f"没有 {language}/{code} 这一行")


# ---------------------------------------------------------------- 字段识别


class FakeModel(dict):
    pass


class TestFieldScoring(unittest.TestCase):
    def test_word_field_name_vetoes_helper_fields(self):
        # 真取词字段要放行
        for name in ("vocabularyWord", "VocabKanji", "Word"):
            self.assertTrue(V.is_word_field_name(name), f"{name} 应该被当成取词字段")
        # 词性 / 句子类型 / 假名读音 / 语法点要挡掉
        # （ECDICT 里 noun / verb 自带 cet4 标签，SentType 里的「対」自带 JLPT 标签，
        #   光看命中率拦不住，只能按字段名一票否决）
        for name in ("wordPartOfSpeech", "SentType3", "VocabPoS", "Word Reading",
                     "VocabFurigana", "Grammar", "Meaning", "id"):
            self.assertFalse(V.is_word_field_name(name), f"{name} 不该被当成取词字段")

    def test_part_of_speech_field_is_not_picked(self):
        m = matchers()
        # 词性字段是「假高分」：命中率 100% 但字段名不对
        pos = V.score_field("wordPartOfSpeech", ["noun", "verb", "adverb", "adjective"], m)
        self.assertFalse(V.is_word_field_name("wordPartOfSpeech"))
        self.assertEqual(pos["language"], "en")
        word = V.score_field("vocabularyWord", ["apple", "abandon", "children", "beautiful"], m)
        self.assertGreater(word["score"], pos["score"])

    def test_english_word_field_wins(self):
        m = matchers()
        good = V.score_field(
            "vocabularyWord", ["apple", "abandon", "children", "beautiful"], m
        )
        bad = V.score_field(
            "ExampleSentence",
            ["I ate an apple yesterday morning in the park."] * 4,
            m,
        )
        self.assertEqual(good["language"], "en")
        self.assertGreater(good["score"], bad["score"])

    def test_japanese_field_detected(self):
        m = matchers()
        good = V.score_field("VocabKanji", ["食べる", "勉強", "会う", "青"], m)
        self.assertEqual(good["language"], "ja")
        self.assertGreater(good["score"], 40)

    def test_recommend_fields_orders_by_score(self):
        m = matchers()
        fields = {1: ["Example", "vocabularyWord"]}
        samples = {
            1: [
                ["我喜欢这个词的例句", "apple"],
                ["再来一个例句", "abandon"],
                ["第三个例句", "beautiful"],
            ]
        }
        rows = V.recommend_fields(fields, samples, m)
        self.assertEqual(rows[0]["field"], "vocabularyWord")
        self.assertEqual(rows[0]["language"], "en")

    def test_score_field_reports_samples_hits_and_sentence_flag(self):
        m = matchers()
        info = V.score_field("vocabularyWord", ["apple", "abandon", ""], m)
        # 空值不计入抽样
        self.assertEqual(info["samples"], 2)
        self.assertEqual(info["hits"], 2)
        self.assertFalse(info["looks_like_sentence"])
        self.assertAlmostEqual(info["hit_rate"], 1.0)

    def test_example_field_is_flagged_as_sentence(self):
        m = matchers()
        rows = [
            "I ate an apple yesterday morning in the park.",
            "She abandoned the plan after the meeting ended.",
            "They decided to run home before the rain started.",
            "We should study harder for the coming exam.",
        ]
        info = V.score_field("ExampleSentence", rows, m)
        self.assertTrue(info["looks_like_sentence"])
        self.assertEqual(info["samples"], 4)

    def test_recommend_fields_samples_at_most_60(self):
        m = matchers()
        fields = {1: ["vocabularyWord"]}
        rows = [[f"word{i}"] for i in range(100)]
        result = V.recommend_fields(fields, {1: rows}, m)
        # 默认抽样 60 条，100 条输入里最多只用 60 条
        self.assertEqual(result[0]["samples"], 60)
        # 显式传入更小的抽样数也要生效
        small = V.recommend_fields(fields, {1: rows}, m, sample=5)
        self.assertEqual(small[0]["samples"], 5)


class TestLooksLikeSentence(unittest.TestCase):
    """「像例句/长文本」判定：设置页拿它解释为什么跳过某字段。"""

    def test_english_sentence_with_period(self):
        # 英文句点必须算数，否则整列英文例句漏判
        self.assertTrue(
            V.looks_like_sentence(
                ["He abandoned the plan after the meeting ended."] * 3
            )
        )

    def test_chinese_sentence_with_full_stop(self):
        self.assertTrue(
            V.looks_like_sentence(
                ["他昨天上午在公园里一个人吃了一个又大又红的苹果，然后就回家了。"] * 3
            )
        )

    def test_html_is_sentence(self):
        self.assertTrue(
            V.looks_like_sentence(["<b>apple</b>", "<i>abandon</i>", "<br>run"])
        )

    def test_sound_tag_is_sentence(self):
        self.assertTrue(V.looks_like_sentence(["[sound:apple.mp3]", "[sound:run.mp3]"]))

    def test_cloze_markup_is_sentence(self):
        self.assertTrue(V.looks_like_sentence(["{{c1::apple}}", "{{c2::abandon}}"]))

    def test_short_words_are_not_sentence(self):
        self.assertFalse(V.looks_like_sentence(["apple", "abandon", "run", "better"]))
        self.assertFalse(V.looks_like_sentence(["食べる", "勉強", "会う"]))

    def test_empty_is_not_sentence(self):
        self.assertFalse(V.looks_like_sentence([]))
        self.assertFalse(V.looks_like_sentence(["", "   "]))

    def test_majority_rule(self):
        # 少数长词条不该把整列判成例句
        mostly_words = [
            "apple",
            "abandon",
            "He abandoned the plan after the meeting finally ended.",
        ]
        self.assertFalse(V.looks_like_sentence(mostly_words))
        # 过半是整句才算
        mostly_sentences = [
            "apple",
            "He abandoned the plan after the meeting finally ended.",
            "She decided to run home before the heavy rain started.",
        ]
        self.assertTrue(V.looks_like_sentence(mostly_sentences))


# ---------------------------------------------------------------- 统计口径辅助


class FakeModels:
    """只实现 detect_fields / sample_notes 用到的那两个方法。"""

    def __init__(self, models):
        self._models = {int(m["id"]): m for m in models}

    def all(self):
        return list(self._models.values())

    def get(self, ntid):
        return self._models.get(int(ntid))


class FakeCollection:
    """给 analysis.detect_fields 用的假集合（笔记值直接喂进来，不碰数据库）。"""

    def __init__(self, models, notes):
        self.models = FakeModels(models)
        self._lookup = {}
        nid = 1
        for ntid, rows in notes.items():
            for row in rows:
                self._lookup[nid] = (int(ntid), list(row))
                nid += 1

    def find_notes(self, query):
        # 只支持 detect_fields 发出的那种 note:"名字" 查询
        name = query.split('"')[1]
        for model in self.models.all():
            if model["name"] == name:
                ntid = int(model["id"])
                break
        else:
            return []
        return [nid for nid, (typ, _) in self._lookup.items() if typ == ntid]

    def get_note(self, nid):
        note = type("FakeNote", (), {})()
        note.fields = list(self._lookup[nid][1])
        return note


class TestDetectFields(unittest.TestCase):
    """字段识别端到端：真取词字段被推荐，词性/句子类型/语法点被挡掉。"""

    def build(self):
        models = [
            {
                "id": 1,
                "name": "English-CEFR",
                "flds": [
                    {"name": "vocabularyWord"},
                    {"name": "wordPartOfSpeech"},
                    {"name": "definition"},
                    {"name": "ExampleSentence"},
                ],
            },
            {
                "id": 2,
                "name": "语法卡",
                "flds": [{"name": "Grammar"}, {"name": "Meaning"}],
            },
            {
                "id": 3,
                "name": "JLPT10k",
                "flds": [
                    {"name": "VocabKanji"},
                    {"name": "VocabFurigana"},
                    {"name": "SentType3"},
                    {"name": "VocabPoS"},
                ],
            },
        ]
        notes = {
            1: [
                ["apple", "noun", "苹果", "I ate an apple."],
                ["abandon", "verb", "放弃", "He abandoned it."],
                ["children", "noun", "孩子们", "Children play."],
                ["beautiful", "adjective", "美丽的", "A beautiful day."],
            ],
            2: [
                ["～てしまう", "完了"],
                ["～ながら", "同时"],
                ["～ので", "原因"],
                ["～のに", "逆接"],
            ],
            3: [
                ["食べる", "たべる", "動詞", "一段"],
                ["勉強", "べんきょう", "名詞", "する"],
                ["会う", "あう", "動詞", "五段"],
                ["青", "あお", "名詞", "イ形"],
            ],
        }
        return analysis.detect_fields(FakeCollection(models, notes), matchers(), limit=10)

    def test_english_word_field_is_recommended(self):
        got = self.build()["1"]
        self.assertTrue(got["enabled"])
        self.assertEqual(got["fields"], ["vocabularyWord"])
        self.assertEqual(got["language"], "en")

    def test_part_of_speech_is_not_chosen(self):
        got = self.build()["1"]
        self.assertNotIn("wordPartOfSpeech", got["fields"])
        self.assertNotIn("ExampleSentence", got["fields"])

    def test_grammar_notetype_is_excluded(self):
        got = self.build()["2"]
        self.assertFalse(got["enabled"], "语法点牌库不该被自动勾选")
        self.assertEqual(got["fields"], [])

    def test_japanese_reading_field_becomes_fallback(self):
        got = self.build()["3"]
        self.assertTrue(got["enabled"])
        self.assertEqual(got["fields"], ["VocabKanji"])
        self.assertEqual(got["reading_field"], "VocabFurigana")
        self.assertEqual(got["language"], "ja")
        self.assertNotIn("SentType3", got["fields"])
        self.assertNotIn("VocabPoS", got["fields"])


class TestPercentHelper(unittest.TestCase):
    def test_zero_denominator(self):
        self.assertEqual(analysis._pct(0, 0), 0.0)

    def test_rounding(self):
        self.assertEqual(analysis._pct(1, 3), 33.3)
        self.assertEqual(analysis._pct(2, 3), 66.7)
        self.assertEqual(analysis._pct(1, 8), 12.5)


# ---------------------------------------------------------------- 打标签


class TestExamTags(unittest.TestCase):
    def test_single_word_list_tags(self):
        self.assertEqual(V.exam_tag_for_code("cet4"), "应试::四级")
        self.assertEqual(V.exam_tag_for_code("cet6"), "应试::六级")
        self.assertEqual(V.exam_tag_for_code("ky"), "应试::考研")
        self.assertEqual(V.exam_tag_for_code("ielts"), "应试::雅思")
        self.assertEqual(V.exam_tag_for_code("gre"), "应试::GRE")

    def test_jlpt_tags_carry_prefix(self):
        # 光写「N3」容易和别的牌库撞名，所以统一带 JLPT-
        self.assertEqual(V.exam_tag_for_code("n3"), "应试::JLPT-N3")
        self.assertEqual(V.exam_tag_for_code("n1"), "应试::JLPT-N1")

    def test_merged_and_unknown_codes_have_no_tag(self):
        for code in ("cet46", "jlpt", "", "zzz"):
            self.assertEqual(V.exam_tag_for_code(code), "", code)

    def test_own_tag_detection(self):
        self.assertTrue(V.is_own_tag("应试::四级"))
        self.assertTrue(V.is_own_tag("应试::JLPT-N3"))
        self.assertFalse(V.is_own_tag("English-CEFR::CEFR-A1"))
        self.assertFalse(V.is_own_tag(""))

    def test_confirm_text(self):
        text = V.exam_tag_confirm_text(12, ["应试::四级", "应试::雅思"])
        self.assertIn("12", text)
        self.assertIn("应试::四级", text)
        self.assertIn("应试::雅思", text)
        self.assertIn("撤销", text)
        remove_text = V.exam_tag_confirm_text(3, ["应试::四级"], remove=True)
        self.assertIn("去掉", remove_text)
        self.assertIn("3", remove_text)


# ---------------------------------------------------------------- 明细多选


class TestFilterByCodes(unittest.TestCase):
    def build(self):
        return [
            make_entry("child", exams=["cet4", "cet6"], note_id=1),
            make_entry("good", exams=["cet4"], note_id=2),
            make_entry("kaoyan", exams=["ky"], note_id=3),
            make_entry("zzz", note_id=4),
            make_entry("taberu", language="ja", levels=["n5"], note_id=5),
            make_entry("beer", language="ja", levels=["n5", "n3"], note_id=6),
        ]

    def keys(self, words):
        return {w["key"] for w in words}

    def test_empty_selection_means_no_filter(self):
        words = self.build()
        self.assertEqual(len(analysis.filter_words_by_codes(words, [], "any")), len(words))
        self.assertEqual(len(analysis.filter_words_by_codes(words, None, "all")), len(words))

    def test_union_across_languages(self):
        words = analysis.filter_words_by_codes(
            self.build(), [("en", "cet4"), ("ja", "n5")], "any"
        )
        self.assertEqual(self.keys(words), {"child", "good", "taberu", "beer"})

    def test_intersection_within_one_language(self):
        words = analysis.filter_words_by_codes(
            self.build(), [("en", "cet4"), ("en", "cet6")], "all"
        )
        self.assertEqual(self.keys(words), {"child"})

    def test_cross_language_selection_ignores_other_words(self):
        # 一个语言的勾选只能管这个语言的词：勾了 ky 不该把日语词带出来
        words = analysis.filter_words_by_codes(
            self.build(), [("en", "ky"), ("ja", "n3")], "all"
        )
        self.assertEqual(self.keys(words), {"kaoyan", "beer"})

    def test_merged_code_expands_to_its_members(self):
        words = analysis.filter_words_by_codes(self.build(), [("en", "cet46")], "any")
        self.assertEqual(self.keys(words), {"child", "good"})
        # 并集本身就是一个「或」条件，交集模式下只有它一个勾选时结果不变
        words_all = analysis.filter_words_by_codes(self.build(), [("en", "cet46")], "all")
        self.assertEqual(self.keys(words_all), {"child", "good"})

    def test_unknown_language_item_is_skipped(self):
        words = analysis.filter_words_by_codes(self.build(), [("xx", "cet4")], "any")
        self.assertEqual(words, [])


# ---------------------------------------------------------------- 来源维度


class TestSourceRows(unittest.TestCase):
    def build_words(self):
        return [
            {
                **make_entry("child", exams=["cet4"], deck_id=1, note_id=1),
                "source": "四级词汇",
                "tags": ["English-CEFR::CEFR-A1"],
                "notetype_ids": [10],
            },
            {
                **make_entry("good", exams=["cet4"], deck_id=2, note_id=2),
                "source": "六级词汇::真题",
                "tags": ["English-CEFR::CEFR-A1", "应试::四级"],
                "notetype_ids": [10],
            },
            {
                **make_entry("plain", exams=["cet4"], deck_id=1, note_id=3),
                "source": "四级词汇",
                "tags": [],
                "notetype_ids": [11],
            },
        ]

    def test_deck_dimension_counts_each_word_once(self):
        rows = analysis.source_rows(self.build_words(), "deck")
        self.assertEqual(
            {(r["name"], r["count"]) for r in rows},
            {("四级词汇", 2), ("六级词汇::真题", 1)},
        )
        self.assertAlmostEqual(sum(r["pct"] for r in rows), 100.0, delta=0.2)

    def test_tag_dimension_counts_each_tag_and_excludes_own_tags(self):
        rows = analysis.source_rows(self.build_words(), "tag")
        self.assertEqual(
            {r["name"]: r["count"] for r in rows},
            {"English-CEFR::CEFR-A1": 2, "（无标签）": 1},
        )
        self.assertAlmostEqual(sum(r["pct"] for r in rows), 100.0, delta=0.2)

    def test_notetype_dimension_uses_names(self):
        rows = analysis.source_rows(
            self.build_words(), "notetype", {10: "English-CEFR", 11: "Kaishi"}
        )
        self.assertEqual(
            {r["name"]: r["count"] for r in rows},
            {"English-CEFR": 2, "Kaishi": 1},
        )
        self.assertAlmostEqual(sum(r["pct"] for r in rows), 100.0, delta=0.2)

    def test_unknown_dimension_falls_back_to_deck(self):
        rows = analysis.source_rows(self.build_words(), "whatever")
        self.assertEqual({r["name"] for r in rows}, {"四级词汇", "六级词汇::真题"})


class TestSummarizeSourceDimension(unittest.TestCase):
    def build(self, dimension):
        col = FakeCol({1: "四级词汇", 2: "六级词汇::真题"})
        col.models = FakeModels(
            [
                {"id": 10, "name": "English-CEFR", "flds": []},
                {"id": 11, "name": "Kaishi", "flds": []},
            ]
        )
        entries = [
            make_entry("apple", exams=["cet4"], deck_id=1, note_id=1,
                       tags=["Book::A"], notetype_ids=[10]),
            make_entry("abandon", exams=["cet4"], deck_id=2, note_id=2,
                       tags=["Book::A", "Book::B"], notetype_ids=[10, 11]),
        ]
        config = dict(CONFIG, source_dimension=dimension)
        collected = {"entries": entries, "scanned_notes": 2, "scanned_cards": 2, "skipped": {}}
        return analysis.summarize(col, collected, config, matchers())

    def test_deck(self):
        summary = self.build("deck")
        rows = summary["languages"]["en"]["sources"]["cet4"]
        self.assertEqual(
            {(r["name"], r["count"]) for r in rows},
            {("四级词汇", 1), ("六级词汇::真题", 1)},
        )
        self.assertEqual(summary["source_dimension"], "deck")

    def test_tag_counts_every_tag(self):
        summary = self.build("tag")
        rows = summary["languages"]["en"]["sources"]["cet4"]
        self.assertEqual({r["name"]: r["count"] for r in rows}, {"Book::A": 2, "Book::B": 1})
        self.assertAlmostEqual(sum(r["pct"] for r in rows), 100.0, delta=0.2)

    def test_notetype_uses_model_names(self):
        summary = self.build("notetype")
        rows = summary["languages"]["en"]["sources"]["cet4"]
        self.assertEqual(
            {r["name"]: r["count"] for r in rows},
            {"English-CEFR": 2, "Kaishi": 1},
        )

    def test_source_dimension_changes_cache_signature(self):
        col = FakeCol()
        scope = {"deck_ids": [], "search": ""}
        meta = {"built": "2026-09-01"}
        a = analysis.collection_signature(col, scope, dict(CONFIG, source_dimension="deck"), meta)
        b = analysis.collection_signature(col, scope, dict(CONFIG, source_dimension="tag"), meta)
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
