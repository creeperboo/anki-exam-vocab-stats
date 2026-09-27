"""应试词汇覆盖统计 · Anki 插件主体。

入口：工具菜单 →「应试词汇覆盖统计…」、牌组齿轮/选项菜单、卡片浏览器表格右键、
左侧栏牌组/标签右键，以及卡片浏览器菜单栏里的顶级菜单「应试词汇」。
窗口里五个页签：总览 / 覆盖 / 来源 / 明细 / 设置。
统计全程离线、只读集合、不改卡片（除了明细页里你自己确认过的打标签/建补漏卡）。
唯一联网的两处：①「从 GitHub 检查更新」；②设置页的「一键构建素材库」
（下载制卡用的音频包，也可以用「从本地 zip 文件安装…」完全离线解决）。
两处都只拉取文件，不发送任何卡片内容。
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from collections import defaultdict

from aqt import gui_hooks, mw
from aqt.qt import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPainter,
    QPlainTextEdit,
    QPen,
    QProgressDialog,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTimer,
    Qt,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QColor,
)
from aqt.utils import askUser, showInfo, showWarning, tooltip

try:
    from . import (
        analysis,
        builder as B,
        hook_guard as HG,
        resources as RES,
        update_logic as U,
        vocab_logic as V,
    )
except ImportError:  # pragma: no cover
    import analysis
    import builder as B
    import hook_guard as HG
    import resources as RES
    import update_logic as U
    import vocab_logic as V

try:
    from aqt.qt import QAction
except ImportError:  # pragma: no cover  PyQt6 里 QAction 在 QtGui
    from PyQt6.QtGui import QAction


ADDON = __name__.split(".")[0]
ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ADDON_DIR, "data")
MENU_LABEL = "应试词汇覆盖统计…"
# 必须与「源码/version.txt」一致（有测试盯着）；线上更新靠它比较新旧。
__version__ = "0.3.0"
# 更新状态文件的目录名，和统计缓存同一个文件夹（都在用户配置目录里）
USER_DIR_NAME = "exam_vocab_stats"

DEFAULTS = {
    "field_map": {},
    "enabled_exams": list(V.ENGLISH_EXAM_CODES),
    "enabled_levels": list(V.JLPT_CODES),
    "language": "en",
    "count_unit": "unique_lemma",
    "source_dimension": "deck",
    "allow_duplicate_source": False,
    "state_rule": "highest_progress",
    "lemma_merge": True,
    "show_uncovered": True,
    "cache_enabled": True,
    "update_check": True,
    # 素材库：文本素材（释义/例句）已经内置在插件里；音频包体积大，单独下载。
    # materials_source 留空表示用插件内置的默认下载地址（GitHub Release / raw）。
    "materials_source": "",
    "materials_sha256": "",
    "materials_installed_at": 0,
    # 上一次用过的本地音频包（「从本地 zip 文件安装…」），只记路径方便下次定位
    "materials_zip": "",
    # 一键补漏制卡的默认值
    "builder_defaults": {
        "limit": 500,
        "new_per_day": 0,
        "add_tags": True,
    },
    "scope": {
        "deck_ids": [],
        "include_subdecks": True,
        "tags_include": [],
        "tags_exclude": [],
        "tag_mode": "any",
        "notetype_ids": [],
        "states": ["new", "learn", "review", "suspended"],
        "search": "",
    },
}

# v0.2 遗留的配置键：v0.3 已经删掉自定义词表与词典配置，写回时顺手丢掉，
# 免得配置文件里长期躺着用不上的东西。
LEGACY_CONFIG_KEYS = (
    "custom_wordlists",
    "dictionary_paths",
    "audio_sources",
    "network_materials",
    "tts_fallback",
)

PALETTE = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
    "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
]

# 来源页的「来源维度」下拉：(显示名, 内部键)
SOURCE_DIMENSIONS = (
    ("牌组（一个词只算一次）", "deck"),
    ("标签（一词多标签各算一次）", "tag"),
    ("笔记类型（一词多类型各算一次）", "notetype"),
)
STATE_COLORS = {
    "new": "#4e79a7",
    "learn": "#f28e2b",
    "review": "#59a14f",
    "suspended": "#bab0ac",
}

# 顶部「统计单位」下拉 → 统计层内部的键
UNIT_KEYS = {"unique_lemma": "word", "note": "note", "card": "card"}
UNIT_LABELS = {"unique_lemma": "唯一词", "note": "笔记", "card": "卡片"}

_matchers = None


# ---------------------------------------------------------------- 配置


def deep_merge(defaults: dict, incoming: dict) -> dict:
    out = dict(defaults)
    for key, value in (incoming or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config() -> dict:
    raw = mw.addonManager.getConfig(ADDON) or {}
    return deep_merge(DEFAULTS, raw)


def save_config(config: dict) -> None:
    # 老版本留下的自定义词表 / 词典配置键不写回去（功能已经删了）
    clean = {key: value for key, value in dict(config).items() if key not in LEGACY_CONFIG_KEYS}
    mw.addonManager.writeConfig(ADDON, clean)


def _run_on_main(fn) -> None:
    """把一段改界面的代码丢回主线程（后台任务里更新进度条要用）。"""
    try:
        mw.taskman.run_on_main(fn)
    except Exception:  # noqa: BLE001
        try:
            QTimer.singleShot(0, fn)
        except Exception:  # noqa: BLE001
            pass


def get_matchers(config: dict | None = None, force: bool = False) -> dict:
    """载入三个词库索引（约 0.2 秒），进程内只做一次。"""
    global _matchers
    if config is None:
        config = load_config()
    merge = bool(config.get("lemma_merge", True))
    if _matchers is not None and not force and _matchers.get("merge") == merge:
        return _matchers
    exam = V.load_json_gz(os.path.join(DATA_DIR, "exam_index.json.gz"))
    lemma = V.load_json_gz(os.path.join(DATA_DIR, "lemma_index.json.gz"))
    jlpt = V.load_json_gz(os.path.join(DATA_DIR, "jlpt_index.json.gz"))
    _matchers = {
        "en": V.EnglishMatcher(exam, lemma, merge_lemma=merge),
        "ja": V.JapaneseMatcher(jlpt),
        "en_meta": exam.get("meta", {}),
        "ja_meta": jlpt.get("meta", {}),
        "merge": merge,
    }
    return _matchers


def field_index_map(config: dict, force: bool = False) -> dict:
    """配置里的 field_map 键是字符串，这里转成 int 给查询层用。"""
    out = {}
    for key, value in (config.get("field_map") or {}).items():
        if not value or not value.get("fields"):
            continue
        if value.get("enabled") is False:
            # 设置页里没勾的笔记类型一律不参与统计，哪怕它还留着字段配置
            continue
        if value.get("language") == "none":
            continue
        try:
            ntid = int(key)
        except (TypeError, ValueError):
            continue
        out[ntid] = value
    return out


# ---------------------------------------------------------------- 字段识别

# 字段识别的判定规则只有一份，放在纯逻辑层 analysis.py 里，
# 这样「真 Anki」和「真实集合只读副本抽查」跑出来的一定是同一套结论。
detect_fields = analysis.detect_fields
sample_notes = analysis.sample_notes


def ensure_field_map(col, config: dict, matchers: dict, force: bool = False) -> bool:
    """第一次运行时自动填一份字段映射；返回是否有改动。"""
    if config.get("field_map") and not force:
        return False
    config["field_map"] = detect_fields(col, matchers)
    return True


# ---------------------------------------------------------------- 本机素材


def user_files_dir() -> str:
    """插件自己的用户目录（缓存、词典解析结果都放这儿，不往 addons21 里写）。"""
    base = ""
    try:
        base = mw.pm.profileFolder() or ""
    except Exception:
        base = ""
    if not base:
        base = ADDON_DIR
    path = os.path.join(base, USER_DIR_NAME)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        path = ADDON_DIR
    return path


_resources = None
_resources_key = None


def get_resources(config: dict, force: bool = False):
    """载入内置素材层。素材取自插件自带的 data\\*_materials.json.gz，不依赖配置。"""
    global _resources, _resources_key
    key = user_files_dir()
    if _resources is not None and not force and _resources_key == key:
        return _resources
    _resources = RES.LocalResources(config, cache_dir=user_files_dir())
    _resources_key = key
    return _resources


# ---------------------------------------------------------------- 统计


def compute(col, config: dict, scope: dict, use_cache: bool = True) -> dict:
    matchers = get_matchers(config)
    signature = analysis.collection_signature(
        col, scope, config, matchers.get("en_meta", {})
    )
    if use_cache and config.get("cache_enabled", True):
        cached = analysis.read_cached(col, signature)
        if cached:
            cached["from_cache"] = True
            return cached

    started = time.time()
    collected = analysis.collect_entries(
        col, scope, field_index_map(config), matchers
    )
    summary = analysis.summarize(col, collected, config, matchers)
    summary["scope"] = scope
    summary["elapsed"] = round(time.time() - started, 2)
    summary["from_cache"] = False
    summary["index_meta"] = matchers.get("en_meta", {}).get("built", "")
    if config.get("cache_enabled", True):
        analysis.save_cache(col, signature, summary)
    return summary


# ---------------------------------------------------------------- 绘图组件


class Donut(QWidget):
    """学习状态环图。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(220, 180)
        self.rows = []
        self.unit_label = "个词"

    def set_rows(self, rows, unit_label: str | None = None):
        self.rows = rows or []
        if unit_label:
            self.unit_label = unit_label
        self.update()

    def paintEvent(self, event):  # noqa: N802 (Qt 命名)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self.width()
        height = self.height()
        size = min(width, height) - 20
        if size <= 10:
            return
        rect = self.rect()
        left = 10
        top = (height - size) // 2
        total = sum(r["words"] for r in self.rows) or 1
        start = 90 * 16
        for row in self.rows:
            span = int(round(-360 * 16 * row["words"] / total))
            color = STATE_COLORS.get(row["code"], "#cccccc")
            painter.setPen(QPen(QColor(color), 0))
            painter.setBrush(QColor(color))
            painter.drawPie(left, top, size, size, start, span)
            start += span
        inner = int(size * 0.58)
        painter.setBrush(self.palette().window())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(
            left + (size - inner) // 2, top + (size - inner) // 2, inner, inner
        )
        painter.setPen(self.palette().windowText().color())
        font = painter.font()
        font.setPointSize(11)
        painter.setFont(font)
        painter.drawText(
            left, top, size, size, Qt.AlignmentFlag.AlignCenter,
            f"{total}\n{self.unit_label}",
        )
        # 图例
        painter.setPen(self.palette().windowText().color())
        font.setPointSize(9)
        painter.setFont(font)
        y = top + 6
        for row in self.rows:
            painter.setBrush(QColor(STATE_COLORS.get(row["code"], "#cccccc")))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRect(left + size + 14, y + 3, 10, 10)
            painter.setPen(self.palette().windowText().color())
            painter.drawText(
                left + size + 30,
                y,
                width - (left + size + 30),
                18,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                f"{row['label']} {row['words']}（{row['pct']}%）",
            )
            y += 20


