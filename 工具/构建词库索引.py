# -*- coding: utf-8 -*-
"""构建插件自带的词库索引（v0.3：wordforge 六本词书 ∪ ECDICT；eggrolls ∪ OpenJLPT ∪ tanos）。

用法（在项目根目录里执行）：
    python 工具\\构建词库索引.py            # 全量重建
    python 工具\\构建词库索引.py --only-lite  # 只重建 ecdict_lite（改词表集合后用）

产物：
    源码\\data\\exam_index.json.gz     英语词表（按词元组去重）
    源码\\data\\lemma_index.json.gz    英语词形 -> 词元（多认领时给候选表）
    源码\\data\\ecdict_lite.json.gz    补漏制卡用的音标/释义轻量索引
    源码\\data\\jlpt_index.json.gz     日语 JLPT 词表（N5-N1）
    工具\\索引构建记录.md              人看的构建报告

原则：构建期与运行期共用 vocab_logic 里的同一套归一规则，绝不各写一份。
"""

from __future__ import annotations

import csv
import datetime
import gzip
import hashlib
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "源码")
RAW = os.path.join(HERE, "下载")
DATA = os.path.join(SRC, "data")

if SRC not in sys.path:
    sys.path.insert(0, SRC)

import vocab_logic as V  # noqa: E402


EXAM_LABELS = {
    "cet4": "四级",
    "cet6": "六级",
    "ky": "考研",
    "ielts": "雅思",
    "toefl": "托福",
    "gre": "GRE",
}
OPTIONAL_LABELS = {"zk": "中考", "gk": "高考"}

# ECDICT tag 列里直接用这些代码；wordforge 的词书 id 要映射过来
ECDICT_TAGS = tuple(OPTIONAL_LABELS) + tuple(EXAM_LABELS)
BOOK_TO_EXAM = {
    "cet4": "cet4",
    "cet6": "cet6",
    "kaoyan": "ky",
    "ielts": "ielts",
    "toefl": "toefl",
    "gre": "gre",
    "gaokao": "gk",
    "zhongkao": "zk",
}

JLPT_LABELS = {"n5": "N5", "n4": "N4", "n3": "N3", "n2": "N2", "n1": "N1"}
JLPT_ORDER = ("n1", "n2", "n3", "n4", "n5")  # 由高到低，报告里用

# 词书里把词性顺手写进词条的情况：``turning n``、``could modal``
POS_TAGS = {
    "n", "v", "vt", "vi", "adj", "adv", "prep", "conj", "pron", "num",
    "art", "int", "modal", "aux", "pl", "abbr",
}


# ------------------------------------------------------------------ 工具


def now() -> str:
    return datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_gz_json(path: str, payload) -> int:
    """确定性 gzip（mtime=0），同样的输入每次产出同样的字节，方便比对。"""
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with gzip.GzipFile(path, "wb", compresslevel=9, mtime=0) as handle:
        handle.write(blob)
    return len(blob)


def eff_freq(frq, bnc) -> int:
    """词频（越小越常用）。ECDICT 的 frq 列没有时退回 bnc 排名。"""
    for value in (frq, bnc):
        try:
            number = int(str(value).strip() or 0)
        except (TypeError, ValueError):
            number = 0
        if number > 0:
            return number
    return 0


def _join_tags(tags) -> str:
    return " ".join(sorted(t for t in tags if t))


def _exams_from_exchange(exchange: str) -> str:
    """ECDICT 的 exchange 列里 ``0:`` 是词元（lemma）。"""
    for piece in (exchange or "").split("/"):
        if piece.startswith("0:"):
            return piece[2:].strip().lower()
    return ""


def _split_multi(value: str) -> list[str]:
    """并列写法拆成多个写法。

    tanos 用 ``キロ; キログラム``，OpenJLPT 用 ``キロ/キログラム``，还有用
    ``・``/``、`` 的。以前只拆 ``;``，导致 ``キロ/キログラム`` 被归一成
    ``きろきろぐらむ`` 这种假词混进词表，分母虚高、素材也补不上。
    """
    out = []
    for piece in re.split(r"[;；/／・、，,]", value or ""):
        piece = piece.strip()
        if piece:
            out.append(piece)
    return out


