"""应试词汇统计插件 · v0.3.1 补漏牌组自动清理的纯逻辑测试（不需要 Anki）。

盯四件事：

1. 建卡登记表：范围快照的存取、同名牌组二次生成时的取舍、牌组被删后的摘除；
2. 删除规则：全 ``type=0`` 才可删、任意一张进过学习就保留、暂停/埋葬的新卡可删；
3. 覆盖判定：被**任何一张具体词表**认出来就算已覆盖（含变形还原）；
4. 日志与文件读写：条数上限、状态文案、坏文件不崩。

跑法（在项目根目录执行）：
    python -m unittest discover -s 源码\\tests -p "test_*.py" -v
"""

from __future__ import annotations

import ast
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)
PROJECT = os.path.dirname(SRC)
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import analysis as A  # noqa: E402
import cleanup as CL  # noqa: E402
import vocab_logic as V  # noqa: E402

DATA = os.path.join(SRC, "data")


def module_constants(path: str) -> dict:
    """只解析模块级字面量常量（不执行那个脚本）。"""
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    out: dict = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                try:
                    out[target.id] = ast.literal_eval(node.value)
                except ValueError:
                    continue
    return out


def load_matchers() -> dict:
    exam = V.load_json_gz(os.path.join(DATA, "exam_index.json.gz"))
    lemma = V.load_json_gz(os.path.join(DATA, "lemma_index.json.gz"))
    jlpt = V.load_json_gz(os.path.join(DATA, "jlpt_index.json.gz"))
    return {
        "en": V.EnglishMatcher(exam, lemma, merge_lemma=True),
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


def word_row(key, language="en", exams=(), levels=(), confident=True, note_id=1):
    """造一条 summarize() 风格的词条行（只放判定要用的字段）。"""
    return {
        "key": key,
        "language": language,
        "exams": list(exams),
        "levels": list(levels),
        "confident": confident,
        "surface": key,
        "surfaces": [key],
        "alternatives": [],
    }


class FakeDecks:
    def __init__(self, names):
        self._names = dict(names)

    def all_names_and_ids(self):
        class Deck:
            def __init__(self, did, name):
                self.id = did
                self.name = name

        return [Deck(did, name) for did, name in self._names.items()]

    def name(self, did):
        return self._names.get(did, str(did))


class FakeCol:
    def __init__(self, names=None):
        self.decks = FakeDecks(names or {})


# ---------------------------------------------------------------- 范围快照


class TestScopeSnapshot(unittest.TestCase):
    def test_default_scope_snapshots_to_empty(self):
        scope = {
            "deck_ids": [],
            "include_subdecks": True,
            "tags_include": [],
            "tags_exclude": [],
            "tag_mode": "any",
            "notetype_ids": [],
            "states": list(V.STATE_ORDER),
            "search": "",
        }
        self.assertEqual(CL.scope_snapshot(scope), {})
        self.assertTrue(CL.scope_is_empty({}))
        self.assertTrue(CL.scope_is_empty({"tags_include": [], "search": "  "}))

    def test_snapshot_keeps_only_the_real_limits(self):
        snap = CL.scope_snapshot(
            {
                "deck_ids": [12, 3],
                "include_subdecks": False,
                "tags_include": ["English-CEFR"],
                "tags_exclude": ["太难"],
                "tag_mode": "all",
                "notetype_ids": [5],
                "states": ["new", "review"],
                "search": " added:7 ",
            }
        )
        self.assertEqual(snap["deck_ids"], [3, 12])
        self.assertEqual(snap["include_subdecks"], False)
        self.assertEqual(snap["tags_include"], ["English-CEFR"])
        self.assertEqual(snap["tags_exclude"], ["太难"])
        self.assertEqual(snap["tag_mode"], "all")
        self.assertEqual(snap["notetype_ids"], [5])
        self.assertEqual(snap["states"], ["new", "review"])
        self.assertEqual(snap["search"], "added:7")
        self.assertFalse(CL.scope_is_empty(snap))

    def test_scope_from_snapshot_empty_means_whole_collection(self):
        scope = CL.scope_from_snapshot({})
        self.assertEqual(scope["deck_ids"], [])
        self.assertEqual(scope["tags_include"], [])
        self.assertEqual(scope["states"], list(V.STATE_ORDER))
        self.assertTrue(scope["include_subdecks"])

    def test_scope_from_snapshot_roundtrips(self):
        original = {
            "deck_ids": [7],
            "include_subdecks": False,
            "tags_include": ["A::B"],
            "tags_exclude": [],
            "tag_mode": "any",
            "notetype_ids": [3],
            "states": ["new"],
            "search": "is:new",
        }
        scope = CL.scope_from_snapshot(CL.scope_snapshot(original))
        self.assertEqual(scope["deck_ids"], [7])
        self.assertFalse(scope["include_subdecks"])
        self.assertEqual(scope["tags_include"], ["A::B"])
        self.assertEqual(scope["notetype_ids"], [3])
        self.assertEqual(scope["states"], ["new"])
        self.assertEqual(scope["search"], "is:new")

    def test_scope_from_snapshot_uses_fallback_when_empty(self):
        scope = CL.scope_from_snapshot({}, fallback={"deck_ids": [1, 2]})
        self.assertEqual(scope["deck_ids"], [1, 2])


# ---------------------------------------------------------------- 登记表


class TestRegistry(unittest.TestCase):
    def test_register_new_deck_records_scope_and_words(self):
        registry = CL.register_deck(
            CL.empty_registry(),
            "应试补漏::四级",
            code="cet4",
            language="en",
            scope={"deck_ids": [1]},
            words=["abandon", "ability"],
            count=2,
            now=1000.0,
        )
        entry = registry["decks"]["应试补漏::四级"]
        self.assertEqual(entry["code"], "cet4")
        self.assertEqual(entry["scope"], {"deck_ids": [1]})
        self.assertEqual(entry["words"], ["abandon", "ability"])
        self.assertEqual(entry["created_notes"], 2)
        self.assertEqual(entry["created_at"], 1000.0)

    def test_second_build_keeps_the_first_scope(self):
        registry = CL.empty_registry()
        registry = CL.register_deck(
            registry, "d", scope={"deck_ids": [1]}, words=["a"], count=1, now=1.0
        )
        registry = CL.register_deck(
            registry, "d", scope={"tags_include": ["x"]}, words=["b"], count=1, now=2.0
        )
        entry = registry["decks"]["d"]
        # 牌组里旧卡的判定口径是第一次建卡时的范围，不能被后来的设置冲掉
        self.assertEqual(entry["scope"], {"deck_ids": [1]})
        self.assertEqual(entry["scope_latest"], {"tags_include": ["x"]})
        self.assertEqual(entry["words"], ["a", "b"])
        self.assertEqual(entry["created_notes"], 2)

    def test_register_without_scope_keeps_old_snapshot(self):
        registry = CL.register_deck(CL.empty_registry(), "d", scope={"deck_ids": [1]})
        registry = CL.register_deck(registry, "d", scope=None)
        self.assertEqual(registry["decks"]["d"]["scope"], {"deck_ids": [1]})

    def test_prune_registry_drops_deleted_decks(self):
        registry = CL.register_deck(CL.empty_registry(), "keep")
        registry = CL.register_deck(registry, "gone")
        registry, dropped = CL.prune_registry(registry, ["keep", "别的牌组"])
        self.assertEqual(dropped, ["gone"])
        self.assertEqual(sorted(registry["decks"]), ["keep"])

    def test_registered_decks_skips_junk(self):
        self.assertEqual(CL.registered_decks({"decks": {"a": {}, "b": 3, 4: {}}}), {"a": {}})
        self.assertEqual(CL.registered_decks(None), {})


# ---------------------------------------------------------------- 删除规则


class TestDeletable(unittest.TestCase):
    def test_all_new_cards_are_deletable(self):
        self.assertTrue(CL.note_deletable([0]))
        self.assertTrue(CL.note_deletable([0, 0]))

    def test_suspended_or_buried_new_cards_are_still_new(self):
        # 暂停/埋葬只改 queue，type 仍是 0：按未学习处理，可以删
        self.assertTrue(CL.note_deletable([0]))
        self.assertTrue(CL.note_deletable(["0", 0]))

    def test_any_started_card_keeps_the_note(self):
        for started in (1, 2, 3):
            self.assertFalse(CL.note_deletable([0, started]))
            self.assertFalse(CL.note_deletable([started]))

    def test_no_cards_means_not_deletable(self):
        self.assertFalse(CL.note_deletable([]))
        self.assertFalse(CL.note_deletable(None))


# ---------------------------------------------------------------- 覆盖判定


class TestCoverage(unittest.TestCase):
    def setUp(self):
        self.covered = CL.coverage_from_summary(
            {
                "words": [
                    word_row("abandon", exams=["cet4"]),
                    word_row("analyse", exams=["cet6"]),
                    word_row("child", exams=["cet4", "cet6"]),
                    word_row("食べる", language="ja", levels=["n5"]),
                    word_row("unknown", exams=[]),  # 没命中任何词表，不算覆盖
                    word_row("ambiguous", confident=False, exams=["cet4"]),
                ]
            }
        )
        self.resolver = CL.make_resolver(matchers())

    def test_any_wordlist_counts(self):
        self.assertIn("abandon", self.covered["keys"]["en"])
        self.assertTrue(CL.is_covered("en", "abandon", covered=self.covered, resolver=self.resolver))

    def test_variant_spelling_counts(self):
        # 卡片里写 analyse，统计那边归到 analyze；写法不同也要认出来
        self.assertTrue(CL.is_covered("en", "analyze", covered=self.covered, resolver=self.resolver))

    def test_inflected_form_counts_through_lemmatisation(self):
        self.assertTrue(CL.is_covered("en", "children", covered=self.covered, resolver=self.resolver))

    def test_japanese_kana_and_kanji_both_count(self):
        self.assertTrue(CL.is_covered("ja", "たべる", covered=self.covered, resolver=self.resolver))
        self.assertTrue(CL.is_covered("ja", "食べる", covered=self.covered, resolver=self.resolver))

    def test_cross_language_does_not_leak(self):
        self.assertFalse(CL.is_covered("en", "たべる", covered=self.covered, resolver=self.resolver))
        self.assertFalse(CL.is_covered("ja", "abandon", covered=self.covered, resolver=self.resolver))

    def test_not_covered_words_stay(self):
        self.assertFalse(CL.is_covered("en", "zzzznotaword", covered=self.covered, resolver=self.resolver))

    def test_ambiguous_row_is_not_counted_as_covered(self):
        self.assertNotIn("ambiguous", self.covered["keys"]["en"])

    def test_missing_matchers_only_matches_by_spelling(self):
        resolver = CL.make_resolver(None)
        # 写法直接命中（含大小写/连字符归一）不需要匹配器，仍算已覆盖。
        self.assertTrue(CL.is_covered("en", "abandon", covered=self.covered, resolver=resolver))
        # 变形词只能靠匹配器还原：没有匹配器时宁可留着，不删。
        self.assertFalse(CL.is_covered("en", "children", covered=self.covered, resolver=resolver))

    def test_matcher_error_does_not_delete(self):
        class Boom:
            def resolve(self, *args):
                raise RuntimeError("匹配炸了")

        resolver = CL.make_resolver({"en": Boom()})
        self.assertFalse(CL.is_covered("en", "children", covered=self.covered, resolver=resolver))


# ---------------------------------------------------------------- 判定总览


class TestPlanCleanup(unittest.TestCase):
    def setUp(self):
        self.covered = CL.coverage_from_summary(
            {"words": [word_row("abandon", exams=["cet4"]), word_row("child", exams=["cet4"])]}
        )
        self.resolver = CL.make_resolver(matchers())

    def candidates(self):
        return [
            {"nid": 1, "word": "abandon", "language": "en", "deck": "应试补漏::四级", "card_types": [0]},
            {"nid": 2, "word": "children", "language": "en", "deck": "应试补漏::四级", "card_types": [0]},
            {"nid": 3, "word": "abandon", "language": "en", "deck": "应试补漏::四级", "card_types": [2]},
            {"nid": 4, "word": "zzzzghost", "language": "en", "deck": "应试补漏::四级", "card_types": [0]},
            {"nid": 5, "word": "", "language": "en", "deck": "应试补漏::四级", "card_types": [0]},
            {"nid": 6, "word": "abandon", "language": "en", "deck": "应试补漏::四级", "card_types": []},
        ]

    def test_plan_separates_remove_from_keep(self):
        plan = CL.plan_cleanup(self.candidates(), self.covered, self.resolver)
        self.assertEqual(plan["remove"], [1, 2])
        reasons = {row["nid"]: row["why"] for row in plan["keep"]}
        self.assertEqual(reasons[3], CL.KEEP_STARTED)
        self.assertEqual(reasons[4], CL.KEEP_UNCOVERED)
        self.assertEqual(reasons[5], CL.KEEP_NO_WORD)
        self.assertEqual(reasons[6], CL.KEEP_NO_CARD)
        self.assertEqual(plan["counts"][CL.KEEP_UNCOVERED], 1)
        self.assertEqual(plan["candidates"], 6)
        self.assertEqual(plan["decks"], ["应试补漏::四级"])

    def test_plan_without_coverage_removes_nothing(self):
        plan = CL.plan_cleanup(self.candidates(), CL.empty_coverage(), self.resolver)
        self.assertEqual(plan["remove"], [])
        self.assertEqual(plan["counts"][CL.KEEP_UNCOVERED], 3)

    def test_plan_ignores_bad_nids(self):
        plan = CL.plan_cleanup(
            [{"nid": None, "word": "abandon", "card_types": [0]}],
            self.covered,
            self.resolver,
        )
        self.assertEqual(plan["remove"], [])
        self.assertEqual(plan["candidates"], 0)


# ---------------------------------------------------------------- 日志


class TestLog(unittest.TestCase):
    def test_append_log_keeps_newest_first_and_caps_length(self):
        log = {}
        for index in range(CL.LOG_KEEP + 5):
            log = CL.append_log(log, {"at": index, "removed": index})
        self.assertEqual(len(log["runs"]), CL.LOG_KEEP)
        self.assertEqual(log["runs"][0]["removed"], CL.LOG_KEEP + 4)
        self.assertEqual(log["last"]["removed"], CL.LOG_KEEP + 4)

    def test_status_text_covers_all_shapes(self):
        self.assertIn("还没跑过", CL.cleanup_status_text({}))
        self.assertIn("没有卡片", CL.cleanup_status_text({"last": {"at": time.time(), "candidates": 0}}))
        self.assertIn(
            "没有需要清理的卡片",
            CL.cleanup_status_text({"last": {"at": time.time(), "candidates": 10, "removed": 0, "kept": 10}}),
        )
        text = CL.cleanup_status_text(
            {"last": {"at": time.time(), "candidates": 10, "removed": 2, "kept": 8}}
        )
        self.assertIn("删掉 2 张", text)
        self.assertIn("保留 8 张", text)
        self.assertIn("出错", CL.cleanup_status_text({"last": {"at": time.time(), "error": "boom"}}))

    def test_format_time_handles_junk(self):
        self.assertIn("未知", CL.format_time(None))
        self.assertIn("未知", CL.format_time("不是时间"))
        self.assertEqual(len(CL.format_time(1700000000)), 16)


# ---------------------------------------------------------------- 文件读写


class TestFiles(unittest.TestCase):
    def test_paths_live_in_the_user_dir(self):
        self.assertEqual(os.path.basename(CL.registry_path("X")), CL.REGISTRY_NAME)
        self.assertEqual(os.path.basename(CL.log_path("X")), CL.LOG_NAME)

    def test_write_then_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = CL.registry_path(folder)
            payload = CL.register_deck(CL.empty_registry(), "d", scope={"deck_ids": [1]})
            self.assertTrue(CL.write_json(path, payload))
            self.assertEqual(CL.read_json(path)["decks"]["d"]["scope"], {"deck_ids": [1]})

    def test_corrupt_or_missing_file_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = os.path.join(folder, "nope.json")
            self.assertEqual(CL.read_json(missing), {})
            broken = os.path.join(folder, "broken.json")
            with open(broken, "w", encoding="utf-8") as handle:
                handle.write("{不是 json")
            self.assertEqual(CL.read_json(broken), {})
            listed = os.path.join(folder, "list.json")
            with open(listed, "w", encoding="utf-8") as handle:
                handle.write("[1, 2]")
            self.assertEqual(CL.read_json(listed), {})


# ---------------------------------------------------------------- 范围 SQL


class TestScopeSql(unittest.TestCase):
    def test_deck_ids_exclude_becomes_not_in(self):
        class Decks:
            def deck_and_child_ids(self, did):
                return {1: [1, 11], 2: [2]}[did]

        class Col:
            decks = Decks()

        where, params = A.nodeck_scope(
            {"deck_ids": [], "deck_ids_exclude": [1, 2]}, Col()
        )
        self.assertIn("not in", where)
        self.assertEqual(sorted(params), [1, 2, 11])

    def test_deck_ids_exclude_absent_changes_nothing(self):
        class Col:
            class decks:  # noqa: N801
                @staticmethod
                def deck_and_child_ids(did):
                    return [did]

        where, params = A.nodeck_scope({"deck_ids": []}, Col())
        self.assertEqual(where, "")
        self.assertEqual(params, [])


# ---------------------------------------------------------------- 打包清单


class TestSourceGuards(unittest.TestCase):
    """不 import Anki 也能钉住的源码级断言，防同一类 bug 回归。"""

    def setUp(self):
        with open(os.path.join(SRC, "__init__.py"), "r", encoding="utf-8") as handle:
            self.source = handle.read()

    def test_cleanup_merges_removed_words(self):
        # 聚合多个牌组结果时必须把 removed_words 也并起来，否则日志/探针拿不到删了哪些词。
        self.assertIn('plan["removed_words"].extend(', self.source)

    def test_cleanup_fills_missing_field_map(self):
        # 启动清理可能早于用户打开统计窗，必须自己补一次字段映射。
        self.assertIn("ensure_field_map(col, config, matchers)", self.source)

    def test_cleanup_hook_is_guarded(self):
        self.assertIn("hook.append(HG.guard(", self.source)

    def test_deck_names_are_normalised_before_compare(self):
        # 集合库里的牌组名用 \x1f 分层（应试补漏\x1f四级），接口层通常给 ::。
        # 只读脚本 / 某些版本会给原始写法，比较之前必须先归一，否则清理会误判。
        self.assertIn('replace("\\x1f", "::")', self.source)
        # 至少三处要用到归一函数：挑候选牌组、挑候选笔记的牌组名、摘登记表
        self.assertGreaterEqual(self.source.count("deck_name_text("), 4)


class TestSkipReasons(unittest.TestCase):
    """确认框要说清「跳过了哪几类词」，不能笼统一句带过。"""

    def test_plan_text_lists_reason_kinds(self):
        import builder as B

        drafts = [
            {
                "deck": "应试补漏::四级",
                "key": "abandon",
                "audio_path": "a.mp3",
                "audio_ready": True,
            }
        ]
        skipped = [
            {"key": "children", "reason": A.GAP_UNRECOGNIZED},
            {"key": "ability", "reason": "超过本次上限 500 个"},
        ]
        text = B.plan_text(drafts, skipped)
        self.assertIn("识别失败 1 个", text)
        self.assertIn("超过本次上限 500 个 1 个", text)


class TestPackaging(unittest.TestCase):
    def setUp(self):
        self.installer = module_constants(os.path.join(PROJECT, "安装到Anki.py"))
        self.probe = module_constants(os.path.join(PROJECT, "工具", "run_anki_probe.py"))

    def test_cleanup_is_packaged(self):
        self.assertIn("cleanup.py", self.installer.get("ADDON_FILES") or [])
        self.assertIn("cleanup.py", self.probe.get("ADDON_FILES") or [])
        self.assertTrue(os.path.isfile(os.path.join(SRC, "cleanup.py")))

    def test_two_lists_still_agree(self):
        self.assertEqual(
            sorted(self.installer.get("ADDON_FILES") or []),
            sorted(self.probe.get("ADDON_FILES") or []),
            "两份打包清单必须一致，否则探针验的不是真装的那份",
        )

    def test_test_v07_in_source_zip(self):
        self.assertIn("源码/tests/test_v07.py", self.installer.get("SOURCE_EXTRA") or [])

    def test_version_matches_code(self):
        version = read_version()
        self.assertEqual(version, "0.3.1")
        with open(os.path.join(SRC, "__init__.py"), "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn(f'__version__ = "{version}"', source)


def read_version() -> str:
    with open(os.path.join(SRC, "version.txt"), "r", encoding="utf-8") as handle:
        return handle.read().strip()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