class StackedBar(QWidget):
    """水平堆叠条：按来源显示覆盖词占比。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(26)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.rows = []

    def set_rows(self, rows):
        self.rows = rows or []
        self.update()

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        width = self.width()
        height = self.height()
        total = sum(r["count"] for r in self.rows) or 1
        x = 0
        for index, row in enumerate(self.rows):
            span = int(round(width * row["count"] / total))
            if index == len(self.rows) - 1:
                span = width - x
            painter.setBrush(QColor(PALETTE[index % len(PALETTE)]))
            painter.drawRect(x, 0, max(span, 0), height)
            x += span
            if x >= width:
                break


class ProgressCell(QWidget):
    """覆盖率进度条（表格里用）。"""

    def __init__(self, rate: float, color: str = "#4e79a7", parent=None):
        super().__init__(parent)
        self.rate = max(0.0, min(100.0, rate))
        self.color = color

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#e8e8e8"))
        painter.drawRect(0, 7, self.width(), self.height() - 14)
        painter.setBrush(QColor(self.color))
        painter.drawRect(0, 7, int(self.width() * self.rate / 100.0), self.height() - 14)


# ---------------------------------------------------------------- 主窗口


def _item(text, align_right=False):
    cell = QTableWidgetItem(str(text))
    if align_right:
        cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return cell


def _pair(value):
    """下拉/列表里存的 (语言, 词表码) 统一成 tuple 再比，Qt 各版本回传的类型不一致。"""
    if not value:
        return ()
    try:
        return tuple(value)
    except TypeError:
        return (value,)


class StatsDialog(QDialog):
    def __init__(self, col, config: dict, scope: dict, parent=None):
        super().__init__(parent or mw)
        self.col = col
        self.config = config
        self.scope = scope
        self.summary = None
        self._detail_index = []
        self.setWindowTitle("应试词汇覆盖统计")
        self.resize(1080, 720)
        self._build()

    # ---- 界面骨架
    def _build(self):
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("语言"))
        self.language_box = QComboBox()
        self.language_box.addItem("英语应试", "en")
        self.language_box.addItem("日语 JLPT", "ja")
        self.language_box.addItem("全部", "all")
        index = max(0, self.language_box.findData(self.config.get("language", "en")))
        self.language_box.setCurrentIndex(index)
        self.language_box.currentIndexChanged.connect(self.refresh)
        top.addWidget(self.language_box)
        top.addSpacing(12)
        top.addWidget(QLabel("统计单位"))
        self.unit_box = QComboBox()
        self.unit_box.addItem("唯一词（按词元去重）", "unique_lemma")
        self.unit_box.addItem("笔记数", "note")
        self.unit_box.addItem("卡片数", "card")
        self.unit_box.setCurrentIndex(
            max(0, self.unit_box.findData(self.config.get("count_unit", "unique_lemma")))
        )
        self.unit_box.currentIndexChanged.connect(self.render_overview)
        top.addWidget(self.unit_box)
        top.addStretch(1)
        self.scope_label = QLabel("")
        top.addWidget(self.scope_label)
        self.refresh_button = QPushButton("重新统计")
        self.refresh_button.clicked.connect(lambda: self.refresh(force=True))
        top.addWidget(self.refresh_button)
        self.scope_button = QPushButton("选择范围…")
        self.scope_button.clicked.connect(self.edit_scope)
        top.addWidget(self.scope_button)
        self.export_button = QPushButton("导出 CSV")
        self.export_button.clicked.connect(self.export_csv)
        top.addWidget(self.export_button)
        layout.addLayout(top)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_overview(), "总览")
        self.tabs.addTab(self._build_coverage(), "覆盖")
        self.tabs.addTab(self._build_sources(), "来源")
        self.tabs.addTab(self._build_detail(), "明细")
        self.tabs.addTab(self._build_settings(), "设置")
        layout.addWidget(self.tabs)
        self.status = QLabel("正在统计…")
        layout.addWidget(self.status)

    def _build_overview(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        self.donut = Donut()
        layout.addWidget(self.donut)
        right = QVBoxLayout()
        self.overview_text = QLabel("")
        self.overview_text.setTextFormat(Qt.TextFormat.PlainText)
        self.overview_text.setWordWrap(True)
        right.addWidget(self.overview_text)
        self.overview_table = QTableWidget(0, 4)
        self.overview_table.setHorizontalHeaderLabels(["词表", "命中词数", "占范围内词数", "词表总词数"])
        self.overview_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.overview_table.verticalHeader().setVisible(False)
        right.addWidget(self.overview_table)
        layout.addLayout(right, 1)
        return page

    def _build_coverage(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.coverage_table = QTableWidget(0, 6)
        self.coverage_table.setHorizontalHeaderLabels(
            ["词表", "词表总词数", "已覆盖", "覆盖率", "未覆盖", "进度"]
        )
        header = self.coverage_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.coverage_table.setColumnWidth(5, 160)
        self.coverage_table.verticalHeader().setVisible(False)
        layout.addWidget(self.coverage_table)
        self.coverage_note = QLabel(
            "口径：已覆盖 ÷ 词表总词数，全部按「唯一词（词元去重）」算，不随顶部的统计单位变化。"
            "英语分母来自公开词书（wordforge 六本 ∪ ECDICT 考试标签），日语来自公开 JLPT 词表"
            "（eggrolls JLPT10k ∪ tanos ∪ OpenJLPT）；词表与版本都写在设置页。"
            "这些都不是考试机构官方大纲数字，适合自检和补漏，不适合对外宣称「官方覆盖率」。"
            "跨词表的词各自都算，并集只算一次。"
        )
        self.coverage_note.setWordWrap(True)
        layout.addWidget(self.coverage_note)
        return page

    def _build_sources(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        top = QHBoxLayout()
        top.addWidget(QLabel("词表"))
        self.source_box = QComboBox()
        self.source_box.currentIndexChanged.connect(self.render_sources)
        top.addWidget(self.source_box, 1)
        top.addWidget(QLabel("来源维度"))
        self.source_dim_box = QComboBox()
        for label, code in SOURCE_DIMENSIONS:
            self.source_dim_box.addItem(label, code)
        self.source_dim_box.setCurrentIndex(
            max(0, self.source_dim_box.findData(self.config.get("source_dimension", "deck")))
        )
        self.source_dim_box.currentIndexChanged.connect(self.change_source_dimension)
        top.addWidget(self.source_dim_box)
        layout.addLayout(top)
        self.source_bar = StackedBar()
        layout.addWidget(self.source_bar)
        self.source_table = QTableWidget(0, 3)
        self.source_table.setHorizontalHeaderLabels(["来源", "覆盖词数", "占该词表覆盖数"])
        self.source_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.source_table.verticalHeader().setVisible(False)
        layout.addWidget(self.source_table)
        self.source_note = QLabel("")
        self.source_note.setWordWrap(True)
        layout.addWidget(self.source_note)
        return page

    def _build_detail(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        bar = QHBoxLayout()
        self.detail_filter = QComboBox()
        for label, code in (
            ("全部词条", "all"),
            ("只看已覆盖（命中词表）", "hit"),
            ("只看未覆盖（词表里有、范围里没有）", "uncovered"),
        ):
            self.detail_filter.addItem(label, code)
        self.detail_filter.currentIndexChanged.connect(self.render_detail)
        bar.addWidget(self.detail_filter)
        self.detail_match = QComboBox()
        self.detail_match.addItem("任一命中（并集）", "any")
        self.detail_match.addItem("全部命中（交集）", "all")
        self.detail_match.currentIndexChanged.connect(self.render_detail)
        self.detail_match.setVisible(False)
        bar.addWidget(self.detail_match)
        bar.addStretch(1)
        self.detail_count = QLabel("")
        bar.addWidget(self.detail_count)
        copy_button = QPushButton("复制词表")
        copy_button.clicked.connect(self.copy_words)
        bar.addWidget(copy_button)
        layout.addLayout(bar)

        # 词表多选 + 打标签按钮。只在「命中」「未覆盖」两档露出来。
        code_row = QHBoxLayout()
        self.detail_codes = QListWidget()
        self.detail_codes.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.detail_codes.setMaximumHeight(104)
        self.detail_codes.setVisible(False)
        self.detail_codes.itemChanged.connect(self.on_detail_codes_changed)
        code_row.addWidget(self.detail_codes, 1)
        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        self.tag_button = QPushButton("给命中笔记打标签…")
        self.tag_button.clicked.connect(self.add_exam_tags)
        side_layout.addWidget(self.tag_button)
        self.untag_button = QPushButton("去掉这些标签")
        self.untag_button.clicked.connect(self.remove_exam_tags)
        side_layout.addWidget(self.untag_button)
        self.build_button = QPushButton("一键生成补漏牌组…")
        self.build_button.clicked.connect(self.build_uncovered_deck)
        side_layout.addWidget(self.build_button)
        self.tag_hint = QLabel("")
        self.tag_hint.setWordWrap(True)
        side_layout.addWidget(self.tag_hint)
        side_layout.addStretch(1)
        self.detail_side = side
        self.detail_side.setVisible(False)
        code_row.addWidget(side)
        layout.addLayout(code_row)

        self.detail_table = QTableWidget(0, 9)
        self.detail_table.setHorizontalHeaderLabels(
            [
                "词元", "原始写法", "语言", "命中词表", "状态", "卡片",
                "来源牌组", "原因", "备注",
            ]
        )
        self.detail_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        self.detail_table.horizontalHeader().setStretchLastSection(True)
        self.detail_table.verticalHeader().setVisible(False)
        self.detail_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.detail_table.setSortingEnabled(False)
        self.detail_table.itemDoubleClicked.connect(self.open_selected_in_browser)
        layout.addWidget(self.detail_table)
        hint = QLabel(
            "勾选上面一个或多个词表可以只看这些词的命中情况（可切换并集/交集）；"
            "双击一行 → 在卡片浏览器里打开对应的笔记。\n"
            "「原因」列只有两态：已覆盖 / 未覆盖（一词多解、匹配没认出来、词表里本来就没有，"
            "对用户都算未覆盖）。一键生成补漏牌组只补「词表里有、你的牌组里确实没有」的词，"
            "不会给「其实有卡、只是没认出来」的词重复制卡。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return page

    def _build_settings(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("取词字段（每个笔记类型一对：从哪个字段取词）"))
        field_hint = QLabel(
            "「应试词命中率」= 抽样 60 条里有多少条能命中考试词表，只是用来判断这一列像不像取词字段，"
            "不进入任何统计数字。词性、例句、读音这类字段按字段名直接排除；"
            "像例句的长文本会单独标出来，识别不准就在下面手动指定。"
        )
        field_hint.setWordWrap(True)
        layout.addWidget(field_hint)
        self.field_table = QTableWidget(0, 7)
        self.field_table.setHorizontalHeaderLabels(
            ["启用", "笔记类型", "取词字段", "兜底/假名字段", "语言", "例句分词", "说明"]
        )
        self.field_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        self.field_table.horizontalHeader().setStretchLastSection(True)
        self.field_table.verticalHeader().setVisible(False)
        layout.addWidget(self.field_table, 1)

        row = QHBoxLayout()
        self.exam_boxes = {}
        for code, label, _group in V.ENGLISH_EXAMS + V.OPTIONAL_EXAMS:
            box = QCheckBox(label)
            box.setChecked(code in self.config.get("enabled_exams", []))
            self.exam_boxes[code] = box
            row.addWidget(box)
        for code, label in V.JLPT_LEVELS:
            box = QCheckBox(label)
            box.setChecked(code in self.config.get("enabled_levels", []))
            self.exam_boxes[code] = box
            row.addWidget(box)
        row.addStretch(1)
        layout.addLayout(row)

        row2 = QHBoxLayout()
        self.merge_box = QCheckBox("合并词元组标签（认识 child 就算认识 children）")
        self.merge_box.setChecked(bool(self.config.get("lemma_merge", True)))
        row2.addWidget(self.merge_box)
        self.cache_box = QCheckBox("使用本地缓存")
        self.cache_box.setChecked(bool(self.config.get("cache_enabled", True)))
        row2.addWidget(self.cache_box)
        row2.addStretch(1)
        layout.addLayout(row2)

        row3 = QHBoxLayout()
        self.update_box = QCheckBox(
            "启动时自动检查更新（从 GitHub 读 version.txt，每天最多一次）"
        )
        self.update_box.setChecked(bool(self.config.get("update_check", True)))
        row3.addWidget(self.update_box)
        row3.addStretch(1)
        layout.addLayout(row3)

        row4 = QHBoxLayout()
        self.update_button = QPushButton("检查更新（GitHub）")
        self.update_button.clicked.connect(lambda: check_for_update(silent=False))
        row4.addWidget(self.update_button)
        self.update_label = QLabel("")
        row4.addWidget(self.update_label, 1)
        layout.addLayout(row4)

        # —— 内置素材库与一键补漏制卡
        box = QGroupBox("素材库与一键补漏制卡")
        box_layout = QVBoxLayout(box)
        self.material_label = QLabel("")
        self.material_label.setWordWrap(True)
        box_layout.addWidget(self.material_label)
        mat_row = QHBoxLayout()
        self.install_button = QPushButton("一键构建素材库（下载音频包）")
        self.install_button.clicked.connect(self.install_materials)
        mat_row.addWidget(self.install_button)
        self.local_zip_button = QPushButton("从本地 zip 文件安装…")
        self.local_zip_button.clicked.connect(self.install_materials_from_zip)
        mat_row.addWidget(self.local_zip_button)
        mat_row.addStretch(1)
        box_layout.addLayout(mat_row)

        defaults = dict(DEFAULTS["builder_defaults"])
        defaults.update(self.config.get("builder_defaults") or {})
        build_row = QHBoxLayout()
        build_row.addWidget(QLabel("单次最多生成"))
        self.build_limit_box = QSpinBox()
        self.build_limit_box.setRange(1, 20000)
        self.build_limit_box.setValue(int(defaults.get("limit") or 500))
        build_row.addWidget(self.build_limit_box)
        build_row.addWidget(QLabel("张卡片；新牌组的新卡上限"))
        self.build_new_box = QSpinBox()
        self.build_new_box.setRange(0, 9999)
        self.build_new_box.setValue(int(defaults.get("new_per_day") or 0))
        build_row.addWidget(self.build_new_box)
        self.build_tag_box = QCheckBox("制卡时顺手打「应试::」标签")
        self.build_tag_box.setChecked(bool(defaults.get("add_tags", True)))
        build_row.addWidget(self.build_tag_box)
        build_row.addStretch(1)
        box_layout.addLayout(build_row)
        layout.addWidget(box)

        buttons = QHBoxLayout()
        redo = QPushButton("重新识别字段")
        redo.clicked.connect(self.redetect_fields)
        buttons.addWidget(redo)
        clear = QPushButton("清除缓存")
        clear.clicked.connect(self.clear_cache)
        buttons.addWidget(clear)
        buttons.addStretch(1)
        save = QPushButton("保存设置并重新统计")
        save.clicked.connect(self.apply_settings)
        buttons.addWidget(save)
        layout.addLayout(buttons)
        self.index_label = QLabel("")
        layout.addWidget(self.index_label)
        return page

    # ---- 范围
    def edit_scope(self):
        dialog = ScopeDialog(self.col, self.config, self.scope, self)
        if dialog.exec():
            self.scope = dialog.result_scope()
            self.config["scope"] = self.scope
            save_config(self.config)
            self.refresh(force=True)

    def describe_scope(self, scope: dict) -> str:
        parts = []
        deck_ids = scope.get("deck_ids") or []
        if deck_ids:
            names = [
                self.col.decks.name(did)
                for did in deck_ids
                if self.col.decks.get(did)
            ]
            text = "、".join(names[:2]) + ("…" if len(names) > 2 else "")
            parts.append(f"牌组：{text}" + ("（含子牌组）" if scope.get("include_subdecks") else ""))
        else:
            parts.append("牌组：全部")
        if scope.get("tags_include"):
            parts.append("标签：" + "、".join(scope["tags_include"][:2]))
        if scope.get("notetype_ids"):
            names = []
            for ntid in scope["notetype_ids"]:
                model = self.col.models.get(ntid)
                if model:
                    names.append(model["name"])
            parts.append("笔记类型：" + "、".join(names[:2]) + ("…" if len(names) > 2 else ""))
        states = scope.get("states") or []
        if states and len(states) < 4:
            parts.append("状态：" + "、".join(V.STATE_LABELS[s] for s in states))
        if scope.get("search"):
            parts.append(f"搜索式：{scope['search']}")
        return "　|　".join(parts)

    # ---- 统计与渲染
    def refresh(self, force: bool = False):
        self.config["language"] = self.language_box.currentData()
        self.config["count_unit"] = self.unit_box.currentData()
        self.refresh_button.setEnabled(False)
        self.status.setText("正在统计…")
        try:
            self.summary = compute(self.col, self.config, self.scope, use_cache=not force)
        except Exception as exc:  # 出错要让用户看得见，别悄悄吞掉
            self.refresh_button.setEnabled(True)
            self.status.setText(f"统计失败：{exc}")
            showWarning(f"统计失败：{exc}")
            return
        self.refresh_button.setEnabled(True)
        self.scope_label.setText(self.describe_scope(self.scope))
        self.render_overview()
        self.render_coverage()
        self.render_sources()
        self.render_detail()
        self.render_settings()
        cache_note = "（用了缓存）" if self.summary.get("from_cache") else ""
        self.status.setText(
            f"范围里 {self.summary['scanned_notes']} 条笔记 / {self.summary['scanned_cards']} 张卡，"
            f"用时 {self.summary.get('elapsed', 0)} 秒{cache_note}"
        )

    def current_languages(self):
        choice = self.language_box.currentData()
        return ["en", "ja"] if choice == "all" else [choice]

    def current_unit(self) -> tuple:
        """返回 (内部键, 中文名)，例如 ("word", "唯一词")。"""
        data = self.unit_box.currentData()
        return UNIT_KEYS.get(data, "word"), UNIT_LABELS.get(data, "唯一词")

    def render_overview(self):
        if not self.summary:
            return
        summary = self.summary
        unit, unit_label = self.current_unit()
        lines = [
            f"统计单位：{unit_label}（覆盖页与来源页的口径固定为「唯一词」，"
            "因为覆盖率的分母是词表总词数）"
        ]
        tables = []
        for language in self.current_languages():
            data = summary["languages"].get(language) or {}
            name = "英语应试" if language == "en" else "日语 JLPT"
            totals = data.get("unit_totals") or {}
            lines.append(
                f"{name}：范围内 {totals.get(unit, 0)} 个{unit_label}"
                f"（唯一词 {data.get('words', 0)} / 笔记 {data.get('notes', 0)} / 卡片 {data.get('cards', 0)}）；"
                f"命中词表 {data.get('matched_words', 0)} 个，"
                f"未命中 {data.get('unmatched_words', 0)} 个，待确认 {data.get('pending_words', 0)} 个。"
            )
            tables.append((language, data))
        self.overview_text.setText("\n".join(lines))

        rows = []
        for language, data in tables:
            for row in data.get("coverage", []):
                if row["code"] in ("cet46", "jlpt"):
                    labels = {"cet46": "四六级并集", "jlpt": "JLPT 并集"}
                    row = dict(row, label=labels[row["code"]])
                rows.append((language, row))
        self.overview_table.setRowCount(len(rows))
        for index, (language, row) in enumerate(rows):
            prefix = "" if len(tables) == 1 else ("英·" if language == "en" else "日·")
            self.overview_table.setItem(index, 0, _item(prefix + row["label"]))
            self.overview_table.setItem(index, 1, _item(row["covered"], True))
            self.overview_table.setItem(index, 2, _item(f"{row['share_of_range']}%", True))
            self.overview_table.setItem(index, 3, _item(row["total"], True))

        primary = self.current_languages()[0]
        data = summary["languages"].get(primary) or {}
        states = (data.get("states_by_unit") or {}).get(unit) or data.get("states", [])
        self.donut.set_rows(
            [r for r in states if r["words"] > 0] or states,
            unit_label="个" + unit_label,
        )

    def render_coverage(self):
        rows = []
        for language in self.current_languages():
            data = self.summary["languages"].get(language) or {}
            for row in data.get("coverage", []):
                rows.append((language, row))
        self.coverage_table.setRowCount(len(rows))
        for index, (language, row) in enumerate(rows):
            prefix = "" if len(self.current_languages()) == 1 else ("英·" if language == "en" else "日·")
            self.coverage_table.setItem(index, 0, _item(prefix + row["label"]))
            self.coverage_table.setItem(index, 1, _item(row["total"], True))
            self.coverage_table.setItem(index, 2, _item(row["covered"], True))
            self.coverage_table.setItem(index, 3, _item(f"{row['rate']}%", True))
            self.coverage_table.setItem(index, 4, _item(row["missing"], True))
            bar = ProgressCell(row["rate"], PALETTE[index % len(PALETTE)])
            self.coverage_table.setCellWidget(index, 5, bar)
        self.coverage_table.resizeRowsToContents()

    def _sync_source_codes(self):
        """按当前语言/词表开关刷新「词表」下拉。

        注意：列表内容没变时**什么都不做**。以前这里每次都 clear + 重建，
        用户一改选就被冲回第一项，看起来就是「点了切不动」。
        """
        wanted = []
        for language in self.current_languages():
            data = self.summary["languages"].get(language) or {}
            for row in data.get("coverage", []):
                if row["code"] in ("cet46", "jlpt"):
                    continue
                wanted.append(
                    (
                        ("英·" if language == "en" else "日·") + row["label"],
                        (language, row["code"]),
                    )
                )
        existing = [
            (self.source_box.itemText(i), _pair(self.source_box.itemData(i)))
            for i in range(self.source_box.count())
        ]
        if existing == [(text, _pair(value)) for text, value in wanted]:
            return

        keep = _pair(self.source_box.currentData())
        self.source_box.blockSignals(True)
        self.source_box.clear()
        for text, value in wanted:
            self.source_box.addItem(text, value)
        index = 0
        if keep:
            for i in range(self.source_box.count()):
                if _pair(self.source_box.itemData(i)) == keep:
                    index = i
                    break
        self.source_box.setCurrentIndex(index)
        self.source_box.blockSignals(False)

    def change_source_dimension(self):
        """换来源维度：存进配置再重算一次（缓存键里带了维度，不会串味）。"""
        if not self.summary:
            return
        code = self.source_dim_box.currentData()
        if code == self.summary.get("source_dimension"):
            return
        self.config["source_dimension"] = code
        save_config(self.config)
        self.refresh()

    def render_sources(self):
        if not self.summary:
            return
        self._sync_source_codes()
        dimension = self.summary.get("source_dimension") or "deck"
        self.source_note.setText(
            "口径：一个词只算一次，归到最具体的子牌组；占比 = 该来源覆盖词数 ÷ 该词表覆盖词数。"
            if dimension == "deck"
            else "口径：一个词有几个标签（或笔记类型）就在几行各算一次，分母是出现次数合计；"
            "没标签的归「（无标签）」。插件自己打的「应试::…」标签已排除。"
        )
        if not self.source_box.count():
            self.source_bar.set_rows([])
            self.source_table.setRowCount(0)
            return
        payload = self.source_box.currentData()
        if not payload:
            return
        language, code = payload
        data = self.summary["languages"].get(language) or {}
        rows = (data.get("sources") or {}).get(code, [])
        self.source_bar.set_rows(rows)
        self.source_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            self.source_table.setItem(index, 0, _item(row["name"]))
            self.source_table.setItem(index, 1, _item(row["count"], True))
            self.source_table.setItem(index, 2, _item(f"{row['pct']}%", True))

    def _detail_rows(self):
        choice = self.detail_filter.currentData()
        language_filter = self.current_languages()
        words = [w for w in self.summary["words"] if w["language"] in language_filter]
        selected = self.checked_detail_codes()
        if choice == "hit":
            words = [w for w in words if w["confident"] and (w["exams"] or w["levels"])]
            words = analysis.filter_words_by_codes(
                words,
                selected,
                self.detail_match.currentData() or "any",
            )
        elif choice == "uncovered":
            words = self._uncovered_rows(selected)
        return words

    def _uncovered_rows(self, selected) -> list:
        """多个词表的「未覆盖」并起来（同一个词只出现一次）。"""
        matchers = get_matchers(self.config)
        seen = set()
        rows = []
        for language, code in selected or ():
            found = analysis.uncovered_words(matchers, self.summary, language, code)
            label = code
            for row in found:
                key = (language, row["key"])
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        "key": row["key"],
                        "surface": row.get("surface") or row["key"],
                        "language": language,
                        "exams": [],
                        "levels": [],
                        "state": "uncovered",
                        "cards": 0,
                        "deck_names": [],
                        "confident": True,
                        "meaning": row["meaning"],
                        "alternatives": [],
                        "note_ids": [],
                        "notetype_ids": [],
                        "tags": [],
                        "reason": row.get("reason") or analysis.GAP_MISSING,
                        "code": code,
                        "code_label": label,
                    }
                )
        rows.sort(key=lambda r: (r["language"], r["key"]))
        return rows

    # ---- 明细页的词表多选
    def _sync_detail_codes(self, choice: str) -> None:
        """重建词表多选列表（内容没变就不动，避免把勾选冲掉）。"""
        wanted = []
        for language in self.current_languages():
            data = self.summary["languages"].get(language) or {}
            for row in data.get("coverage", []):
                wanted.append(
                    (
                        ("英·" if language == "en" else "日·") + row["label"],
                        (language, row["code"]),
                    )
                )
        existing = [
            (self.detail_codes.item(i).text(), _pair(self.detail_codes.item(i).data(Qt.ItemDataRole.UserRole)))
            for i in range(self.detail_codes.count())
        ]
        if existing != [(text, _pair(value)) for text, value in wanted]:
            checked = {tuple(item) for item in self.checked_detail_codes()}
            self.detail_codes.blockSignals(True)
            self.detail_codes.clear()
            for text, value in wanted:
                item = QListWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, value)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked
                    if tuple(value) in checked
                    else Qt.CheckState.Unchecked
                )
                self.detail_codes.addItem(item)
            self.detail_codes.blockSignals(False)

        # 「未覆盖」档必须选中至少一个词表才有意义，默认帮用户勾第一个。
        # 这一段必须在上面那个「内容没变就跳过重建」的分支之外：列表内容没变时
        # 前面会直接跳过重建，早期版本把默认勾选也一起跳过了，结果切到未覆盖档
        # 一行都渲染不出来。
        if choice == "uncovered" and not self.checked_detail_codes() and self.detail_codes.count():
            self.detail_codes.blockSignals(True)
            self.detail_codes.item(0).setCheckState(Qt.CheckState.Checked)
            self.detail_codes.blockSignals(False)

    def checked_detail_codes(self) -> list:
        """当前勾选的 [(语言, 词表码), …]。"""
        out = []
        for i in range(self.detail_codes.count()):
            item = self.detail_codes.item(i)
            if item.checkState() != Qt.CheckState.Checked:
                continue
            value = _pair(item.data(Qt.ItemDataRole.UserRole))
            if value:
                out.append(value)
        return out

    def on_detail_codes_changed(self, _item=None):
        self.render_detail()

    def render_detail(self):
        if not self.summary:
            return
        choice = self.detail_filter.currentData()
        show_codes = choice in ("hit", "uncovered")
        self.detail_codes.setVisible(show_codes)
        self.detail_match.setVisible(choice == "hit")
        self.detail_side.setVisible(choice in ("hit", "uncovered"))
        if show_codes:
            self._sync_detail_codes(choice)

        words = self._detail_rows()
        limit = 3000
        shown = words[:limit]
        self.detail_count.setText(
            f"列出 {len(shown)} / {len(words)} 个" + ("（超过 3000 只显示前 3000，导出 CSV 拿全量）" if len(words) > limit else "")
        )
        self.detail_table.setRowCount(len(shown))
        self._detail_index = []
        matchers = get_matchers(self.config)
        for index, word in enumerate(shown):
            # 双击要用这一行对应的词条去找笔记；以前这里忘了 append，
            # 双击等于没反应，顺手补上。
            self._detail_index.append(word)
            codes = [
                V.ALL_EXAM_LABELS.get(code, code) for code in word.get("exams", [])
            ] + [V.JLPT_LABELS.get(code, code) for code in word.get("levels", [])]
            note = word.get("meaning") or ""
            if not word.get("confident") and word.get("alternatives"):
                note = "一词多解（未覆盖），候选：" + "、".join(word["alternatives"][:4])
            self.detail_table.setItem(index, 0, _item(word["key"]))
            self.detail_table.setItem(index, 1, _item(word["surface"]))
            self.detail_table.setItem(
                index, 2, _item("英语" if word["language"] == "en" else "日语")
            )
            self.detail_table.setItem(index, 3, _item("、".join(codes) or "—"))
            state = word.get("state")
            self.detail_table.setItem(
                index, 4, _item(V.STATE_LABELS.get(state, "未覆盖"))
            )
            self.detail_table.setItem(index, 5, _item(word.get("cards", 0), True))
            self.detail_table.setItem(
                index, 6, _item("、".join(word.get("deck_names") or []))
            )
            # 界面只显示两态：已覆盖 / 未覆盖。内部细分类（识别失败/待确认/真未覆盖）
            # 只服务「不给其实有卡的词重复制卡」和探针断言。
            reason = analysis.display_reason(self.detail_reason(word, matchers))
            self.detail_table.setItem(index, 7, _item(reason))
            self.detail_table.setItem(index, 8, _item(note))
        self.detail_table.resizeColumnToContents(0)
        self.detail_table.resizeColumnToContents(1)
        self._update_tag_state(choice)

    def detail_reason(self, word: dict, matchers: dict) -> str:
        """明细页「原因」列：这一行到底算不算覆盖，不算的话是哪种情况。"""
        if word.get("reason"):
            return word["reason"]
        if word.get("exams") or word.get("levels"):
            return ""
        if not word.get("confident"):
            return analysis.GAP_PENDING
        language = word.get("language") or "en"
        in_list = analysis.in_word_list(matchers, language, word.get("key") or "")
        return analysis.entry_gap_reason(word, in_list)

    def render_settings(self):
        field_map = self.config.get("field_map") or {}
        rows = []
        for ntid, model in ((int(m["id"]), m) for m in self.col.models.all()):
            rows.append((ntid, model))
        rows.sort(key=lambda r: r[1]["name"])
        self.field_table.setRowCount(len(rows))
        self._field_widgets = {}
        for index, (ntid, model) in enumerate(rows):
            cfg = field_map.get(str(ntid)) or {}
            enabled = QCheckBox()
            enabled.setChecked(bool(cfg.get("enabled")))
            self.field_table.setCellWidget(index, 0, enabled)
            self.field_table.setItem(index, 1, _item(model["name"]))
            names = [f["name"] for f in model.get("flds", [])]

            first = QComboBox()
            first.addItem("（不统计）", "")
            for name in names:
                first.addItem(name, name)
            pick = cfg.get("fields") or []
            first.setCurrentIndex(max(0, first.findData(pick[0] if pick else "")))
            self.field_table.setCellWidget(index, 2, first)

            second = QComboBox()
            second.addItem("（无）", "")
            for name in names:
                second.addItem(name, name)
            fallback = cfg.get("reading_field") or (
                (cfg.get("fallback_fields") or [""])[0]
            )
            second.setCurrentIndex(max(0, second.findData(fallback)))
            self.field_table.setCellWidget(index, 3, second)

            language = QComboBox()
            for label, code in (("不统计", "none"), ("英语", "en"), ("日语", "ja")):
                language.addItem(label, code)
            language.setCurrentIndex(
                max(0, language.findData(cfg.get("language", "none")))
            )
            self.field_table.setCellWidget(index, 4, language)

            sentence = QCheckBox()
            sentence.setChecked(bool(cfg.get("allow_sentence")))
            self.field_table.setCellWidget(index, 5, sentence)

            self.field_table.setItem(index, 6, _item(_field_reason(cfg)))
            self._field_widgets[ntid] = (enabled, first, second, language, sentence)
        self.field_table.resizeColumnsToContents()
        meta = get_matchers(self.config).get("en_meta", {})
        jmeta = get_matchers(self.config).get("ja_meta", {})
        self.index_label.setText(
            f"词库索引构建时间：英文 {meta.get('built', '?')}（{meta.get('source', '')}，"
            f"{meta.get('license', '')}）；日语 {jmeta.get('built', '?')}（{jmeta.get('source', '')}，"
            f"{jmeta.get('license', '')}）。全部离线运行。"
        )
        if getattr(self, "update_label", None) is not None:
            self.update_label.setText(U.describe_state(_update_state(), __version__))
        # 设置页里还挂着「素材库状态」那一行，一起刷一遍。
        self.render_materials()

    # ---- 内置素材库
    def render_materials(self, describe: dict | None = None) -> None:
        """设置页那行「素材库状态」。文本素材内置，音频包按需下载。"""
        if getattr(self, "material_label", None) is None:
            return
        resources = get_resources(self.config)
        if describe is None:
            try:
                describe = resources.describe()
            except Exception:  # noqa: BLE001
                self.material_label.setText("素材库状态读取失败（不影响统计）。")
                return
        media = resources.media_installed()
        parts = [
            f"内置文本素材：英语 {describe.get('en_words', 0)} 词、"
            f"日语 {describe.get('ja_words', 0)} 词"
            "（单词 / 音标·假名 / 释义 / 例句，随插件一起打包）",
            (
                f"音频包：已解压 {media.get('files', 0)} 个文件"
                if media.get("installed")
                else "音频包：还没装（点「一键构建素材库」下载，或「从本地 zip 文件安装…」）"
            ),
            "音频目录：" + str(media.get("dir") or ""),
        ]
        if describe.get("errors"):
            parts.append(
                "读取报错：" + "；".join(f"{k}={v}" for k, v in describe["errors"].items())
            )
        self.material_label.setText("；".join(parts))

    def _materials_expect_sha(self, resources) -> str:
        """内置清单 / 配置里记的音频包 sha256（没有就返回空）。"""
        manifest = resources.manifest()
        return (
            str(self.config.get("materials_sha256") or "").strip()
            or str(manifest.get("sha256") or "")
        ).lower()

    def _materials_zip_start_dir(self) -> str:
        """本地 zip 选择框的起始目录：上次用的 → 桌面\\应试插件\\工具\\素材构建 → 桌面。"""
        last = str(self.config.get("materials_zip") or "").strip()
        if last:
            folder = last if os.path.isdir(last) else os.path.dirname(last)
            if folder and os.path.isdir(folder):
                return folder
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        for candidate in (
            os.path.join(desktop, "应试插件", "工具", "素材构建"),
            desktop,
        ):
            if os.path.isdir(candidate):
                return candidate
        return desktop

    def _make_media_progress(self, title: str) -> QProgressDialog:
        """音频包的进度条：先是不确定进度，拿到总量后切成百分比。"""
        progress = QProgressDialog("准备中…", "", 0, 0, self)
        progress.setWindowTitle(title)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setCancelButton(None)
        progress.show()
        return progress

    def _media_progress_cb(self, progress: QProgressDialog, state: dict, verb: str):
        """把「后台线程里的进度」安全地丢回主线程画到进度条上。"""

        def callback(done: int, total: int) -> None:
            if state.get("closed") or not total:
                return
            text = f"{verb}…{done} / {total}"

            def apply():
                progress.setLabelText(text)
                progress.setRange(0, total)
                progress.setValue(done)

            _run_on_main(apply)

        return callback

    def _finish_materials(self, result: dict, extra: str = "") -> None:
        self.config["materials_installed_at"] = int(time.time())
        save_config(self.config)
        self.render_materials()
        showInfo(
            f"素材库已就绪：解压了 {result.get('files', 0)} 个音频文件到\n"
            f"{result.get('dir', '')}\n\n"
            "以后生成补漏卡直接从这里取音频，不再联网。" + (extra or "")
        )

    def install_materials(self) -> None:
        """下载音频包 → 校验 sha256 → 解压到用户配置目录。只有这一步联网。"""
        resources = get_resources(self.config, force=True)
        manifest = resources.manifest()
        url = str(self.config.get("materials_source") or "").strip() or str(
            manifest.get("url") or U.materials_url()
        )
        expect = self._materials_expect_sha(resources)
        if not url:
            showWarning("没有可用的音频包下载地址（插件里也没有 materials_manifest.json）。")
            return
        if not askUser(
            "将从网上（GitHub）下载音频包并解压到本机：\n"
            f"{url}\n\n"
            "大小约 208 MB，只下载一次；之后生成补漏卡全部离线。\n"
            "如果总是下载失败，可以改用「从本地 zip 文件安装…」。\n\n"
            "要现在开始吗？"
        ):
            return

        progress = self._make_media_progress("一键构建素材库")
        state = {"closed": False}

        def on_bytes(done: int, total: int) -> None:
            if state["closed"]:
                return
            mb = 1024 * 1024
            if total:
                text = f"正在下载音频包…已下载 {done / mb:.1f} MB / 共 {total / mb:.1f} MB"
                percent = int(done * 100 / total)
            else:
                # 拿不到 Content-Length 时退回不确定进度，只报已下载多少
                text = f"正在下载音频包…已下载 {done / mb:.1f} MB（总大小未知）"
                percent = None

            def apply():
                progress.setLabelText(text)
                if percent is not None:
                    progress.setRange(0, 100)
                    progress.setValue(percent)

            _run_on_main(apply)

        def work() -> dict:
            import tempfile

            target = os.path.join(tempfile.gettempdir(), "exam_materials_audio.zip")
            U.download_to_file(
                url,
                target,
                timeout=U.MATERIALS_TIMEOUT,
                retries=U.MATERIALS_RETRIES,
                version=__version__,
                progress=on_bytes,
                resume=True,
            )
            if expect and resources.sha256_of(target) != expect:
                raise RuntimeError("下载到的音频包校验不过（sha256 不一致），已放弃。")
            return resources.install_media(
                target, progress=self._media_progress_cb(progress, state, "正在解压音频文件")
            )

        def done(future) -> None:
            state["closed"] = True
            progress.close()
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                showWarning(U.materials_failure_text([url], exc))
                return
            self._finish_materials(result)

        mw.taskman.run_in_background(work, done)

    def install_materials_from_zip(self) -> None:
        """不联网，直接从本机的 exam_materials_audio.zip 解压装素材库。"""
        resources = get_resources(self.config, force=True)
        expect = self._materials_expect_sha(resources)
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "选择音频包（exam_materials_audio.zip）",
            self._materials_zip_start_dir(),
            "音频包 (*.zip);;所有文件 (*)",
        )
        if not path:
            return
        if not os.path.isfile(path):
            showWarning("选到的文件不在了：" + path)
            return
        size_mb = os.path.getsize(path) / (1024 * 1024)
        target = resources.media_dir()
        if not askUser(
            "将从本机解压音频包（全程不联网）：\n"
            f"{path}\n"
            f"大小 {size_mb:.1f} MB\n\n"
            "解压目标：\n"
            f"{target}\n\n"
            "要现在开始吗？"
        ):
            return

        progress = self._make_media_progress("从本地 zip 安装素材库")
        progress.setLabelText("正在校验文件（sha256）…")
        state = {"closed": False}
        on_files = self._media_progress_cb(progress, state, "正在解压音频文件")

        def work() -> dict:
            digest = resources.sha256_of(path)
            if expect and digest != expect:
                # 校验不一致：先不装，回主线程问一句
                return {"ok": False, "digest": digest}
            result = resources.install_media(path, progress=on_files)
            result["ok"] = True
            return result

        def extract_anyway() -> None:
            """校验不一致、用户仍要装：再跑一次纯解压。"""
            progress.setLabelText("正在解压音频文件…")

            def work2() -> dict:
                return resources.install_media(path, progress=on_files)

            def done2(future) -> None:
                state["closed"] = True
                progress.close()
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001
                    showWarning(f"从本地 zip 安装失败：{exc}")
                    return
                self.config["materials_zip"] = path
                self._finish_materials(
                    result, "\n（注意：这个包和插件内置清单的 sha256 不一致）"
                )

            mw.taskman.run_in_background(work2, done2)

        def done(future) -> None:
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                state["closed"] = True
                progress.close()
                showWarning(f"从本地 zip 安装失败：{exc}")
                return
            state["closed"] = True
            progress.close()
            if result.get("ok"):
                self.config["materials_zip"] = path
                self._finish_materials(result)
                return
            if askUser(
                "这个文件的内容和插件内置清单的 sha256 对不上（可能是另一个版本的音频包）。\n"
                f"磁盘：{str(result.get('digest') or '')[:16]}…\n"
                f"清单：{(expect or '')[:16]}…\n\n"
                "仍然要解压安装吗？"
            ):
                state["closed"] = False
                extract_anyway()
            else:
                showInfo("已取消，没有改动任何文件。")

        mw.taskman.run_in_background(work, done)

    # ---- 设置动作
    def apply_settings(self):
        field_map = {}
        for ntid, widgets in self._field_widgets.items():
            enabled, first, second, language, sentence = widgets
            name = self.col.models.get(ntid)["name"] if self.col.models.get(ntid) else str(ntid)
            # 上一次自动识别的分数留着，设置页才能继续显示命中率；
            # reason 不留（那是自动判定的结论，用户已经手动改过了）。
            previous = (self.config.get("field_map") or {}).get(str(ntid)) or {}
            field_map[str(ntid)] = {
                "notetype_name": name,
                "enabled": enabled.isChecked(),
                "fields": [first.currentData()] if first.currentData() else [],
                "fallback_fields": [second.currentData()] if second.currentData() else [],
                "reading_field": second.currentData() or "",
                "language": language.currentData(),
                "allow_sentence": sentence.isChecked(),
                "score": previous.get("score", 0),
                "hit_rate": previous.get("hit_rate", 0),
                "samples": previous.get("samples", 0),
                "hits": previous.get("hits", 0),
                "looks_like_sentence": previous.get("looks_like_sentence", False),
                "preview": previous.get("preview", []),
                "status": "manual",
            }
        self.config["field_map"] = field_map
        exams = [code for code, box in self.exam_boxes.items() if box.isChecked() and code in V.ALL_EXAM_LABELS]
        levels = [code for code, box in self.exam_boxes.items() if box.isChecked() and code in V.JLPT_LABELS]
        self.config["enabled_exams"] = exams
        self.config["enabled_levels"] = levels
        self.config["lemma_merge"] = self.merge_box.isChecked()
        self.config["cache_enabled"] = self.cache_box.isChecked()
        self.config["update_check"] = self.update_box.isChecked()
        defaults = dict(DEFAULTS["builder_defaults"])
        defaults.update(self.config.get("builder_defaults") or {})
        defaults["limit"] = int(self.build_limit_box.value())
        defaults["new_per_day"] = int(self.build_new_box.value())
        defaults["add_tags"] = self.build_tag_box.isChecked()
        self.config["builder_defaults"] = defaults
        save_config(self.config)
        get_matchers(self.config, force=True)
        self.refresh(force=True)
        showInfo("设置已保存。")

    def redetect_fields(self):
        matchers = get_matchers(self.config)
        self.config["field_map"] = detect_fields(self.col, matchers)
        save_config(self.config)
        self.refresh(force=True)
        showInfo("已重新识别字段。自动结果是起点，建议在设置页核对一遍。")

    def clear_cache(self):
        removed = analysis.clear_cache(self.col)
        showInfo(f"已清除 {removed} 个缓存文件。")

    # ---- 明细动作
    def _selected_tag_codes(self) -> list:
        """勾选里能打标签的那些词表码（合并码不算）。"""
        return [
            code
            for _language, code in self.checked_detail_codes()
            if V.exam_tag_for_code(code)
        ]

    def selected_exam_tags(self) -> list:
        """当前勾选 -> 要打的标签名，去重且保持顺序。"""
        tags = []
        for code in self._selected_tag_codes():
            tag = V.exam_tag_for_code(code)
            if tag and tag not in tags:
                tags.append(tag)
        return tags

    def tag_target_note_ids(self) -> list:
        """当前明细里列出的词条涉及到的笔记 ID。"""
        nids = set()
        for word in self._detail_rows():
            for nid in word.get("note_ids") or ():
                nids.add(int(nid))
        return sorted(nids)

    def _update_tag_state(self, choice: str) -> None:
        tags = self.selected_exam_tags()
        usable = choice == "hit" and bool(tags)
        self.tag_button.setEnabled(usable)
        self.untag_button.setEnabled(usable)
        targets = self.builder_targets()
        self.build_button.setEnabled(choice == "uncovered" and bool(targets))
        if choice == "uncovered":
            if targets:
                self.tag_hint.setText(
                    "将只给「真未覆盖」的词建卡："
                    + "、".join(label for _l, _c, label in targets)
                    + "。素材库里已经有音频/释义/例句的词才建；「其实有卡、只是没认出来」的词不会重复制卡。"
                )
            else:
                self.tag_hint.setText("先在左边勾选至少一个具体词表，才能生成补漏牌组。")
        elif choice != "hit":
            self.tag_hint.setText("打标签只在「只看命中词表」这一档可用。")
        elif not tags:
            self.tag_hint.setText("先在左边勾选具体词表（并集那一行不打标签）。")
        else:
            self.tag_hint.setText("将按「" + "、".join(tags) + "」给当前列出的命中笔记打标签。")

    def builder_targets(self) -> list:
        """[(语言, 词表码, 显示名), …]：勾选项里能拿来建牌组的（合并码展开成成员）。"""
        out: list = []
        for language, code in self.checked_detail_codes():
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
            if language == "ja":
                label = V.JLPT_LABELS.get(code, code)
            else:
                label = V.ALL_EXAM_LABELS.get(code, code)
            item = (language, code, label)
            if item not in out:
                out.append(item)
        return out

    def add_exam_tags(self):
        self._write_exam_tags(remove=False)

    def remove_exam_tags(self):
        self._write_exam_tags(remove=True)

    def _write_exam_tags(self, remove: bool) -> None:
        tags = self.selected_exam_tags()
        if not tags:
            showInfo("请先在左边勾选至少一个具体词表（并集那一行不打标签）。")
            return
        nids = self.tag_target_note_ids()
        if not nids:
            showInfo("当前明细里没有可打标签的笔记。")
            return
        if not askUser(V.exam_tag_confirm_text(len(nids), tags, remove=remove)):
            return
        try:
            apply_exam_tags(self.col, nids, tags, remove=remove)
        except Exception as exc:
            showWarning(f"处理标签失败：{exc}")
            return
        self.refresh(force=True)
        showInfo(
            f"已给 {len(nids)} 条笔记{'去掉' if remove else '加上'}："
            + "、".join(tags)
            + ("\n\n（Ctrl+Z 可撤销）" if not remove else "")
        )

    def copy_words(self):
        words = [w for w in self._detail_rows()][:3000]
        text = "\n".join(w["key"] for w in words)
        mw.app.clipboard().setText(text)
        showInfo(f"已复制 {len(words)} 个词到剪贴板。")

    # ---- 明细动作：一键补漏制卡
    def builder_rows(self, targets) -> list:
        """把勾选的词表展开成待补的词条（每个词带着自己的词表码）。"""
        matchers = get_matchers(self.config)
        rows: list = []
        for language, code, label in targets or ():
            found = analysis.uncovered_words(matchers, self.summary, language, code)
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
        return rows

    def build_uncovered_deck(self):
        targets = self.builder_targets()
        if not targets:
            showInfo("请先在左边勾选至少一个具体词表（并集那一行会展开成它的成员）。")
            return
        rows = self.builder_rows(targets)
        if not rows:
            showInfo("勾选的词表在当前范围里没有「真未覆盖」的词。")
            return
        defaults = dict(DEFAULTS["builder_defaults"])
        defaults.update(self.config.get("builder_defaults") or {})
        options = {
            "limit": int(defaults.get("limit") or 500),
            "add_tags": bool(defaults.get("add_tags", True)),
            "new_per_day": int(defaults.get("new_per_day") or 0),
        }
        config = dict(self.config)
        progress = QProgressDialog(
            "正在从插件内置素材库里取素材…\n（不联网、不改你现有的卡片）",
            "",
            0,
            0,
            self,
        )
        progress.setWindowTitle("一键生成补漏牌组")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setCancelButton(None)
        progress.show()

        def work():
            resources = get_resources(config)
            return B.build_drafts(rows, resources, options)

        def done(future):
            progress.close()
            try:
                drafts, skipped = future.result()
            except Exception as exc:  # noqa: BLE001
                showWarning(f"找素材时出错：{exc}")
                return
            self.confirm_and_write(drafts, skipped, options)

        mw.taskman.run_in_background(work, done)

    def confirm_and_write(self, drafts, skipped, options) -> None:
        if not drafts:
            showInfo(
                "没有可生成的卡片："
                + ("；".join(f"{r['key']}（{r['reason']}）" for r in skipped[:8]) or "没有词")
            )
            return
        # 每张补漏卡都必须有音频。音频包还没解压到本机时，宁可先不落卡，
        # 也别生成一批「只有文字、没声音」的半截卡片。
        ready, not_ready = B.split_by_audio(drafts)
        if not_ready:
            installed = get_resources(dict(self.config)).media_installed()
            if not installed.get("installed"):
                if askUser(
                    f"这 {len(not_ready)} 张卡片还没有音频：音频包（约 208 MB）还没下载到本机。\n\n"
                    "你的要求是每张卡都必须带音频，所以现在不会落卡。\n"
                    "要现在下载并解压素材库吗？（下载完成后请再点一次「一键生成补漏牌组」）\n\n"
                    "下载总失败的话，去设置页点「从本地 zip 文件安装…」，"
                    "选中本机的 exam_materials_audio.zip 即可，全程不联网。"
                ):
                    self.install_materials()
                else:
                    showInfo(
                        "已取消，没有改动你的集合。\n"
                        "随时可以去设置页点「一键构建素材库」（联网下载）"
                        "或「从本地 zip 文件安装…」（不联网），装好后再回来生成。"
                    )
                return
            # 音频包装了，但个别词的音频在本机找不到：这些词直接跳过，不凑数。
            skipped = list(skipped) + [
                {"key": d.get("key") or "", "reason": "音频文件在本机缺失，已跳过"}
                for d in not_ready
            ]
            drafts = ready
            if not drafts:
                showInfo(
                    "这些词的音频文件在本机都找不到，已全部跳过。\n"
                    "点设置页的「一键构建素材库」下载，或点「从本地 zip 文件安装…」"
                    "选本机的 exam_materials_audio.zip（不联网），补齐音频后可以再试。"
                )
                return
        decks = sorted({d["deck"] for d in drafts if d.get("deck")})
        text = B.plan_text(drafts, skipped, decks) + "\n\n确定要生成吗？"
        if not askUser(text):
            return
        try:
            report = write_builder_cards(self.col, drafts, options)
        except Exception as exc:  # noqa: BLE001
            showWarning(f"生成失败：{exc}")
            return
        self.refresh(force=True)
        lines = [
            f"已新增 {report['notes']} 条笔记、{report['decks']} 个牌组、"
            f"{report['media']} 个音频文件。",
            "落点：" + "、".join(report["deck_names"]) if report["deck_names"] else "",
            "新牌组的新卡上限是 0，检查满意后在牌组选项里自己放开。",
            "Ctrl+Z 可以把这一整组撤销。",
        ]
        if report["errors"]:
            lines.append("有 {} 处没成功：{}".format(len(report["errors"]), "；".join(report["errors"][:5])))
        showInfo("\n".join(line for line in lines if line))

    def open_selected_in_browser(self, item):
        row = item.row()
        if row >= len(self._detail_index):
            return
        word = self._detail_index[row]
        nids = sorted(word.get("note_ids") or [])
        if not nids:
            return
        search = "nid:" + ",".join(str(n) for n in nids[:200])
        if not open_browser(search):
            showInfo("请在卡片浏览器里粘贴这个搜索式：\n" + search)

    def export_csv(self):
        path, _filter = QFileDialog.getSaveFileName(
            self, "导出统计结果", "应试词汇统计.csv", "CSV (*.csv)"
        )
        if not path:
            return
        words = self._detail_rows()
        matchers = get_matchers(self.config)
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(
                    [
                        "词元",
                        "原始写法",
                        "语言",
                        "命中词表",
                        "状态",
                        "原因",
                        "卡片数",
                        "来源牌组",
                        "备注",
                    ]
                )
                for word in words:
                    codes = [
                        V.ALL_EXAM_LABELS.get(code, code) for code in word.get("exams", [])
                    ] + [V.JLPT_LABELS.get(code, code) for code in word.get("levels", [])]
                    reason = self.detail_reason(word, matchers)
                    if not reason and (word.get("exams") or word.get("levels")):
                        reason = "已覆盖"
                    writer.writerow(
                        [
                            word["key"],
                            word["surface"],
                            "英语" if word["language"] == "en" else "日语",
                            "、".join(codes),
                            V.STATE_LABELS.get(word.get("state"), "未覆盖"),
                            reason,
                            word.get("cards", 0),
                            "、".join(word.get("deck_names") or []),
                            word.get("meaning", ""),
                        ]
                    )
        except OSError as exc:
            showWarning(f"写入失败：{exc}")
            return
        showInfo(f"已导出 {len(words)} 行到：\n{path}")

    # ---- 覆盖页补充：词表行的进度条已经画在表格里


def apply_exam_tags(col, nids, tags, remove: bool = False) -> int:
    """批量给笔记加/去应试标签，返回受影响的笔记数。

    故意不弹任何对话框：确认由界面负责，这样探针能在不打断流程的情况下
    直接验证这一段（标签操作在 Anki 后端是带撤销步骤的，Ctrl+Z 能撤）。
    """
    tags = [t for t in (tags or []) if t]
    nids = [int(n) for n in (nids or [])]
    if not tags or not nids:
        return 0
    text = " ".join(tags)
    if remove:
        col.tags.bulk_remove(nids, text)
    else:
        col.tags.bulk_add(nids, text)
    return len(nids)


# ---------------------------------------------------------------- 一键补漏制卡


# 上一次「自定义撤销步骤」的经过，探针和排查都读它（界面上不显示）
LAST_UNDO_INFO: dict = {}


def _note_request_class():
    """`col.add_notes()` 要用的请求类型；不同 Anki 版本放在不同模块，找不到就返回 None。"""
    for module_name in ("anki.collection", "anki.notes"):
        module = sys.modules.get(module_name)
        if module is None:  # pragma: no cover - Anki 里这两个模块一定已经加载
            continue
        maker = getattr(module, "AddNoteRequest", None)
        if maker is not None:
            return maker
    return None


def _begin_undo(col, name: str):
    """开一个自定义撤销步骤；旧版本没有这个接口就返回 None（照样能建卡）。"""
    starter = getattr(col, "add_custom_undo_entry", None)
    LAST_UNDO_INFO.clear()
    if not callable(starter):
        LAST_UNDO_INFO["begin"] = "这个版本没有 add_custom_undo_entry"
        return None
    try:
        token = starter(name)
        LAST_UNDO_INFO["token"] = repr(token)
        return token
    except Exception as exc:  # noqa: BLE001
        LAST_UNDO_INFO["begin_error"] = repr(exc)
        print(f"[应试词汇覆盖统计] 开撤销步骤失败（不影响制卡）：{exc}")
        return None


def _finish_undo(col, token) -> None:
    if token is None:
        LAST_UNDO_INFO["finish"] = "没有自定义撤销步骤，靠 Anki 自带的逐步撤销"
        return
    merger = getattr(col, "merge_undo_entries", None)
    if callable(merger):
        try:
            merger(token)
            LAST_UNDO_INFO["merged"] = repr(token)
            return
        except Exception as exc:  # noqa: BLE001
            LAST_UNDO_INFO["merge_error"] = repr(exc)
            print(f"[应试词汇覆盖统计] 合并撤销步骤失败（不影响制卡）：{exc}")
    try:
        col.save()
    except Exception:  # noqa: BLE001
        pass


def ensure_builder_notetype(col):
    """建（或取回）插件自带的「应试补漏卡」笔记类型。"""
    spec = B.notetype_spec()
    model = col.models.by_name(spec["name"])
    if model:
        return model
    model = col.models.new(spec["name"])
    for field in spec["fields"]:
        col.models.add_field(model, col.models.new_field(field))
    template = col.models.new_template(spec["templates"][0]["name"])
    template["qfmt"] = spec["templates"][0]["qfmt"]
    template["afmt"] = spec["templates"][0]["afmt"]
    col.models.add_template(model, template)
    model["css"] = spec["css"]
    col.models.save(model)
    return col.models.by_name(spec["name"]) or model


def _set_deck_new_limit(col, did: int, per_day: int) -> None:
    """给补漏牌组单独配一份「新卡上限」，不碰用户现有的牌组选项。

    多个牌组共用同一份选项时，直接改它会把用户别的牌组也一起改掉，所以这里
    新建一份专用配置再挂上去；任何一步不被这个版本支持就安静跳过。
    """
    try:
        deck = col.decks.get(did)
        if not deck:
            return
        adder = getattr(col.decks, "add_config_returning_id", None)
        cid = None
        if callable(adder):
            cid = adder(f"应试补漏（新卡上限 {per_day}）")
        conf = col.decks.get_config(cid) if cid else col.decks.config_dict_for_deck_id(did)
        conf.setdefault("new", {})["perDay"] = int(per_day)
        col.decks.update_config(conf)
        if cid:
            setter = getattr(col.decks, "set_config_id_for_deck_dict", None)
            if callable(setter):
                setter(deck, cid)
            else:
                deck["conf"] = cid
                col.decks.update(deck)
    except Exception as exc:  # noqa: BLE001
        print(f"[应试词汇覆盖统计] 设置新卡上限失败（不影响制卡）：{exc}")


def _add_media(col, path: str) -> str:
    """把音频写进 Anki 媒体库，返回入库后的文件名。"""
    if not path or not os.path.isfile(path):
        return ""
    manager = getattr(col, "media", None)
    add_file = getattr(manager, "add_file", None)
    if not callable(add_file):
        return ""
    return add_file(path) or ""


def _write_notes(col, pending: list, report: dict) -> None:
    """把攒好的卡整批写进 Anki。

    Anki 26 的 `col.add_notes([...])` 是一次后端调用，Ctrl+Z 一步就撤掉整批；
    老版本没有这个接口（或调用失败）时退回逐条 `add_note`，再用自定义撤销步骤
    把它们并成一步，保证「整组一键撤销」在任何版本都成立。
    """
    if not pending:
        return
    maker = _note_request_class()
    bulk = getattr(col, "add_notes", None)
    requests = None
    if callable(bulk) and maker is not None:
        try:
            requests = [maker(note, did) for _draft, note, did in pending]
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"组装批量写卡请求失败，改逐条写（{exc}）")
            requests = None
    if requests is not None:
        try:
            bulk(requests)
            report["notes"] += len(pending)
            LAST_UNDO_INFO["write"] = f"批量写 {len(pending)} 条（Anki 记成一个撤销步骤）"
            return
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"批量写卡失败，改逐条写（{exc}）")
    token = _begin_undo(col, "生成应试补漏卡")
    LAST_UNDO_INFO["write"] = f"逐条写 {len(pending)} 条（并成一个自定义撤销步骤）"
    for draft, note, did in pending:
        try:
            col.add_note(note, did)
            report["notes"] += 1
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"{draft.get('key')}（{exc}）")
    _finish_undo(col, token)


def write_builder_cards(col, drafts, options: dict | None = None) -> dict:
    """真正落地：建笔记类型 / 建牌组 / 写媒体 / 加笔记。

    故意不弹任何对话框，确认由界面负责——探针才能在无人值守的情况下验证
    这一段（整组操作放在一个自定义撤销步骤里，Ctrl+Z 一次撤掉）。
    """
    options = dict(options or {})
    add_tags = bool(options.get("add_tags", True))
    per_day = int(options.get("new_per_day") or 0)
    report = {"notes": 0, "decks": 0, "media": 0, "deck_names": [], "errors": []}
    if not drafts:
        return report

    LAST_UNDO_INFO.clear()
    model = ensure_builder_notetype(col)
    pending: list = []
    for deck_name, group in B.group_by_deck(drafts).items():
        try:
            did = col.decks.id_for_name(deck_name)
            created = False
            if not did:
                did = col.decks.id(deck_name)
                created = True
            if not did:
                report["errors"].append(f"建牌组失败：{deck_name}")
                continue
            if created:
                report["decks"] += 1
                report["deck_names"].append(deck_name)
                _set_deck_new_limit(col, did, per_day)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"建牌组失败：{deck_name}（{exc}）")
            continue
        for draft in group:
            try:
                audio = draft.get("audio") or ""
                if not audio and draft.get("audio_path"):
                    audio = _add_media(col, draft["audio_path"])
                    if audio:
                        report["media"] += 1
                draft["audio"] = audio
                note = col.new_note(model)
                for index, value in enumerate(B.note_fields(draft)):
                    if index < len(note.fields):
                        note.fields[index] = value
                if add_tags and draft.get("exam_tag"):
                    note.tags = [draft["exam_tag"]]
                pending.append((draft, note, int(did)))
            except Exception as exc:  # noqa: BLE001
                report["errors"].append(f"{draft.get('key')}（{exc}）")
    _write_notes(col, pending, report)
    try:
        mw.reset()
    except Exception:  # noqa: BLE001
        pass
    return report


def _field_reason(cfg: dict) -> str:
    """设置页「说明」列：应试词命中率（抽样几条/命中几条）+ 自动判定的理由。"""
    reason = cfg.get("reason") or ""
    if not cfg.get("score"):
        return reason
    samples = int(cfg.get("samples") or 0)
    hits = int(cfg.get("hits") or 0)
    rate = float(cfg.get("hit_rate") or 0) * 100
    if cfg.get("looks_like_sentence"):
        head = f"像例句/长文本，已跳过（抽样 {samples} 条，命中 {hits} 条）"
    elif samples:
        head = f"应试词命中率 {rate:.0f}%（抽样 {samples} 条，命中 {hits} 条）"
    else:
        head = f"应试词命中率 {rate:.0f}%"
    prefix = "上次自动识别：" if cfg.get("status") == "manual" else ""
    return f"{prefix}{head}，得分 {cfg.get('score')}。" + reason


def open_browser(search: str) -> bool:
    """在卡片浏览器里打开搜索结果。Anki 各版本构造参数不同，逐个试。"""
    attempts = []
    try:
        from aqt.browser import Browser

        attempts.append(lambda: Browser(mw, search=search))
        attempts.append(lambda: Browser(mw, mw, search=search))
        attempts.append(lambda: Browser(mw))
    except Exception:
        pass
    try:
        import aqt.dialogs

        attempts.append(lambda: aqt.dialogs.open("Browser", mw, search=search))
    except Exception:
        pass
    for attempt in attempts:
        try:
            attempt()
            return True
        except Exception:
            continue
    try:
        if hasattr(mw, "onBrowse"):
            mw.onBrowse()
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------- 范围对话框


class ScopeDialog(QDialog):
    def __init__(self, col, config, scope, parent=None):
        super().__init__(parent or mw)
        self.col = col
        self.scope = dict(scope)
        self.setWindowTitle("选择统计范围")
        self.resize(760, 640)
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("牌组（勾选要统计的牌组；子牌组按下面的开关决定是否一起算）"))
        deck_buttons = QHBoxLayout()
        for label, handler in (
            ("全选", self.select_all_decks),
            ("全不选", self.clear_all_decks),
            ("只选父牌组", self.select_top_decks),
        ):
            button = QPushButton(label)
            button.clicked.connect(handler)
            deck_buttons.addWidget(button)
        deck_buttons.addStretch(1)
        layout.addLayout(deck_buttons)
        self.deck_list = QListWidget()
        self.deck_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        deck_ids = self.scope.get("deck_ids") or []
        self.deck_items = {}
        self._deck_busy = False
        names_by_id = {}
        for deck in sorted(col.decks.all_names_and_ids(), key=lambda d: d.name):
            item = QListWidgetItem(deck.name)
            item.setData(Qt.ItemDataRole.UserRole, deck.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.deck_list.addItem(item)
            self.deck_items[item.text()] = item
            names_by_id[analysis.as_int(deck.id)] = item.text()
        # 存过的范围里勾父牌组时，「含子牌组」是靠开关生效的；这里把子牌组也一起
        # 勾上，视觉上和统计口径才是同一件事，用户点确定也不会把范围改小。
        self._deck_busy = True
        try:
            for raw in deck_ids:
                name = names_by_id.get(analysis.as_int(raw))
                if not name:
                    continue
                self.deck_items[name].setCheckState(Qt.CheckState.Checked)
                if self.scope.get("include_subdecks", True):
                    for child in self._children_of(name):
                        self.deck_items[child].setCheckState(Qt.CheckState.Checked)
        finally:
            self._deck_busy = False
        # 整行都能点：以前只有那个小方框能点，看着就像「选不上」。
        # 点在勾选框本身上的时候要让 Qt 自己处理，否则会连点两次等于没点。
        self.deck_list.itemClicked.connect(self._on_deck_clicked)
        self.deck_list.itemChanged.connect(self._on_deck_changed)
        layout.addWidget(self.deck_list, 2)
        self.subdeck_box = QCheckBox("包含子牌组")
        self.subdeck_box.setChecked(bool(self.scope.get("include_subdecks", True)))
        self.subdeck_box.stateChanged.connect(lambda *_a: self.update_deck_count())
        layout.addWidget(self.subdeck_box)
        self.deck_count_label = QLabel("")
        layout.addWidget(self.deck_count_label)
        # 初始化时回填父牌组的三态也要关掉联动，否则会顺着 itemChanged 把子牌组
        # 又改回去（父牌组半选时尤其明显）。
        self._deck_busy = True
        try:
            self.sync_parent_checks()
        finally:
            self._deck_busy = False
        self.update_deck_count()

        layout.addWidget(QLabel("标签（逗号分隔；留空表示不限）"))
        row = QHBoxLayout()
        self.tag_include = QLineEdit(", ".join(self.scope.get("tags_include") or []))
        self.tag_include.setPlaceholderText("包含这些标签，例如 English-CEFR::CEFR-A1")
        row.addWidget(self.tag_include)
        self.tag_mode = QComboBox()
        self.tag_mode.addItem("任一命中", "any")
        self.tag_mode.addItem("全部命中", "all")
        self.tag_mode.setCurrentIndex(
            max(0, self.tag_mode.findData(self.scope.get("tag_mode", "any")))
        )
        row.addWidget(self.tag_mode)
        layout.addLayout(row)
        row2 = QHBoxLayout()
        self.tag_exclude = QLineEdit(", ".join(self.scope.get("tags_exclude") or []))
        self.tag_exclude.setPlaceholderText("排除这些标签（含子标签）")
        row2.addWidget(self.tag_exclude)
        layout.addLayout(row2)

        layout.addWidget(QLabel("状态（按词的最高进度状态判定）"))
        self.state_boxes = {}
        row3 = QHBoxLayout()
        for code in V.STATE_ORDER:
            box = QCheckBox(V.STATE_LABELS[code])
            box.setChecked(code in (self.scope.get("states") or []))
            self.state_boxes[code] = box
            row3.addWidget(box)
        row3.addStretch(1)
        layout.addLayout(row3)

        layout.addWidget(QLabel("笔记类型（不勾选表示不限）"))
        self.notetype_list = QListWidget()
        self.notetype_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        selected = set(self.scope.get("notetype_ids") or [])
        for model in sorted(col.models.all(), key=lambda m: m["name"]):
            item = QListWidgetItem(model["name"])
            item.setData(Qt.ItemDataRole.UserRole, int(model["id"]))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if int(model["id"]) in selected
                else Qt.CheckState.Unchecked
            )
            self.notetype_list.addItem(item)
        layout.addWidget(self.notetype_list, 1)

        layout.addWidget(QLabel("Anki 搜索式（可留空；用于跟卡片浏览器的筛选结果对齐）"))
        self.search_edit = QLineEdit(self.scope.get("search") or "")
        layout.addWidget(self.search_edit)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        ok = QPushButton("确定")
        ok.clicked.connect(self.accept)
        buttons.addWidget(ok)
        layout.addLayout(buttons)

    # ---- 牌组树的父子联动
    def _children_of(self, name: str) -> list:
        prefix = name + "::"
        return [other for other in self.deck_items if other.startswith(prefix)]

    @staticmethod
    def _parent_of(name: str) -> str:
        return name.rsplit("::", 1)[0] if "::" in name else ""

    def _set_all(self, state) -> None:
        self._deck_busy = True
        try:
            for item in self.deck_items.values():
                item.setCheckState(state)
        finally:
            self._deck_busy = False
        self.update_deck_count()

    def select_all_decks(self):
        self._set_all(Qt.CheckState.Checked)

    def clear_all_decks(self):
        self._set_all(Qt.CheckState.Unchecked)

    def select_top_decks(self):
        """只勾最外层牌组（带动所有子牌组）。"""
        self._deck_busy = True
        try:
            for name, item in self.deck_items.items():
                item.setCheckState(
                    Qt.CheckState.Unchecked
                    if "::" in name
                    else Qt.CheckState.Checked
                )
        finally:
            self._deck_busy = False
        self.update_deck_count()

    def _on_deck_clicked(self, item):
        """点整行切换勾选；点在勾选框上时让 Qt 自己处理，避免连点两次。"""
        try:
            from aqt.qt import QCursor

            pos = self.deck_list.viewport().mapFromGlobal(QCursor.pos())
            rect = self.deck_list.visualItemRect(item)
            if rect.isValid() and 0 <= pos.y() - rect.y() < rect.height() and pos.x() - rect.x() < 26:
                return
        except Exception:  # noqa: BLE001
            pass
        if item.checkState() == Qt.CheckState.Checked:
            item.setCheckState(Qt.CheckState.Unchecked)
        else:
            item.setCheckState(Qt.CheckState.Checked)

    def _on_deck_changed(self, item):
        if self._deck_busy:
            return
        self._deck_busy = True
        try:
            name = item.text()
            state = item.checkState()
            if state != Qt.CheckState.PartiallyChecked:
                for child in self._children_of(name):
                    self.deck_items[child].setCheckState(state)
            self.sync_parent_checks(name)
        finally:
            self._deck_busy = False
        self.update_deck_count()

    def sync_parent_checks(self, start: str = "") -> None:
        """按子牌组的状态回填父牌组：全选 → 勾上，全不选 → 去掉，混着 → 半选。"""
        name = self._parent_of(start) if start else ""
        if not start:
            parents = sorted(
                {self._parent_of(n) for n in self.deck_items if self._parent_of(n)},
                key=lambda n: -n.count("::"),
            )
        else:
            parents = []
            while name:
                parents.append(name)
                name = self._parent_of(name)
        for parent in parents:
            item = self.deck_items.get(parent)
            if item is None:
                continue
            children = self._children_of(parent)
            if not children:
                continue
            states = [self.deck_items[c].checkState() for c in children]
            if all(s == Qt.CheckState.Checked for s in states):
                item.setCheckState(Qt.CheckState.Checked)
            elif all(s == Qt.CheckState.Unchecked for s in states):
                item.setCheckState(Qt.CheckState.Unchecked)
            else:
                item.setCheckState(Qt.CheckState.PartiallyChecked)

    def update_deck_count(self) -> None:
        checked = [
            name
            for name, item in self.deck_items.items()
            if item.checkState() == Qt.CheckState.Checked
        ]
        if self.subdeck_box.isChecked():
            expanded = set()
            for name in checked:
                expanded.add(name)
                expanded.update(self._children_of(name))
        else:
            expanded = set(checked)
        self.deck_count_label.setText(
            f"已选 {len(checked)} 个牌组（含子牌组共 {len(expanded)} 个）"
        )

    def result_scope(self) -> dict:
        decks = [
            self.deck_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.deck_list.count())
            if self.deck_list.item(i).checkState() == Qt.CheckState.Checked
        ]
        notetypes = [
            self.notetype_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.notetype_list.count())
            if self.notetype_list.item(i).checkState() == Qt.CheckState.Checked
        ]
        return {
            "deck_ids": decks,
            "include_subdecks": self.subdeck_box.isChecked(),
            "tags_include": _split_tags(self.tag_include.text()),
            "tags_exclude": _split_tags(self.tag_exclude.text()),
            "tag_mode": self.tag_mode.currentData(),
            "notetype_ids": notetypes,
            "states": [code for code, box in self.state_boxes.items() if box.isChecked()],
            "search": self.search_edit.text().strip(),
        }


def _split_tags(text: str) -> list:
    parts = []
    for chunk in (text or "").replace("，", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return parts


# ---------------------------------------------------------------- 入口


def open_stats(scope_override: dict | None = None) -> None:
    if mw.col is None:
        showWarning("还没有打开集合。")
        return
    config = load_config()
    matchers = get_matchers(config)
    if ensure_field_map(mw.col, config, matchers):
        save_config(config)
    scope = dict(config.get("scope") or DEFAULTS["scope"])
    if scope_override:
        scope.update(scope_override)
    dialog = StatsDialog(mw.col, config, scope, mw)
    dialog.refresh()
    dialog.show()
    dialog.exec()


# 本次实际挂上了哪些钩子。Anki 各版本的钩子名会变（比如 26.9 里
# 「牌组列表的菜单」叫 deck_browser_will_show_options_menu，
# 根本没有 deck_browser_will_show_context_menu），所以这里逐个探测着挂，
# 挂不上就跳过——绝不能在导入期抛异常，那样整个插件都加载不了。
HOOK_REGISTRY: dict = {}


# --------------------------------------------------------------------------
# 从 GitHub 检查 / 安装更新（公开仓库，只读两个文件，不采集任何卡片内容）
# --------------------------------------------------------------------------


def update_state_path() -> str:
    """更新状态放用户配置目录，不往 addons21 里写东西。"""
    base = ""
    try:
        base = mw.pm.profileFolder() or ""
    except Exception:
        base = ""
    if not base:
        base = ADDON_DIR
    path = os.path.join(base, USER_DIR_NAME)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return os.path.join(ADDON_DIR, "update.json")
    return os.path.join(path, "update.json")


def _update_state() -> dict:
    """读更新状态；文件坏了当空的，绝不让它把插件拖挂。"""
    try:
        with open(update_state_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_update_state(state: dict) -> None:
    try:
        with open(update_state_path(), "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
    except OSError as exc:
        print(f"[应试词汇覆盖统计] 记录更新状态失败：{exc}")


def disk_version() -> str:
    """磁盘上这份插件现在是哪一版（直接读文件，不导入）。

    运行中的代码是启动那一刻载入内存的：用户「装好了但还没重启」时，
    磁盘上的版本会比运行中的新。检查更新要靠它分辨这种情况，别再催着下载。
    """
    try:
        with open(os.path.join(ADDON_DIR, "__init__.py"), "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return ""
    return U.parse_version(text)


def _record_update_check(latest: str = "", error: str = "") -> dict:
    """记一笔「查过了」。

    注意**失败也要记时间**：否则仓库还没建好时，每次启动都会重试一遍网络，
    白白拖慢启动还刷日志。一天最多查一次，成功失败都算。
    """
    state = _update_state()
    state["checked_at"] = int(time.time())
    state["latest"] = latest or ""
    state["running"] = __version__
    state["on_disk"] = disk_version()
    state["repo"] = U.UPDATE_REPO
    state["error"] = error or ""
    _save_update_state(state)
    return state


def download_and_install_update() -> bool:
    """下载最新的 .ankiaddon 并交给 Anki 自己装。

    Anki 26.9 的接口是 AddonManager.install(file, manifest=None, force_enable=False)，
    成功返回 InstallOk，失败返回 InstallError（靠类型名区分）。
    """
    import tempfile

    data = U.fetch_package(version=__version__)
    target = os.path.join(tempfile.gettempdir(), "exam_vocab_stats_update.ankiaddon")
    with open(target, "wb") as fh:
        fh.write(data)

    manager = getattr(mw, "addonManager", None)
    install = getattr(manager, "install", None)
    result = None
    if callable(install):
        try:
            result = install(target)
        except Exception as exc:
            print(f"[应试词汇覆盖统计] 调用 addonManager.install 失败：{exc}")
            result = exc
    if (
        result is not None
        and "Error" not in type(result).__name__
        and not isinstance(result, Exception)
    ):
        return True

    detail = ""
    if result is not None:
        for attr in ("error", "message", "text", "reason"):
            value = getattr(result, attr, None)
            if value:
                detail = f"\n\n{value}"
                break
    showInfo(
        "应试词汇覆盖统计：新版已经下载好了，但自动安装没成功。\n\n"
        f"文件在：{target}\n\n"
        "请用「工具 → 插件 → 从文件安装」选它，然后重启 Anki。" + detail
    )
    return False


def check_for_update(silent: bool = False) -> None:
    """检查 GitHub 上的最新版本；silent=True 时只在有新版本时吭声。"""
    if not U.repo_ready():
        if not silent:
            showInfo("应试词汇覆盖统计：还没配置更新地址。")
        return

    def work() -> str:
        return U.fetch_latest_version(version=__version__)

    def done(future) -> None:
        try:
            latest = future.result()
        except Exception as exc:
            _record_update_check(error=str(exc))
            if not silent:
                showInfo(f"应试词汇覆盖统计：检查更新失败。\n{exc}")
            return
        if not latest:
            _record_update_check(error="拿不到线上版本号")
            if not silent:
                showInfo("应试词汇覆盖统计：检查更新失败（拿不到线上版本号）。")
            return
        state = _record_update_check(latest=latest)
        if not U.is_newer(latest, __version__):
            if not silent:
                tooltip(f"应试词汇覆盖统计：已经是最新版本（{__version__}）")
            return
        on_disk = disk_version()
        # 「装好了但还没重启」：磁盘上那份已经不比线上旧（或上次装的就是这版）→ 只提醒重启
        disk_is_current = bool(on_disk) and not U.is_newer(latest, on_disk)
        if disk_is_current or state.get("installed") == latest:
            tooltip(U.pending_restart_text(on_disk or latest, __version__))
            return
        if not askUser(U.update_prompt_text(latest, __version__)):
            return
        try:
            if download_and_install_update():
                state = _update_state()
                state["installed"] = latest
                _save_update_state(state)
                tooltip(
                    f"应试词汇覆盖统计：已更新到 {latest}，请重启 Anki"
                )
        except Exception as exc:
            showInfo(f"应试词汇覆盖统计：下载更新失败\n{exc}")

    taskman = getattr(mw, "taskman", None)
    if taskman is not None and hasattr(taskman, "run_in_background"):
        taskman.run_in_background(work, done)
        return

    # 兜底：没有 taskman 就直接跑（会卡一下，但能用）
    class _Done:
        def __init__(self, value=None, error=None) -> None:
            self._value = value
            self._error = error

        def result(self):
            if self._error is not None:
                raise self._error
            return self._value

    try:
        value, error = work(), None
    except Exception as exc:  # pragma: no cover  只在不支持后台任务的旧版上走到
        value, error = None, exc
    done(_Done(value, error))


def maybe_check_update_on_start() -> None:
    """启动时悄悄查一次（默认一天最多一次）。"""
    try:
        if not bool(load_config().get("update_check", True)):
            return
    except Exception:
        return
    if not U.repo_ready():
        return
    state = _update_state()
    try:
        last = float(state.get("checked_at") or 0)
    except (TypeError, ValueError):
        last = 0.0
    if time.time() - last < U.UPDATE_INTERVAL_SECONDS:
        return
    check_for_update(silent=True)


def _on_profile_did_open(*_args) -> None:
    """等界面稳下来再查，别跟启动抢资源。"""
    try:
        QTimer.singleShot(8000, maybe_check_update_on_start)
    except Exception:
        pass


def _install_update_hook() -> None:
    hook = getattr(gui_hooks, "profile_did_open", None)
    if hook is None or not hasattr(hook, "append"):
        HOOK_REGISTRY["profile_did_open"] = False
        return
    try:
        hook.append(HG.guard(_on_profile_did_open))
        HOOK_REGISTRY["profile_did_open"] = True
    except Exception:
        HOOK_REGISTRY["profile_did_open"] = False


def _add_hook(name: str, callback) -> bool:
    """注册一个 gui_hooks 回调。

    回调统一用 ``hook_guard.guard`` 包一层：Anki 改了某个钩子的参数个数时，
    我们最多是「这个入口不出现」，不会再像 26.09 的 ``browser_menus_did_init``
    那样直接把卡片浏览器搞崩。回调**内部**真正的报错照旧往上抛。
    """
    hook = getattr(gui_hooks, name, None)
    if hook is None or not hasattr(hook, "append"):
        HOOK_REGISTRY[name] = False
        return False
    try:
        hook.append(HG.guard(callback))
    except Exception:
        HOOK_REGISTRY[name] = False
        return False
    HOOK_REGISTRY[name] = True
    return True


def _deck_scope(did) -> dict:
    """统计某个牌组（含子牌组）时用的范围。"""
    return {
        "deck_ids": [int(did)],
        "include_subdecks": True,
        "tags_include": [],
        "tags_exclude": [],
        "notetype_ids": [],
        "states": ["new", "learn", "review", "suspended"],
        "search": "",
    }


def _tag_scope(tag: str) -> dict:
    """统计某个标签时用的范围（标签含子标签，牌组不限）。"""
    return {
        "deck_ids": [],
        "include_subdecks": True,
        "tags_include": [tag] if tag else [],
        "tags_exclude": [],
        "tag_mode": "any",
        "notetype_ids": [],
        "states": ["new", "learn", "review", "suspended"],
        "search": "",
    }


def _menu_action(menu, label: str, callback):
    action = QAction(label, menu)
    action.triggered.connect(callback)
    menu.addAction(action)
    return action


def _looks_like_menu(obj) -> bool:
    """是不是一个 QMenu（只认方法形状，不用 import Qt 类型，方便离线测）。"""
    return callable(getattr(obj, "addAction", None)) and callable(getattr(obj, "actions", None))


def _looks_like_browser(obj) -> bool:
    """是不是卡片浏览器窗口（它有 ``form`` 和 ``col``，QMenu 没有）。"""
    return hasattr(obj, "form") and hasattr(obj, "col")


def _deck_browser_menu(menu, deck_id):
    """牌组列表里对着某个牌组弹出的菜单（含齿轮里的选项菜单）。"""
    # 正常签名是 (menu: QMenu, deck_id: int)；万一 Anki 哪天把参数换序，自动对调一次
    if _looks_like_menu(deck_id) and not _looks_like_menu(menu):
        menu, deck_id = deck_id, menu
    if not _looks_like_menu(menu):
        return
    if not deck_id:
        try:
            deck_id = mw.col.decks.get_current_id()
        except Exception:
            deck_id = None
    if not deck_id:
        return
    _menu_action(
        menu,
        "统计这个牌组的应试词汇…",
        lambda: open_stats(_deck_scope(deck_id)),
    )


def _browser_context_menu(browser, menu):
    """卡片浏览器表格区的右键菜单：统计当前筛选结果。"""
    # 正常签名是 (browser, menu)；参数换序时自动对调，对不上就跳过
    if _looks_like_menu(browser) and _looks_like_browser(menu):
        browser, menu = menu, browser
    if not _looks_like_menu(menu):
        return

    def run():
        search = _browser_search(browser)
        scope = _tag_scope("")
        scope["search"] = search
        open_stats(scope)

    _menu_action(menu, "统计当前筛选结果的应试词汇…", run)


def _browser_search(browser) -> str:
    for getter in ("current_search", "search"):
        value = getattr(browser, getter, None)
        try:
            text = value() if callable(value) else value
        except Exception:
            text = ""
        if text:
            return text
    return ""


def _sidebar_context_menu(sidebar, menu, item, index):
    """卡片浏览器左侧栏：牌组 / 标签上点右键。"""
    kind = (getattr(getattr(item, "item_type", None), "name", "") or "").lower()
    node = getattr(item, "search_node", None)
    label = getattr(item, "full_name", "") or getattr(item, "name", "") or ""
    if kind == "deck":
        did = getattr(node, "deck_id", None)
        if not did:
            return
        _menu_action(
            menu,
            "统计这个牌组的应试词汇…",
            lambda: open_stats(_deck_scope(did)),
        )
    elif kind == "tag":
        tag = getattr(node, "tag", None) or label
        if not tag:
            return
        _menu_action(
            menu,
            "统计这个标签的应试词汇…",
            lambda: open_stats(_tag_scope(tag)),
        )


BROWSER_MENU_TITLE = "应试词汇"
BROWSER_MENU_OBJECT = "exam_vocab_stats_menu"
# 上一次往浏览器菜单栏挂菜单时的诊断信息（探针用；正常运行时一直是初始值）
BROWSER_MENU_INFO: dict = {"attempts": 0, "added": 0, "error": "", "bar": ""}
# 挂上去的 QMenu 留个引用（PyQt 的对象生命周期在 C++ 那边，留一份更保险）
_BROWSER_MENUS: list = []


def _menubar_of(browser):
    """拿到卡片浏览器的菜单栏：先走 ``form.menubar``，再退回 ``menuBar()``。"""
    form = getattr(browser, "form", None)
    bar = getattr(form, "menubar", None)
    if bar is not None:
        return bar
    getter = getattr(browser, "menuBar", None)
    if callable(getter):
        try:
            return getter()
        except Exception:  # noqa: BLE001
            return None
    return None


def _find_menu(bar, object_name: str):
    """在菜单栏里找已经挂过的同名菜单（防重复叠加）。"""
    try:
        for action in bar.actions():
            menu = action.menu()
            if menu is not None and menu.objectName() == object_name:
                return menu
    except Exception:  # noqa: BLE001
        return None
    return None


def _add_browser_menu(browser) -> None:
    """真正往某个浏览器窗口的菜单栏挂「应试词汇」（幂等，重复调用不叠加）。"""
    BROWSER_MENU_INFO["attempts"] = int(BROWSER_MENU_INFO.get("attempts", 0)) + 1
    bar = _menubar_of(browser)
    if bar is None:
        BROWSER_MENU_INFO["error"] = "拿不到菜单栏"
        return
    BROWSER_MENU_INFO["bar"] = type(bar).__name__
    if _find_menu(bar, BROWSER_MENU_OBJECT) is not None:
        return  # 已经有了，不叠加第二份
    # 父对象给菜单栏：不然这个 QMenu 的 Python 包装一被回收，Qt 那边的菜单
    # 也会跟着没（PyQt 的所有权陷阱，实测菜单会「加进去又消失」）。
    menu = QMenu(BROWSER_MENU_TITLE, bar)
    menu.setObjectName(BROWSER_MENU_OBJECT)

    def run():
        scope = _tag_scope("")
        scope["search"] = _browser_search(browser)
        open_stats(scope)

    _menu_action(menu, "统计当前筛选结果的应试词汇…", run)
    try:
        bar.addMenu(menu)
    except Exception as exc:  # noqa: BLE001
        BROWSER_MENU_INFO["error"] = f"{type(exc).__name__}: {exc}"
        print(f"[应试词汇覆盖统计] 往卡片浏览器菜单栏加菜单失败：{type(exc).__name__}: {exc}")
        return
    BROWSER_MENU_INFO["added"] = int(BROWSER_MENU_INFO.get("added", 0)) + 1
    try:
        BROWSER_MENU_INFO["last"] = {
            "browser_id": id(browser),
            "bar_id": id(bar),
            "bar_is_form": bar is getattr(getattr(browser, "form", None), "menubar", None),
            "titles_after": [action.text() for action in bar.actions()],
            "menu_id": id(menu),
            "menu_parent_is_bar": menu.parent() is bar,
        }
    except Exception:  # noqa: BLE001
        pass
    # 除了给父对象，这里再留一份引用兜底；只留最近几份，免得开一次浏览器涨一点
    _BROWSER_MENUS.append(menu)
    del _BROWSER_MENUS[:-8]


def _browser_menu(browser, *args):
    """卡片浏览器主菜单栏：加一个顶级菜单「应试词汇」。

    注意：Anki 26.09 的 ``browser_menus_did_init`` 只传 ``browser`` 一个参数
    （旧代码按 ``(browser, menu)`` 挂，一开浏览器就 TypeError、窗口打不开），
    所以这里签名写成 ``(browser, *args)``，多传参数也不会出问题；菜单栏拿不到
    就静默跳过，绝不往外抛。

    另一个坑（26.09 实测）：这个钩子在 ``setupMenus()`` 里触发得很早，而它后面
    还会把菜单栏重建一遍，所以「当场加」的菜单会被冲掉。这里先当场加一次
    （兼容钩子触发时菜单栏已就绪的版本），再延到事件循环下一轮补一次，
    两边都靠 objectName 去重，不会出现两份。
    """

    def safe_add() -> None:
        try:
            _add_browser_menu(browser)
        except Exception as exc:  # noqa: BLE001
            BROWSER_MENU_INFO["error"] = f"{type(exc).__name__}: {exc}"
            print(f"[应试词汇覆盖统计] 补挂浏览器菜单失败：{type(exc).__name__}: {exc}")

    safe_add()
    try:
        QTimer.singleShot(0, safe_add)
    except Exception:  # noqa: BLE001
        pass


def setup_menu():
    action = QAction(MENU_LABEL, mw)
    action.triggered.connect(lambda: open_stats())
    mw.form.menuTools.addAction(action)
    HOOK_REGISTRY["tools_menu"] = True

    # 「检查更新」只在设置页里（启动静默检查 + 设置页按钮），工具菜单不再放一份，
    # 免得菜单越堆越长。
    # 顺序：新版本名字在前，旧版本名字在后，哪个存在挂哪个
    _add_hook("deck_browser_will_show_options_menu", _deck_browser_menu)
    # Anki 26.09 已经删掉了 deck_browser_will_show_context_menu 这个钩子，
    # 所以「牌组列表右键」没有入口可挂；那一行留在这里只是向前兼容的探测，
    # 返回 False。牌组相关入口由「齿轮/选项菜单」+ 工具菜单承担。
    _add_hook("deck_browser_will_show_context_menu", _deck_browser_menu)
    _add_hook("browser_will_show_context_menu", _browser_context_menu)
    _add_hook("browser_menus_did_init", _browser_menu)
    _add_hook("browser_sidebar_will_show_context_menu", _sidebar_context_menu)
    _install_update_hook()


setup_menu()