# ------------------------------------------------------------------ 英语


def read_ecdict(path: str) -> dict:
    rows: dict[str, dict] = {}
    with io.open(path, encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            word = (row.get("word") or "").strip().lower()
            if word and word not in rows:
                rows[word] = row
    return rows


def read_wordforge(path: str) -> list:
    with io.open(path, encoding="utf-8") as handle:
        return json.load(handle)


def read_lemma_groups(path: str):
    """lemma.en.txt 一行是 ``head/词频 -> form1,form2,…``。"""
    groups: list[tuple[str, int, list[str]]] = []
    form_to_heads: dict[str, list[str]] = {}
    with io.open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith(";"):
                continue
            left, _, right = line.partition("->")
            left = left.strip()
            if not left:
                continue
            head, _, weight = left.partition("/")
            head = head.strip().lower()
            if not head:
                continue
            try:
                rank = int(weight.strip() or 0)
            except ValueError:
                rank = 0
            forms = [f.strip().lower() for f in right.split(",") if f.strip()]
            groups.append((head, rank, forms))
            for form in forms:
                form_to_heads.setdefault(form, []).append(head)
    return groups, form_to_heads


def build_english(raw_dir: str, data_dir: str) -> dict:
    ecdict_path = os.path.join(raw_dir, "ecdict.csv")
    lemma_path = os.path.join(raw_dir, "lemma.en.txt")
    wf_path = os.path.join(raw_dir, "wordforge_words.json")

    ecdict = read_ecdict(ecdict_path)
    wf_rows = read_wordforge(wf_path)
    wf_words: set[str] = set()
    for entry in wf_rows:
        if isinstance(entry, list) and entry:
            normalized = V.normalize_english(str(entry[0] or ""))
            if normalized:
                wf_words.add(normalized)
    # 真实词表：ECDICT 收录的词 + wordforge 的词。
    # ECDICT 的 exchange 列和 lemma.en.txt 里都夹着截断词干（armored -> "armore"、
    # lasting -> "long-last"、breakwater -> "breakwate"）。这些「词干」不是真词，
    # 一旦当上词元组组长，分母里就会混进假词、补漏清单里多出一堆做不出卡的条目。
    # 所以只认「ECDICT 或 wordforge 里真的当过词条」的写法。
    real_words = set(ecdict) | wf_words

    # -- 1) 词 -> 考试集合：ECDICT 的 tag 列 ∪ wordforge 的六本词书
    word_exam: dict[str, set] = {}
    for word, row in ecdict.items():
        tags = {t for t in (row.get("tag") or "").split() if t in ECDICT_TAGS}
        if tags:
            word_exam[word] = set(tags)

    wf_books: dict[str, int] = {}
    for entry in wf_rows:
        if not isinstance(entry, list) or not entry:
            continue
        word = V.normalize_english(str(entry[0] or ""))
        if not word:
            continue
        books = str(entry[8] or "").split("|") if len(entry) > 8 else []
        for book in books:
            book = book.strip()
            if book in BOOK_TO_EXAM:
                wf_books[book] = wf_books.get(book, 0) + 1
        exams = {BOOK_TO_EXAM[b.strip()] for b in books if b.strip() in BOOK_TO_EXAM}
        if exams:
            word_exam.setdefault(word, set()).update(exams)

    # -- 2) 词元组：lemma.en.txt 优先，ECDICT 的 exchange 0: 补漏
    lemma_groups, lemma_forms = read_lemma_groups(lemma_path)
    # lemma.en.txt 的词元只有当它本身也是真词条时才算数（见上面的 real_words 注释）
    claims: dict[str, list[str]] = {}
    head_rank: dict[str, int] = {}
    for head, rank, forms in lemma_groups:
        head_rank[head] = max(head_rank.get(head, 0), rank)
        for form in forms:
            claims.setdefault(form, [])
            if head not in claims[form]:
                claims[form].append(head)

    for word, row in ecdict.items():
        head = _exams_from_exchange(row.get("exchange") or "")
        if head and head != word and head in real_words:
            claims.setdefault(word, [])
            if head not in claims[word]:
                claims[word].append(head)
            head_rank.setdefault(head, 0)

    # exchange 里指到假词干的认领一律丢掉，让这个词自己单独立组
    claims = {
        form: [h for h in heads if h in real_words]
        for form, heads in claims.items()
    }
    claims = {form: heads for form, heads in claims.items() if heads}

    def _rank(head: str) -> int:
        # 词频排名越小越「正统」，同一词形被多个词元认领时优先归给更常用的那个
        return head_rank.get(head, 0)

    member_head: dict[str, str] = {}
    for form, heads in claims.items():
        if form in head_rank and head_rank[form] > 0:
            # 自己就是词元时不再往上并（child 不该被并进别的词）
            continue
        ordered = sorted(set(heads), key=lambda h: (-_rank(h), h))
        if ordered:
            member_head[form] = ordered[0]

    # -- 3) 词元组 -> 标签并集
    groups: dict[str, set] = {}
    for head, _rank_value, forms in lemma_groups:
        if head not in real_words:
            continue
        groups.setdefault(head, set()).add(head)
        groups[head].update(forms)
    for form, head in member_head.items():
        groups.setdefault(head, set()).add(head)
        groups[head].add(form)
    # 有考试标签但没进任何词元组的词，各自算一个单独的词元组
    for word in word_exam:
        if word not in member_head and word not in groups:
            groups[word] = {word}

    # -- 3b) 清掉词书里的「词形表」噪音
    # 高考/四六级词书里混着 "arise arose arisen"、"foot feet"、"turning n"、
    # "a can opener" 这种把变形或词性直接写进词条的写法。它们不是单词，做成卡也没意义，
    # 留着只会让分母虚高、让补漏清单里出现一堆做不出的条目。
    def _resolve(token: str) -> str:
        if token in member_head:
            return member_head[token]
        heads = [h for h in claims.get(token, []) if h in real_words]
        if heads:
            return sorted(set(heads), key=lambda h: (-_rank(h), h))[0]
        return token

    def _is_noise(head: str) -> bool:
        if not head or "." in head:
            return True
        tokens = head.split()
        if len(tokens) < 2:
            return False
        if tokens[0] in ("a", "an", "the"):
            return True
        if tokens[-1] in POS_TAGS:
            return True
        if len({_resolve(t) for t in tokens}) == 1:
            return True
        return False

    noise = {w for w in (set(word_exam) | set(groups)) if _is_noise(w)}
    for word in noise:
        word_exam.pop(word, None)
        groups.pop(word, None)

    group_exam: dict[str, str] = {}
    for head, forms in groups.items():
        tags: set = set()
        for form in forms:
            tags |= word_exam.get(form, set())
        if tags:
            group_exam[head] = _join_tags(tags)

    # -- 4) 分母与词频
    counts = {code: 0 for code in ECDICT_TAGS}
    for tags in group_exam.values():
        for code in tags.split():
            counts[code] = counts.get(code, 0) + 1
    counts_surface = {code: 0 for code in ECDICT_TAGS}
    for tags in word_exam.values():
        for code in tags:
            counts_surface[code] = counts_surface.get(code, 0) + 1
    counts_cet46 = sum(
        1 for tags in group_exam.values() if {"cet4", "cet6"} & set(tags.split())
    )

    freq: dict[str, int] = {}
    for head, forms in groups.items():
        for form in forms:
            row = ecdict.get(form)
            if row is None:
                continue
            value = eff_freq(row.get("frq"), row.get("bnc"))
            if value > 0:
                freq[form] = value
            else:
                freq.setdefault(form, 0)

    lemma_map: dict[str, object] = {}
    multi_claim = 0
    for form, heads in claims.items():
        ordered = [h for h in sorted(set(heads), key=lambda h: (-_rank(h), h)) if h != form]
        if not ordered:
            continue
        if len(ordered) == 1:
            lemma_map[form] = ordered[0]
        else:
            lemma_map[form] = ordered
            multi_claim += 1

    meta = {
        "built": now(),
        "source": "wordforge（六本词书）∪ skywind3000/ECDICT",
        "source_url": "https://github.com/skywind3000/ECDICT",
        "license": "MIT（ECDICT）／wordforge 词书数据",
        "files": {
            "ecdict.csv": {"sha256": sha256_of(ecdict_path), "rows": len(ecdict)},
            "lemma.en.txt": {
                "sha256": sha256_of(lemma_path),
                "groups": len(lemma_groups),
            },
            "wordforge_words.json": {
                "sha256": sha256_of(wf_path),
                "rows": len(wf_rows),
            },
        },
        "denominator_unit": "lemma_group",
        "counts": counts,
        "counts_surface": counts_surface,
        "counts_cet46": counts_cet46,
        "lemma_groups": len(group_exam),
        "lemma_map": len(lemma_map),
        "multi_claim": multi_claim,
        "tag_labels": dict(EXAM_LABELS),
        "optional_labels": dict(OPTIONAL_LABELS),
        "wordforge_books": {k: wf_books.get(k, 0) for k in BOOK_TO_EXAM},
    }

    write_gz_json(
        os.path.join(data_dir, "exam_index.json.gz"),
        {
            "meta": meta,
            "exams": counts,
            "word_exam": {w: _join_tags(t) for w, t in word_exam.items()},
            "group_exam": group_exam,
            "freq": freq,
        },
    )
    write_gz_json(
        os.path.join(data_dir, "lemma_index.json.gz"),
        {
            "meta": {
                "built": now(),
                "source": "skywind3000/ECDICT + wordforge",
                "license": "MIT",
                "entries": len(lemma_map),
            },
            "lemma": lemma_map,
        },
    )

    build_ecdict_lite(raw_dir, data_dir, groups, meta)
    return meta


def build_ecdict_lite(raw_dir: str, data_dir: str, groups: dict, meta: dict) -> int:
    """只留词表内（及其词元组成员）的词，减小体积。"""
    path = os.path.join(raw_dir, "ecdict.csv")
    wanted: set[str] = set()
    for head, forms in groups.items():
        wanted.add(head)
        wanted.update(forms)
    out: dict[str, dict] = {}
    with io.open(path, encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            word = (row.get("word") or "").strip().lower()
            if not word or word not in wanted:
                continue
            out[word] = {
                "p": row.get("phonetic") or "",
                "t": row.get("translation") or "",
                "d": row.get("definition") or "",
                "o": row.get("exchange") or "",
                "f": eff_freq(row.get("frq"), row.get("bnc")),
                "x": row.get("detail") or "",
            }
    payload = {
        "meta": {
            "source": "skywind3000/ECDICT",
            "source_url": "https://github.com/skywind3000/ECDICT",
            "license": "MIT",
            "built": now(),
            "purpose": "补漏制卡用的释义/音标轻量索引（只含应试词表里的词）",
            "words": len(out),
        },
        "words": out,
    }
    write_gz_json(os.path.join(data_dir, "ecdict_lite.json.gz"), payload)
    return len(out)


# ------------------------------------------------------------------ 日语


def read_tanos(raw_dir: str) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for level in ("n5", "n4", "n3", "n2", "n1"):
        path = os.path.join(raw_dir, level + ".csv")
        if not os.path.isfile(path):
            continue
        with io.open(path, encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                expr = (row.get("expression") or "").strip()
                if not expr:
                    continue
                rows.append(
                    (
                        expr,
                        (row.get("reading") or "").strip(),
                        (row.get("meaning") or "").strip(),
                        level,
                    )
                )
    return rows


def read_openjlpt(raw_dir: str) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for level in ("n5", "n4", "n3", "n2", "n1"):
        path = os.path.join(raw_dir, "openjlpt-" + level + ".csv")
        if not os.path.isfile(path):
            continue
        with io.open(path, encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                expr = (row.get("word") or "").strip()
                if not expr:
                    continue
                rows.append(
                    (
                        expr,
                        (row.get("reading") or "").strip(),
                        (row.get("meanings") or "").strip(),
                        (row.get("level") or level).strip().lower(),
                    )
                )
    return rows


def read_eggrolls(raw_dir: str) -> list[dict]:
    path = os.path.join(raw_dir, "eggrolls_notes.csv")
    rows: list[dict] = []
    with io.open(path, encoding="utf-8", newline="") as handle:
        for line in handle:
            line = line.rstrip("\r\n")
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) < 39:
                continue
            match = re.search(r"-N([1-5])", cols[1])
            if not match:
                continue
            level = "n" + match.group(1)
            kanji = V.strip_markup(cols[3]).strip()
            furigana = V.strip_markup(cols[6]).strip()
            if not kanji and not furigana:
                continue
            rows.append(
                {
                    "word": kanji or furigana,
                    "reading": furigana,
                    "meaning": V.strip_markup(cols[7]).strip(),
                    "level": level,
                    "example": V.strip_markup(cols[12]).strip(),
                    "example_zh": V.strip_markup(cols[14]).strip(),
                }
            )
    return rows


def build_japanese(raw_dir: str, data_dir: str) -> dict:
    tanos = read_tanos(raw_dir)
    openjlpt = read_openjlpt(raw_dir)
    eggrolls = read_eggrolls(raw_dir)

    # 三份来源各自记 levels，冲突时 eggrolls 说了算
    levels_by_source: dict[str, dict[str, set]] = {"tanos": {}, "openjlpt": {}, "eggrolls": {}}
    readings: dict[str, dict[str, set]] = {}
    meanings: dict[str, str] = {}

    def _add(source: str, raw_word: str, raw_reading: str, meaning: str, level: str):
        level = (level or "").strip().lower()
        if level not in JLPT_LABELS:
            return
        words = [V.normalize_japanese(p) for p in _split_multi(raw_word)]
        words = [w for w in words if w]
        reads = [V.normalize_japanese(p) for p in _split_multi(raw_reading)]
        reads = [r for r in reads if r]
        if not words and not reads:
            return
        if not words:
            words = list(reads)
        for word in words:
            levels_by_source[source].setdefault(word, set()).add(level)
            bucket = readings.setdefault(word, {"reads": set(), "level": set()})
            bucket["level"].add(level)
            for read in reads:
                bucket["reads"].add(read)
                readings.setdefault(read, {"reads": set(), "level": set()})["reads"].add(read)
            if meaning and word not in meanings:
                meanings[word] = V.strip_markup(meaning)

    for raw_word, raw_reading, meaning, level in tanos:
        _add("tanos", raw_word, raw_reading, meaning, level)
    for raw_word, raw_reading, meaning, level in openjlpt:
        _add("openjlpt", raw_word, raw_reading, meaning, level)
    for row in eggrolls:
        _add("eggrolls", row["word"], row["reading"], row["meaning"], row["level"])

    # 读音 -> 写法，用于「假名命中也归到词表标准写法」
    reading_words: dict[str, set] = {}
    for word, bucket in readings.items():
        for read in bucket["reads"]:
            reading_words.setdefault(read, set()).add(word)
    for entry in eggrolls:
        word = V.normalize_japanese(entry["word"])
        read = V.normalize_japanese(entry["reading"])
        if word and read:
            reading_words.setdefault(read, set()).add(word)

    words_all = set()
    for source in levels_by_source.values():
        words_all |= set(source)
    # eggrolls 有该词时用 eggrolls 的等级，否则 OpenJLPT，再否则 tanos
    final_levels: dict[str, set] = {}
    for word in words_all:
        for source in ("eggrolls", "openjlpt", "tanos"):
            got = levels_by_source[source].get(word)
            if got:
                final_levels[word] = set(got)
                break
    # 读音的等级 = 所有带该读音的词条等级并集
    read_levels: dict[str, set] = {}
    for word, bucket in readings.items():
        for read in bucket["reads"]:
            read_levels.setdefault(read, set()).update(final_levels.get(word, bucket["level"]))

    by_word = {w: _join_tags(t) for w, t in final_levels.items() if t}
    by_reading = {r: _join_tags(t) for r, t in read_levels.items() if t}

    # 同一个假名对应多个词条、而且等级不一致 → 不猜，运行时进「待确认」
    ambiguous = []
    reading_word: dict[str, str] = {}
    for read, words in reading_words.items():
        real = sorted(w for w in words if w in by_word)
        if not real:
            continue
        level_sets = {frozenset(_split_multi(by_word[w])) for w in real}
        if len(level_sets) > 1:
            ambiguous.append(read)
        else:
            reading_word[read] = real[0]

    counts = {code: sum(1 for t in final_levels.values() if code in t) for code in JLPT_LABELS}
    meta = {
        "built": now(),
        "source": "eggrolls-JLPT10k ∪ OpenJLPT ∪ tanos（公开 JLPT 词表）",
        "source_url": "https://github.com/nihongodera/eggrolls-JLPT10k",
        "license": "CC BY-NC 4.0 / CC-BY-SA-4.0 / MIT（各自适用）",
        "files": {
            "eggrolls_notes.csv": {"sha256": sha256_of(os.path.join(raw_dir, "eggrolls_notes.csv")), "rows": len(eggrolls)},
        },
        "denominator_unit": "expression",
        "counts": counts,
        "ambiguous_readings": len(ambiguous),
        "level_labels": dict(JLPT_LABELS),
    }
    write_gz_json(
        os.path.join(data_dir, "jlpt_index.json.gz"),
        {
            "meta": meta,
            "levels": counts,
            "union": len(by_word),
            "by_word": by_word,
            "by_reading": by_reading,
            "reading_word": reading_word,
            "ambiguous_readings": sorted(ambiguous),
            "meanings": meanings,
        },
    )
    return meta


# ------------------------------------------------------------------ 报告


def write_report(en_meta: dict, ja_meta: dict) -> None:
    lines = [
        "# 索引构建记录",
        "",
        f"- 构建时间：{now()[:19].replace('T', ' ')}",
        "- 生成脚本：`工具\\构建词库索引.py`",
        "- 归一规则来源：`源码\\vocab_logic.py`（构建期与运行期共用同一套）",
        "",
        "## 英文（wordforge 六本词书 ∪ ECDICT）",
        "",
        f"- 来源：{en_meta['source']}",
        f"- 许可：{en_meta['license']}",
        f"- 分母口径：{en_meta['denominator_unit']}（词元组去重后的唯一词）",
        "",
        "| 词表 | 分母（词元组） | 词形口径 |",
        "| --- | ---: | ---: |",
    ]
    for code, label in EXAM_LABELS.items():
        lines.append(
            "| {} | {} | {} |".format(
                label,
                en_meta["counts"].get(code, 0),
                en_meta["counts_surface"].get(code, 0),
            )
        )
    lines += [
        f"| 四六级并集 | {en_meta['counts_cet46']} | — |",
        "",
        f"- 词元组总数：{en_meta['lemma_groups']}；词形映射：{en_meta['lemma_map']}（多认领 {en_meta['multi_claim']}）",
        "",
        "## 日语（eggrolls ∪ OpenJLPT ∪ tanos）",
        "",
        f"- 来源：{ja_meta['source']}",
        f"- 许可：{ja_meta['license']}",
        f"- 分母口径：{ja_meta['denominator_unit']}（汉字表记去重后的唯一词）",
        "",
        "| 等级 | 分母 |",
        "| --- | ---: |",
    ]
    for code in JLPT_ORDER:
        lines.append(f"| {JLPT_LABELS[code]} | {ja_meta['counts'].get(code, 0)} |")
    lines += [
        "",
        f"- 跨级假名歧义 {ja_meta['ambiguous_readings']} 个（运行时进「待确认」，不计入覆盖）",
        "",
        "> 这些数字来自公开/第三方词库，不等于考试机构官方大纲词表；插件里只用作自检和补漏的分母。",
        "",
    ]
    path = os.path.join(HERE, "索引构建记录.md")
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))


def main(argv: list[str]) -> int:
    only_lite = "--only-lite" in argv
    os.makedirs(DATA, exist_ok=True)
    if only_lite:
        index = V.load_json_gz(os.path.join(DATA, "exam_index.json.gz"))
        groups = {}
        for head, tags in index.get("group_exam", {}).items():
            groups.setdefault(head, {head})
        build_ecdict_lite(RAW, DATA, groups, index.get("meta", {}))
        print("已重建 ecdict_lite.json.gz")
        return 0

    print("构建英语词表索引…")
    en_meta = build_english(RAW, DATA)
    print("英语：", en_meta["counts"], "四六级并集", en_meta["counts_cet46"])
    print("构建日语词表索引…")
    ja_meta = build_japanese(RAW, DATA)
    print("日语：", ja_meta["counts"], "歧义读音", ja_meta["ambiguous_readings"])
    write_report(en_meta, ja_meta)
    print("完成，报告：工具\\索引构建记录.md")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
