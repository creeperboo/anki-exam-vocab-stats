"""开发用：在**真实集合的临时副本**上预演一次「补漏牌组自动清理」。

只读：先把 collection.anki2 复制到临时目录，在副本上算，算完删掉临时目录。
不碰原文件，也不写任何笔记/牌组，纯粹回答「这次打开 Anki 会自动删掉哪些补漏卡」。

判定口径和插件里一模一样（复用 源码/cleanup.py）：

- 候选 = 笔记类型「应试补漏卡」的笔记，加上「应试补漏::*」牌组里的笔记；
- 「已覆盖」＝按建卡时的范围重算，被**任何一张具体词表**认出来就算（合并码不参与）；
- 判定永久排除补漏牌组自己，否则补漏卡会把自己算成已覆盖而瞬间全删；
- 只有全部卡片都是 ``type == 0``（从未进入学习）才可删，任一张学过就永久保留。

用法（PowerShell）：

    $py = "C:\\Program Files\\Lenovo\\ModelMgr\\Plugins\\Image\\python.exe"
    & $py "工具\\补漏清理预演.py"                 # 自动取最近修改的用户配置
    & $py "工具\\补漏清理预演.py" --profile creeperboo
    & $py "工具\\补漏清理预演.py" --sample 40      # 每类最多列几个词
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "源码")
sys.path.insert(0, SRC)

import analysis  # noqa: E402
import cleanup as CL  # noqa: E402
import vocab_logic as V  # noqa: E402

CARD_NOTETYPE_NAME = "应试补漏卡"
DECK_PREFIX = "应试补漏"

_JA_CHAR_RE = re.compile(r"[\u3041-\u309f\u30a0-\u30ff\u4e00-\u9fff\u3400-\u4dbf]")


def _load_probe():
    """把「真实集合抽查.py」当模块加载，借它的只读集合适配器。"""
    path = os.path.join(HERE, "真实集合抽查.py")
    spec = importlib.util.spec_from_file_location("real_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def guess_language(word: str) -> str:
    return "ja" if _JA_CHAR_RE.search(word or "") else "en"


def deck_name_text(name) -> str:
    """集合库里牌组名用 ``\\x1f`` 分层（``应试补漏\\x1f四级``），统一成 ``::``。"""
    return str(name or "").replace("\x1f", "::")


def builder_deck_ids(col) -> list[int]:
    out: list[int] = []
    for deck in col.decks.all_names_and_ids():
        name = deck_name_text(getattr(deck, "name", ""))
        if name == DECK_PREFIX or name.startswith(DECK_PREFIX + "::"):
            out.append(int(deck.id))
    return out


def builder_notetype_ids(col) -> list[int]:
    return [int(m["id"]) for m in col.models.all() if m.get("name") == CARD_NOTETYPE_NAME]


def collect_candidates(col, deck_names) -> list[dict]:
    nts = builder_notetype_ids(col)
    dids = builder_deck_ids(col)
    if not nts and not dids:
        return []
    clauses, params = [], []
    if nts:
        clauses.append("n.mid in (%s)" % ",".join("?" * len(nts)))
        params.extend(nts)
    if dids:
        clauses.append("c.did in (%s)" % ",".join("?" * len(dids)))
        params.extend(dids)
    rows = col.db.all(
        "select n.id, n.flds, c.type, c.did from notes n join cards c on c.nid = n.id where "
        + " or ".join(clauses),
        *params,
    )
    notes: dict = {}
    for nid, flds, ctype, did in rows:
        note = notes.get(nid)
        if note is None:
            fields = (flds or "").split("\x1f")
            note = {
                "nid": int(nid),
                "word": V.clean_field(fields[0]) if fields else "",
                "reading": V.clean_field(fields[1]) if len(fields) > 1 else "",
                "decks": set(),
                "card_types": [],
            }
            notes[nid] = note
        note["decks"].add(int(did))
        note["card_types"].append(int(ctype or 0))
    out = []
    for note in notes.values():
        names = sorted(deck_name_text(deck_names.get(d, str(d))) for d in note["decks"])
        deck_name = next(
            (n for n in names if n == DECK_PREFIX or n.startswith(DECK_PREFIX + "::")),
            names[0] if names else "",
        )
        language = guess_language(note["word"])
        out.append(
            {
                "nid": note["nid"],
                "word": note["word"],
                "reading": note["reading"] if language == "ja" else "",
                "language": language,
                "deck": deck_name,
                "card_types": note["card_types"],
            }
        )
    return out


def addon_config_candidates(explicit: str | None) -> list[str]:
    """一份「插件实际用的配置」候选路径（Anki 把用户改动写在 meta.json 里）。"""
    if explicit:
        return [explicit]
    appdata = os.environ.get("APPDATA") or ""
    addon = os.path.join(appdata, "Anki2", "addons21", "exam_vocab_stats")
    return [
        os.path.join(addon, "meta.json"),
        os.path.join(addon, "config.json"),
    ]


def load_saved_config(explicit: str | None) -> tuple[dict, str]:
    """读插件当前的配置（默认值 + 你的改动），读不到就返回空配置。"""
    defaults: dict = {}
    defaults_path = os.path.join(SRC, "config.json")
    try:
        with open(defaults_path, "r", encoding="utf-8") as handle:
            defaults = json.load(handle) or {}
    except (OSError, ValueError):
        defaults = {}
    for path in addon_config_candidates(explicit):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle) or {}
        except (OSError, ValueError):
            continue
        if os.path.basename(path) == "meta.json":
            data = data.get("config") or {}
        if not isinstance(data, dict) or not data:
            continue
        merged = dict(defaults)
        merged.update(data)
        return merged, path
    return defaults, ""


def resolve_field_map(col, matchers, saved: dict, include_disabled: bool) -> dict:
    """取插件实际会用的字段映射；配置里没有就自动识别一次。"""
    stored = saved.get("field_map") or {}
    if not stored:
        return {int(k): v for k, v in analysis.detect_fields(col, matchers).items()}
    out: dict = {}
    for key, value in stored.items():
        if not value or not value.get("fields"):
            continue
        if value.get("language") == "none":
            continue
        if not include_disabled and value.get("enabled") is False:
            continue
        out[int(key)] = value
    return out


def explain_words(con, deck_names, matchers, summary, covered, exclude, words) -> None:
    """逐个词说明「为什么删 / 为什么不删」（只读，纯诊断）。"""
    resolver = CL.make_resolver(matchers)
    print("—— 逐词诊断 ——")
    for word in words:
        language = guess_language(word)
        forms = analysis.surface_forms(language, word)
        form_hits = sorted(forms & (covered["forms"].get(language) or set()))
        keys = covered["keys"].get(language) or set()
        key, alternatives = resolver(language, word, "")
        alt_hits = sorted({a for a in alternatives if a in keys} | ({key} & keys if key else set()))
        lists = []
        for code in V.ENGLISH_EXAM_CODES:
            if language != "en":
                break
            member = analysis.word_list_keys(matchers, summary, "en", code)
            if word in member or (forms & member):
                lists.append(code)
        for code in V.JLPT_CODES:
            if language != "ja":
                break
            member = analysis.word_list_keys(matchers, summary, "ja", code)
            if word in member or (forms & member):
                lists.append(code)
        verdict = CL.is_covered(language, word, "", covered, resolver)
        print(f"· {word}（{language}）→ {'已覆盖' if verdict else '未覆盖'}")
        print(f"    在词表里：{'、'.join(lists) if lists else '（不在任何词表）'}")
        print(f"    范围里的写法命中：{form_hits or '（无）'}")
        print(f"    词元命中：{alt_hits or '（无）'}")
        rows = con.execute(
            "select c.nid, c.did, c.type, c.queue, n.mid from cards c"
            " join notes n on n.id = c.nid where n.flds like ? limit 8",
            ("%" + word + "%",),
        ).fetchall()
        if not rows:
            print("    集合里含这个词的卡：一张也没有")
        for nid, did, ctype, queue, mid in rows:
            mark = "（补漏牌组，判定时排除）" if int(did) in exclude else ""
            print(
                f"    卡：笔记 {nid}｜牌组 {deck_names.get(int(did), did)}"
                f"｜type={ctype} queue={queue}｜笔记类型 {mid}{mark}"
            )
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="只读预演补漏牌组自动清理")
    parser.add_argument("--profile", default=None, help="用户配置名（默认取最近改动的那个）")
    parser.add_argument("--collection", default=None, help="直接指定 collection.anki2 路径")
    parser.add_argument("--sample", type=int, default=20, help="每类最多列几个词")
    parser.add_argument("--explain", nargs="*", default=None, help="逐个词说明为什么删/不删")
    parser.add_argument("--config", default=None, help="插件配置文件（默认读已安装那份的 meta.json）")
    parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="连设置页里没勾的字段也一起算（对照用：勾上 Lapis 之类会多覆盖一些词）",
    )
    args = parser.parse_args()

    real = _load_probe()
    source = args.collection or real.find_collection(args.profile)
    workdir, copy_path = real.copy_collection(source)
    print(f"集合副本：{copy_path}\n（原文件只读，跑完删临时目录）\n")

    import sqlite3

    con = sqlite3.connect(copy_path)
    try:
        col = real.ReadOnlyCollection(con)
        matchers = real.load_matchers()
        saved, config_path = load_saved_config(args.config)
        field_map = resolve_field_map(col, matchers, saved, args.include_disabled)
        print(
            f"配置来源：{config_path or '（没找到已安装的配置，用自动识别）'}"
            f"｜启用字段的笔记类型 {len(field_map)} 个"
            f"{'（含设置页没勾的）' if args.include_disabled else ''}\n"
        )

        deck_names = {int(d.id): deck_name_text(d.name) for d in col.decks.all_names_and_ids()}
        candidates = collect_candidates(col, deck_names)
        print(f"候选补漏笔记：{len(candidates)} 条")
        if not candidates:
            return 0

        exclude = builder_deck_ids(col)
        scope = {"deck_ids": [], "deck_ids_exclude": exclude}
        collected = analysis.collect_entries(col, scope, field_map, matchers)
        summary = analysis.summarize(col, collected, {"source_dimension": "deck"}, matchers)
        covered = CL.coverage_from_summary(summary)
        plan = CL.plan_cleanup(candidates, covered, CL.make_resolver(matchers))

        if args.explain:
            explain_words(
                con, deck_names, matchers, summary, covered, exclude, args.explain
            )

        if os.environ.get("EVS_DRYRUN_DEBUG"):
            where, params = analysis.nodeck_scope(scope, col)
            in_deck = col.db.scalar(
                "select count() from cards where did in (%s)" % ",".join("?" * len(exclude)),
                *exclude,
            )
            print(
                "[debug] where：",
                where,
                "｜params：",
                params,
                "｜补漏牌组里的卡：",
                in_deck,
            )
            print(
                "[debug] 扫描笔记/卡片：",
                collected.get("scanned_notes"),
                collected.get("scanned_cards"),
                "｜范围内唯一词：",
                len(summary.get("words") or ()),
                "｜en keys/forms：",
                len(covered["keys"]["en"]),
                len(covered["forms"]["en"]),
                "｜ja keys/forms：",
                len(covered["keys"]["ja"]),
                len(covered["forms"]["ja"]),
                "｜field_map：",
                sorted(field_map),
                "｜排除牌组：",
                exclude,
            )

        counts = plan["counts"]
        print(
            f"会被删除：{len(plan['remove'])} 条"
            f"（已覆盖且从未学习）\n"
            f"会被保留：{len(plan['keep'])} 条"
            f"（已开始学习 {counts.get(CL.KEEP_STARTED, 0)}"
            f" / 仍未覆盖 {counts.get(CL.KEEP_UNCOVERED, 0)}"
            f" / 没词 {counts.get(CL.KEEP_NO_WORD, 0)}"
            f" / 没卡 {counts.get(CL.KEEP_NO_CARD, 0)}）\n"
        )
        if plan["removed_words"]:
            words = sorted(set(plan["removed_words"]))
            print(f"要删的词（{len(words)} 个）：{'、'.join(words[: args.sample])}")
            print()
        untouched = {
            row["word"]
            for row in plan["keep"]
            if row["why"] == CL.KEEP_UNCOVERED and row["word"]
        }
        if untouched:
            sample = sorted(untouched)[: args.sample]
            print(f"仍未覆盖、继续保留（示例 {len(sample)} 个）：{'、'.join(sample)}")
    finally:
        con.close()
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
