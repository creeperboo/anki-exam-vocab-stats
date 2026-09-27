"""开发用：只读统计「一键补漏制卡」会跳过哪些词、为什么跳过。

这个脚本把真实集合复制到临时目录再算，不碰原文件，也不写任何牌组/笔记。
它模拟界面里点「一键生成补漏牌组」的完整流程：

    未覆盖词 → builder.build_drafts（素材齐全才进队列）→ split_by_audio

然后按「跳过原因」分组，打印每类的数量和具体词。

用法（PowerShell）：

    $py = "C:\\Program Files\\Lenovo\\ModelMgr\\Plugins\\Image\\python.exe"
    & $py "工具\\补漏跳过清单.py"                    # 全部词表，默认上限 500
    & $py "工具\\补漏跳过清单.py" --builder-limit 5000
    & $py "工具\\补漏跳过清单.py" --codes cet4,ky
    & $py "工具\\补漏跳过清单.py" --sample 40         # 每类最多列几个词
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "源码")
sys.path.insert(0, SRC)

import analysis  # noqa: E402
import builder as B  # noqa: E402
import resources as RES  # noqa: E402
import vocab_logic as V  # noqa: E402


def _load_probe():
    """把「真实集合抽查.py」当模块加载，借它的只读集合适配器。"""
    path = os.path.join(HERE, "真实集合抽查.py")
    spec = importlib.util.spec_from_file_location("real_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _all_codes() -> list:
    """能拿来建牌组的具体词表码（合并码展开成成员）。"""
    out = []
    for language, codes in (("en", V.ENGLISH_EXAM_CODES), ("ja", V.JLPT_CODES)):
        for code in codes:
            members = analysis.list_members(code, language)
            if len(members) > 1:
                for member in sorted(members):
                    label = (
                        V.JLPT_LABELS.get(member, member)
                        if language == "ja"
                        else V.ALL_EXAM_LABELS.get(member, member)
                    )
                    item = (language, member, label)
                    if item not in out:
                        out.append(item)
                continue
            label = (
                V.JLPT_LABELS.get(code, code)
                if language == "ja"
                else V.ALL_EXAM_LABELS.get(code, code)
            )
            item = (language, code, label)
            if item not in out:
                out.append(item)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default=None)
    parser.add_argument("--collection", default=None)
    parser.add_argument("--limit", type=int, default=0, help="只统计前 N 条笔记（0=全部）")
    parser.add_argument("--builder-limit", type=int, default=500)
    parser.add_argument("--codes", default="", help="逗号分隔的词表码，默认全部")
    parser.add_argument("--sample", type=int, default=25, help="每类最多打印几个词")
    parser.add_argument(
        "--out", default=os.path.join(HERE, "补漏跳过清单.csv"),
        help="把被跳过的词逐条写进这个 CSV（UTF-8 BOM，Excel 直接能开）",
    )
    args = parser.parse_args()

    probe = _load_probe()
    source = args.collection or probe.find_collection(args.profile)
    workdir, copied = probe.copy_collection(source)
    print(f"集合来源：{source}")
    print(f"已复制到临时目录：{workdir}")
    con = sqlite3.connect(copied)
    con.execute("pragma wal_checkpoint(truncate)")
    con.execute("pragma query_only=on")
    col = probe.ReadOnlyCollection(con)

    try:
        matchers = probe.load_matchers()
        detected = analysis.detect_fields(col, matchers)
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
        from collections import defaultdict

        by_note_type = defaultdict(list)
        for ntid, value in field_map.items():
            by_note_type[tuple(value.get("fields") or [])].append(ntid)
        print(f"取词字段：{len(field_map)} 个笔记类型参与统计")

        deck_ids = [d.id for d in col.decks.all_names_and_ids()]
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
        collected = probe.apply_limit(
            analysis.collect_entries(col, scope, field_map, matchers), args.limit
        )
        summary = analysis.summarize(col, collected, config, matchers)
        print(f"扫了 {summary['scanned_notes']} 条笔记 / {summary['scanned_cards']} 张卡")

        targets = _all_codes()
        if args.codes:
            wanted = {c.strip() for c in args.codes.split(",") if c.strip()}
            targets = [t for t in targets if t[1] in wanted]

        # 和界面一样：把勾选的词表展开成词条（每行带自己的词表码）
        rows = []
        per_code_total = {}
        per_code_missing = {}
        for language, code, label in targets:
            found = analysis.uncovered_words(matchers, summary, language, code, limit=100000)
            per_code_total[f"{language}:{code}"] = len(found)
            per_code_missing[f"{language}:{code}"] = sum(
                1 for r in found if r.get("reason") == analysis.GAP_MISSING
            )
            for row in found:
                rows.append(
                    {
                        "key": row["key"],
                        "surface": row.get("surface") or row["key"],
                        "language": language,
                        "code": code,
                        "label": label,
                        "meaning": row.get("meaning") or "",
                        "reason": row.get("reason") or analysis.GAP_MISSING,
                    }
                )

        print("\n【每个词表的未覆盖词数（识别失败/待确认 单独标出）】")
        reason_tally = {}
        for language, code, label in targets:
            key = f"{language}:{code}"
            total = per_code_total[key]
            missing = per_code_missing[key]
            found = analysis.uncovered_words(matchers, summary, language, code, limit=100000)
            for row in found:
                reason_tally[row["reason"]] = reason_tally.get(row["reason"], 0) + 1
            print(
                f"  {label:<10} 未覆盖 {total:>6}"
                + (f"（其中真未覆盖 {missing}）" if missing != total else "")
            )
        if reason_tally:
            print(
                "  合计："
                + "　｜　".join(f"{k} {v}" for k, v in sorted(reason_tally.items()))
            )

        cache_dir = _cache_dir(args.profile)
        res = RES.LocalResources({"language": "en"}, cache_dir=cache_dir or None)
        installed = res.media_installed()
        print(
            f"\n音频包：{'已装 ' + str(installed['files']) + ' 个 mp3' if installed.get('installed') else '未装（这会触发「音频文件在本机缺失」那一类）'}"
            f"　目录：{installed.get('dir')}"
        )

        drafts, skipped = B.build_drafts(
            rows, res, {"limit": int(args.builder_limit or B.DEFAULT_LIMIT)}
        )
        ready, not_ready = B.split_by_audio(drafts)

        print(f"\n喂进去 {len(rows)} 个未覆盖词　→　能出草稿 {len(drafts)} 张　跳过 {len(skipped)} 个")
        if installed.get("installed"):
            print(f"  草稿里有音频的 {len(ready)} 张；缺音频文件的 {len(not_ready)} 张")
        deck_tally = {}
        for draft in drafts:
            deck_tally[draft.get("deck") or "?"] = deck_tally.get(draft.get("deck") or "?", 0) + 1
        if deck_tally:
            print(
                "  这次能出卡的词表分布："
                + "、".join(f"{k} {v}" for k, v in sorted(deck_tally.items()))
            )
        tally = {}
        for row in skipped:
            reason = row.get("reason") or "?"
            tally.setdefault(reason, []).append(row.get("key") or "")
        print("\n【跳过的种类】")
        for reason, words in sorted(tally.items(), key=lambda kv: -len(kv[1])):
            if len(words) <= 40:
                sample = "、".join(w for w in words if w)
            else:
                sample = (
                    "、".join(w for w in words[: args.sample] if w)
                    + f"…（共 {len(words)} 个，完整清单见 CSV）"
                )
            print(f"\n  ◆ {reason} —— {len(words)} 个")
            if sample:
                print(f"     例：{sample}")

        if args.out:
            with open(args.out, "w", encoding="utf-8-sig", newline="") as handle:
                import csv

                writer = csv.writer(handle)
                writer.writerow(["单词", "语言", "词表", "结果", "原因"])
                for draft in drafts:
                    writer.writerow(
                        [draft["key"], draft["language"], draft["code"], "出卡", ""]
                    )
                where = {}
                for row in rows:
                    where.setdefault(
                        (row["language"], row["key"]), row["code"]
                    )
                    where.setdefault(
                        (row["language"], row["surface"]), row["code"]
                    )
                for row in skipped:
                    key = row.get("key") or ""
                    language = "en" if key.isascii() else "ja"
                    writer.writerow(
                        [key, language, where.get((language, key), ""), "跳过", row.get("reason") or ""]
                    )
            print(f"\n逐条清单已写出：{args.out}")
        return 0
    finally:
        try:
            con.close()
        except Exception:
            pass
        shutil.rmtree(workdir, ignore_errors=True)


def _cache_dir(profile: str | None) -> str:
    """插件用户目录：%APPDATA%\\Anki2\\<配置>\\exam_vocab_stats。"""
    base = os.path.join(os.environ.get("APPDATA", ""), "Anki2")
    if not os.path.isdir(base):
        return ""
    if profile:
        return os.path.join(base, profile, "exam_vocab_stats")
    candidates = [
        name
        for name in os.listdir(base)
        if os.path.isdir(os.path.join(base, name))
        and not name.startswith(".")
        and name not in ("addons21",)
    ]
    if not candidates:
        return ""
    newest = max(
        candidates, key=lambda n: os.path.getmtime(os.path.join(base, n))
    )
    return os.path.join(base, newest, "exam_vocab_stats")


if __name__ == "__main__":
    raise SystemExit(main())
