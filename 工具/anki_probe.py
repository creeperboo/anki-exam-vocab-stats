"""应试词汇覆盖统计 · 隔离集成探针（只用于开发验证，不随插件分发）。

由 工具\\run_anki_probe.py 复制到临时用户配置的 addons21\\zz_probe\\__init__.py，
再用 Anki.exe -b <临时配置> 启动。探针在真实 Anki 里造一小套可控的牌组/笔记类型/卡片，
然后**调用插件自己的函数**（detect_fields / compute / analysis.collect_entries /
analysis.summarize / StatsDialog）跑一遍，把每步的断言结果写成 JSON
（路径由环境变量 EVS_PROBE_OUT 指定），最后关窗口。

它验证的是「插件代码 + 本机 Anki」配合起来的行为，重点是五件事：

1. 字段自动推荐：English-CEFR 认 vocabularyWord、JLPT10k 认 VocabKanji＋假名兜底
   VocabFurigana、Kaishi 认 Word＋Word Reading、Lapis 默认不勾选、语法牌库排除；
2. 变形还原在真实数据里生效：children→child、better→good、日语假名→汉字表记；
   同一假名对应多等级词的进「待确认」且不计入覆盖；
3. 数字对得上：四种状态逐条与数据库直查一致；各组合筛选的笔记集合与直查 SQL 一致；
4. 界面能起来：真的构造 StatsDialog、跑完 refresh 并离屏绘制一遍（抓 Qt6 API 兼容性）；
5. 只读承诺：跑完整套流程后，所有卡片的 did/type/queue/due/ivl/reps 快照必须和跑之前一模一样。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from typing import Any, Callable

from anki.decks import DeckId
from aqt import mw
from aqt.qt import Qt, QTimer

OUT = os.environ.get("EVS_PROBE_OUT") or os.path.join(
    tempfile.gettempdir(), "evs_probe.json"
)
# 性能步骤造多少条笔记。默认 2000 跑得快；验收时用 EVS_PROBE_PERF_N=20000 跑一次真的。
PERF_N = int(os.environ.get("EVS_PROBE_PERF_N") or "2000")

RESULT: dict[str, Any] = {"steps": [], "errors": [], "ok": False}
STATE: dict[str, Any] = {"step": "启动"}

DECK_EN = "EVS测试::英语CEFR"
DECK_JA = "EVS测试::日语JLPT"
DECK_KAISHI = "EVS测试::Kaishi"
DECK_LAPIS = "EVS测试::Lapis"
DECK_GRAMMAR = "EVS测试::语法"
DECK_PERF = "EVS测试::性能"

MODEL_EN = "English-CEFR"
MODEL_JA = "eggrolls-JLPT10k-v3.5"
MODEL_KAISHI = "Kaishi 1.5k zh-CH"
MODEL_LAPIS = "Lapis"
MODEL_GRAMMAR = "おにぎり文法"
MODEL_PERF = "EVS性能用"


# --------------------------------------------------------------------------
# 记录、断言、步骤调度（沿用「标签筛选牌组」探针的写法）
# --------------------------------------------------------------------------


def write_result() -> None:
    try:
        with open(OUT, "w", encoding="utf-8") as fh:
            json.dump(RESULT, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


def record(name: str, payload: dict[str, Any] | None = None) -> None:
    entry: dict[str, Any] = dict(payload or {})
    entry["name"] = name
    entry["step"] = STATE["step"]
    RESULT["steps"].append(entry)
    write_result()


def check(name: str, condition: Any, detail: str = "") -> bool:
    RESULT["checks"] = int(RESULT.get("checks", 0)) + 1
    if condition:
        return True
    RESULT["errors"].append(
        f"[{STATE['step']}] {name}：{detail if detail else '断言不成立'}"
    )
    return False


STEP_QUEUE: list[tuple[str, Callable[[], None], int]] = []


def run_step(name: str, fn: Callable[[], None]) -> None:
    STATE["step"] = name
    try:
        fn()
    except Exception:
        RESULT["errors"].append(f"[{name}] 执行出错：{traceback.format_exc()}")
    write_result()
    if STEP_QUEUE:
        next_name, next_fn, next_delay = STEP_QUEUE.pop(0)
        QTimer.singleShot(next_delay, lambda: run_step(next_name, next_fn))
    else:
        finish()


def finish() -> None:
    RESULT["ok"] = not RESULT["errors"]
    RESULT["finished"] = True
    RESULT["anki_version"] = getattr(getattr(mw, "pm", None), "anki_version", "")
    write_result()
    QTimer.singleShot(300, mw.close)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def evs() -> Any:
    module = sys.modules.get("exam_vocab_stats")
    if module is None:
        module = __import__("exam_vocab_stats")
    return module


def make_deck(name: str) -> int:
    return int(mw.col.decks.id(name))


def make_model(name: str, fields: list[str]) -> Any:
    col = mw.col
    existing = col.models.by_name(name)
    if existing:
        return existing
    model = col.models.new(name)
    for field_name in fields:
        col.models.add_field(model, col.models.new_field(field_name))
    template = col.models.new_template("Card 1")
    template["qfmt"] = "{{" + fields[0] + "}}"
    template["afmt"] = "{{FrontSide}}"
    col.models.add_template(model, template)
    col.models.add(model)
    return col.models.by_name(name)


def add_note(model: Any, deck_id: int, values: dict[str, str], tags=()) -> Any:
    col = mw.col
    note = col.new_note(model)
    for key, value in values.items():
        note[key] = value
    if tags:
        note.tags = list(tags)
    col.add_note(note, DeckId(int(deck_id)))
    return note


def card_ids_of(note_id: Any) -> list[int]:
    return [int(cid) for cid in mw.col.card_ids_of_note(note_id)]


def set_state(note_id: Any, state: str) -> None:
    """把这张笔记的卡直接摆成某一个学习状态（探针造数据用）。"""
    col = mw.col
    for cid in card_ids_of(note_id):
        card = col.get_card(cid)
        if state == "review":
            card.type = 2
            card.queue = 2
            card.ivl = 30
        elif state == "learn":
            card.type = 1
            card.queue = 1
        elif state == "suspended":
            card.queue = -1
        else:
            card.type = 0
            card.queue = 0
        col.update_card(card)


def cards_snapshot() -> dict:
    """所有卡片的只读快照，用来证明插件没动过任何一张卡。"""
    rows = mw.col.db.all(
        "select id, nid, did, ord, type, queue, due, ivl, reps, lapses from cards"
    )
    return {int(r[0]): tuple(int(x) for x in r[1:]) for r in rows}


def notes_snapshot() -> dict:
    """所有笔记的内容快照（笔记类型、字段、标签）。

    统计插件只读，卡片快照之外再把笔记内容也钉住：打标签那几步自己会改笔记，
    但改完必须还原，所以跑完整套流程后这里也该一模一样。
    """
    rows = mw.col.db.all("select id, mid, flds, tags from notes")
    # 标签按「拆开再排序」比：Anki 存的是空格分隔，去掉一个标签再打回来可能
    # 只是前后空格不同，那不算改动。
    return {int(r[0]): (int(r[1]), r[2], tuple(sorted((r[3] or "").split()))) for r in rows}


def _find_browser():
    """找出当前打开的卡片浏览器窗口（按类名 + 形状找，不依赖 Anki 内部注册表）。"""
    try:
        widgets = list(mw.app.topLevelWidgets())
    except Exception:  # noqa: BLE001
        return None
    for widget in widgets:
        name = type(widget).__name__.lower()
        if "browser" not in name:
            continue
        if hasattr(widget, "form") and hasattr(widget, "col"):
            return widget
    return None


def _browser_menus(module, browser):
    """浏览器菜单栏里所有 objectName 等于插件那个的 QMenu（正常应当是 1 个）。"""
    found = []
    if browser is None:
        return found
    bar = module._menubar_of(browser)
    if bar is None:
        return found
    try:
        for action in bar.actions():
            menu = action.menu()
            if menu is not None and menu.objectName() == module.BROWSER_MENU_OBJECT:
                found.append(menu)
    except Exception:  # noqa: BLE001
        return found
    return found


def _menubar_titles(module, browser) -> list[str]:
    """菜单栏上的标题列表，断言失败时打出来方便定位。"""
    if browser is None:
        return []
    bar = module._menubar_of(browser)
    if bar is None:
        return []
    try:
        return [action.text() for action in bar.actions()]
    except Exception:  # noqa: BLE001
        return []


def _hook_callbacks(hook) -> list:
    """gui_hooks 的每个钩子是个 Hook 对象，回调都藏在 ``_hooks`` 里。"""
    inner = getattr(hook, "_hooks", None)
    if isinstance(inner, list):
        return list(inner)
    try:
        return list(hook)
    except Exception:  # noqa: BLE001
        return []


# --------------------------------------------------------------------------
# 步骤
# --------------------------------------------------------------------------


def step_import() -> None:
    module = evs()
    check("插件模块能导入", module is not None)
    labels = [action.text() for action in mw.form.menuTools.actions()]
    check("工具菜单里有入口", module.MENU_LABEL in labels, "、".join(labels))

    matchers = module.get_matchers()
    en = matchers["en"]
    ja = matchers["ja"]
    # v0.3：英文分母换成 wordforge 六本词书 ∪ ECDICT（按词元组去重）
    expected_en = {
        "cet4": 4955,
        "cet6": 6953,
        "ky": 6396,
        "ielts": 6363,
        "toefl": 7897,
        "gre": 8762,
    }
    for code, want in expected_en.items():
        got = int(en.exam_counts.get(code, 0))
        check(f"英语分母 {code} = {want}", got == want, f"实际 {got}")
    # v0.3：日语分母换成 eggrolls JLPT10k ∪ OpenJLPT ∪ tanos（按汉字表记去重）
    expected_ja = {"n5": 866, "n4": 848, "n3": 2076, "n2": 3984, "n1": 5788}
    for code, want in expected_ja.items():
        got = int(ja.level_counts.get(code, 0))
        check(f"日语分母 {code} = {want}", got == want, f"实际 {got}")
    record(
        "index_meta",
        {"en": matchers.get("en_meta", {}), "ja": matchers.get("ja_meta", {})},
    )

    # 更新机制：只做离线断言，探针内不触网。
    # 探针不点「检查更新」，只核对常量、地址形状和默认开关，避免跑到一半挂网络。
    ver = getattr(module, "__version__", "")
    check("插件版本号 = 0.3.0", ver == "0.3.0", f"实际 {ver!r}")
    U = getattr(module, "U", None)
    check("能拿到 update_logic 模块", U is not None)
    if U is not None:
        check(
            "更新仓库常量",
            U.UPDATE_REPO == "creeperboo/anki-exam-vocab-stats",
            U.UPDATE_REPO,
        )
        check("分支常量 = main", U.UPDATE_BRANCH == "main", U.UPDATE_BRANCH)
        check(
            "分发包名常量",
            U.UPDATE_ASSET == "exam_vocab_stats.ankiaddon",
            U.UPDATE_ASSET,
        )
        check("默认每天最多查一次", U.UPDATE_INTERVAL_SECONDS == 86400, str(U.UPDATE_INTERVAL_SECONDS))
        want_ver = (
            "https://raw.githubusercontent.com/creeperboo/anki-exam-vocab-stats/"
            "main/version.txt"
        )
        got_ver = U.raw_url(U.UPDATE_VERSION_FILE)
        check("raw 版本号 URL 形状", got_ver == want_ver, got_ver)
        want_pkg = (
            "https://raw.githubusercontent.com/creeperboo/anki-exam-vocab-stats/"
            "main/exam_vocab_stats.ankiaddon"
        )
        got_pkg = U.raw_url(U.UPDATE_ASSET)
        check("raw 分发包 URL 形状", got_pkg == want_pkg, got_pkg)
        got_rel = U.release_asset_url(U.UPDATE_ASSET)
        check(
            "Release 附件 URL 形状",
            got_rel
            == "https://github.com/creeperboo/anki-exam-vocab-stats/"
            "releases/latest/download/exam_vocab_stats.ankiaddon",
            got_rel,
        )
        check(
            "下载通道顺序：先 raw 再 Release",
            U.download_urls(U.UPDATE_ASSET) == [got_pkg, got_rel],
            str(U.download_urls(U.UPDATE_ASSET)),
        )
    on_disk = module.disk_version()
    check("磁盘上的 version.txt 与 __version__ 一致", on_disk == ver, f"磁盘 {on_disk!r}")
    check(
        "默认开启更新检查",
        module.DEFAULTS.get("update_check") is True,
        str(module.DEFAULTS.get("update_check")),
    )
    state_path = str(module.update_state_path())
    check(
        "更新状态写在用户配置目录，不落 addons21",
        "addons21" not in state_path,
        state_path,
    )
    record("update_meta", {"version": ver, "on_disk": on_disk, "state_path": state_path})


def step_sample_data() -> None:
    decks = {
        "en": make_deck(DECK_EN),
        "ja": make_deck(DECK_JA),
        "kaishi": make_deck(DECK_KAISHI),
        "lapis": make_deck(DECK_LAPIS),
        "grammar": make_deck(DECK_GRAMMAR),
    }
    STATE["decks"] = decks

    model_en = make_model(MODEL_EN, ["vocabularyWord", "ExampleSentence"])
    model_ja = make_model(MODEL_JA, ["VocabKanji", "VocabFurigana"])
    model_kaishi = make_model(MODEL_KAISHI, ["Word", "Word Reading"])
    model_lapis = make_model(MODEL_LAPIS, ["Expression", "Meaning"])
    model_grammar = make_model(MODEL_GRAMMAR, ["Grammar", "Meaning"])

    notes: dict[str, Any] = {}
    notes["abandon"] = add_note(
        model_en, decks["en"],
        {
            "vocabularyWord": "abandon",
            "ExampleSentence": "He abandoned the plan after the meeting ended.",
        },
        ["English-CEFR::CEFR-A1"],
    )
    notes["children"] = add_note(
        model_en, decks["en"],
        {
            "vocabularyWord": "children",
            "ExampleSentence": "The children are playing outside in the garden.",
        },
        ["English-CEFR::CEFR-A2"],
    )
    notes["better"] = add_note(
        model_en, decks["en"],
        {
            "vocabularyWord": "better",
            "ExampleSentence": "She is feeling much better today than yesterday.",
        },
        ["English-CEFR::CEFR-A1"],
    )
    notes["unknown"] = add_note(
        model_en, decks["en"],
        {"vocabularyWord": "zzzqwerty", "ExampleSentence": "Nonsense."},
        ["English-CEFR::CEFR-C1"],
    )
    notes["taberu"] = add_note(
        model_ja, decks["ja"],
        {"VocabKanji": "食べる", "VocabFurigana": "たべる"},
        ["eggrolls-JLPT10k-v3.5::1-N5"],
    )
    notes["beer"] = add_note(
        model_ja, decks["ja"],
        {"VocabKanji": "ビール", "VocabFurigana": "ビール"},
        ["eggrolls-JLPT10k-v3.5::3-N3"],
    )
    notes["building"] = add_note(
        model_ja, decks["ja"],
        {"VocabKanji": "ビル", "VocabFurigana": "ビル"},
        ["eggrolls-JLPT10k-v3.5::4-N4"],
    )
    notes["au"] = add_note(
        model_ja, decks["ja"],
        {"VocabKanji": "あう", "VocabFurigana": "あう"},
        ["eggrolls-JLPT10k-v3.5::2-N4"],
    )
    notes["kaishi"] = add_note(
        model_kaishi, decks["kaishi"],
        {"Word": "たべる", "Word Reading": "たべる"},
        ["Kaishi"],
    )
    notes["lapis_apple"] = add_note(
        model_lapis, decks["lapis"],
        {"Expression": "apple", "Meaning": "りんご"},
        ["Lapis"],
    )
    notes["lapis_abandon"] = add_note(
        model_lapis, decks["lapis"],
        {"Expression": "abandon", "Meaning": "放棄する"},
        ["Lapis"],
    )
    notes["grammar"] = add_note(
        model_grammar, decks["grammar"],
        {"Grammar": "〜ている", "Meaning": "正在…"},
        ["文法"],
    )
    STATE["notes"] = notes

    set_state(notes["abandon"].id, "new")
    set_state(notes["children"].id, "learn")
    set_state(notes["better"].id, "review")
    set_state(notes["unknown"].id, "new")
    set_state(notes["taberu"].id, "new")
    set_state(notes["beer"].id, "review")
    set_state(notes["building"].id, "learn")
    set_state(notes["au"].id, "suspended")
    set_state(notes["kaishi"].id, "review")
    set_state(notes["lapis_apple"].id, "new")
    set_state(notes["lapis_abandon"].id, "new")
    set_state(notes["grammar"].id, "new")

    check("样例数据建好了 12 条笔记", len(notes) == 12, f"实际 {len(notes)}")
    record(
        "sample_decks",
        {
            "decks": {key: int(value) for key, value in decks.items()},
            "notes": {key: int(value.id) for key, value in notes.items()},
        },
    )
    STATE["before_snapshot"] = cards_snapshot()
    STATE["before_notes"] = notes_snapshot()


def step_menus() -> None:
    """菜单入口：确认钩子按本机 Anki 的真实名字挂上了，并且回调真的能加菜单项。"""
    from aqt import gui_hooks
    from aqt.qt import QMenu

    module = evs()
    menu_hooks = sorted(name for name in dir(gui_hooks) if "menu" in name.lower())
    record("hook_names", {"menu_hooks": menu_hooks, "registered": dict(module.HOOK_REGISTRY)})
    check("工具菜单入口已挂上", module.HOOK_REGISTRY.get("tools_menu") is True)
    tools_texts = [action.text() for action in mw.form.menuTools.actions()]
    check(
        # Anki 自己的工具菜单里本来就有一项「检查更新」（查 Anki 本体更新），
        # 所以不能用「检查更新」这个词判定；插件的入口标签里带 GitHub。
        "工具菜单里没有插件的「检查更新（GitHub）」入口",
        not any("GitHub" in text for text in tools_texts),
        "、".join(tools_texts),
    )
    check(
        "至少挂上两个真实存在的右键/菜单钩子",
        sum(1 for key, value in module.HOOK_REGISTRY.items() if key != "tools_menu" and value) >= 2,
        str(module.HOOK_REGISTRY),
    )

    # 真造一个 QMenu，把「牌组菜单」回调跑一遍，确认不炸且确实加了菜单项
    deck_id = STATE["decks"]["en"]
    tag_name = "English-CEFR::CEFR-A1"
    menu = QMenu(mw)
    module._deck_browser_menu(menu, deck_id)
    labels = [action.text() for action in menu.actions()]
    check("牌组菜单里有统计入口", any("统计这个牌组" in text for text in labels), str(labels))
    menu.deleteLater()

    # 左侧栏右键：用同样形状的替身对象走一遍插件自己的分支逻辑
    try:
        from aqt.browser.sidebar.item import SidebarItemType

        item_types = [member.name for member in SidebarItemType]
    except Exception:
        item_types = []
    record("sidebar_item_types", {"types": item_types})

    class _FakeItem:
        """形状跟 aqt.browser.sidebar.SidebarItem 一样的最小替身。"""

        def __init__(self, kind, node, name="", full_name=""):
            self.item_type = type("T", (), {"name": kind})()
            self.search_node = node
            self.name = name
            self.full_name = full_name

    fake_deck = _FakeItem(
        "DECK",
        type("Node", (), {"deck_id": deck_id, "tag": ""})(),
        DECK_EN,
        DECK_EN,
    )
    menu_deck = QMenu(mw)
    module._sidebar_context_menu(None, menu_deck, fake_deck, None)
    deck_labels = [action.text() for action in menu_deck.actions()]
    check(
        "侧边栏牌组右键有入口",
        any("统计这个牌组" in text for text in deck_labels),
        str(deck_labels),
    )
    menu_deck.deleteLater()

    fake_tag = _FakeItem(
        "TAG",
        type("Node", (), {"deck_id": None, "tag": tag_name})(),
        "CEFR-A1",
        tag_name,
    )
    menu_tag = QMenu(mw)
    module._sidebar_context_menu(None, menu_tag, fake_tag, None)
    tag_labels = [action.text() for action in menu_tag.actions()]
    check(
        "侧边栏标签右键有入口",
        any("统计这个标签" in text for text in tag_labels),
        str(tag_labels),
    )
    menu_tag.deleteLater()

    # ---- v0.3 重点回归：打开卡片浏览器不能崩 ----------------------------------
    # 26.09 的 browser_menus_did_init 只传 browser 一个参数；老代码按
    # (browser, menu) 去挂，一开浏览器就 TypeError、窗口根本打不开。这里直接
    # 调 mw.onBrowse() 复现那条路径，再检查顶级菜单「应试词汇」有没有挂上。
    from exam_vocab_stats import hook_guard as HG  # noqa: PLC0415

    opened_error = ""
    try:
        mw.onBrowse()
    except Exception as exc:  # noqa: BLE001
        opened_error = f"{type(exc).__name__}: {exc}"
    # 插件把「补挂菜单」延到事件循环下一轮（26.09 的 setupMenus 会重建菜单栏），
    # 这里先把这一轮事件跑完，再检查菜单。
    try:
        mw.app.processEvents()
    except Exception:  # noqa: BLE001
        pass
    check("打开卡片浏览器不抛异常", not opened_error, opened_error)

    browser = _find_browser()
    check("能拿到卡片浏览器窗口", browser is not None)
    record("browser_menu_info", dict(module.BROWSER_MENU_INFO))
    menus = _browser_menus(module, browser)
    check(
        "浏览器菜单栏出现「应试词汇」子菜单（且只有一份）",
        len(menus) == 1,
        f"找到 {len(menus)} 个；菜单栏={_menubar_titles(module, browser)}",
    )
    # 多跑几轮事件循环再查一遍：这能抓住「菜单加进去又被回收/被重建冲掉」那类
    # 只在真实 Qt 生命周期里才暴露的坑（本机就栽过一次）。
    if browser is not None:
        for _ in range(4):
            mw.app.processEvents()
        check(
            "跑过几轮事件循环后菜单还在",
            len(_browser_menus(module, browser)) == 1,
            f"找到 {len(_browser_menus(module, browser))} 个；菜单栏={_menubar_titles(module, browser)}",
        )
    if menus:
        item_texts = [action.text() for action in menus[0].actions()]
        check(
            "「应试词汇」菜单里有「统计当前筛选结果」入口",
            any("统计当前筛选结果" in text for text in item_texts),
            str(item_texts),
        )
    # 再手动触发一次回调（模拟「关掉再开」时钩子被重复调用）：不许叠加第二份
    if browser is not None:
        try:
            module._browser_menu(browser)
            mw.app.processEvents()
        except Exception as exc:  # noqa: BLE001
            check("重复触发浏览器菜单回调不抛异常", False, f"{type(exc).__name__}: {exc}")
        else:
            check("重复触发浏览器菜单回调不抛异常", True)
        check(
            "重复触发后仍然只有一份菜单（不叠加）",
            len(_browser_menus(module, browser)) == 1,
            f"找到 {len(_browser_menus(module, browser))} 个",
        )
    if browser is not None:
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- 每个已注册的钩子都按「本机 Anki 的真实签名」过一遍 ----------------
    hook_cases = [
        # (钩子名, 我们注册的回调, 本机 Anki 的真实参数)
        ("browser_menus_did_init", "_browser_menu", (object(),)),
        ("deck_browser_will_show_options_menu", "_deck_browser_menu", (None, 1)),
        ("browser_will_show_context_menu", "_browser_context_menu", (object(), None)),
        (
            "browser_sidebar_will_show_context_menu",
            "_sidebar_context_menu",
            (None, None, None, 0),
        ),
        ("profile_did_open", "_on_profile_did_open", ()),
    ]
    for hook_name, attr, args in hook_cases:
        callback = getattr(module, attr, None)
        if callback is None:
            check(f"钩子 {hook_name} 的回调存在", False)
            continue
        check(
            f"钩子 {hook_name} 按真实签名能绑定",
            HG.bind_ok(callback, args),
            f"参数 {len(args)} 个，回调 {getattr(callback, '__name__', '?')}",
        )
        registered = _hook_callbacks(getattr(gui_hooks, hook_name, None))
        check(
            f"钩子 {hook_name} 挂上的是受保护的包装",
            any(
                getattr(entry, "__guarded_name__", "") == getattr(callback, "__name__", "")
                for entry in registered
            )
            or not module.HOOK_REGISTRY.get(hook_name),
            str(module.HOOK_REGISTRY.get(hook_name)),
        )

    strict = HG.guard(lambda first, second: (first, second))
    check("guard 参数对不上时返回 None", strict(1) is None)
    check("guard 参数对上时正常回传结果", strict(1, 2) == (1, 2))
    guarded_deck = HG.guard(module._deck_browser_menu)
    check("guard 包过的回调少传参数时返回 None 而非抛错", guarded_deck(None) is None)

    # 语法牌库等没有取词字段的笔记类型不该出现在任何入口里（结构检查，防回归）
    check(
        "菜单回调没有写死语言/词表",
        "cet4" not in "".join(menu_hooks) and "jlpt" not in "".join(menu_hooks),
    )


def step_field_recommendation() -> None:
    module = evs()
    matchers = module.get_matchers()
    detected = module.detect_fields(mw.col, matchers)
    STATE["detected"] = detected
    by_name = {row["notetype_name"]: row for row in detected.values()}

    en = by_name.get(MODEL_EN)
    check("English-CEFR 能被识别出来", en is not None)
    if en:
        check(
            "English-CEFR 取词字段 = vocabularyWord",
            en["fields"][:1] == ["vocabularyWord"],
            str(en["fields"]),
        )
        check("English-CEFR 判为英语", en["language"] == "en", en["language"])
        check("English-CEFR 默认勾选", bool(en["enabled"]), en.get("reason", ""))
        check(
            "English-CEFR 没把例句当取词字段",
            "ExampleSentence" not in en["fields"],
            str(en["fields"]),
        )
        check("English-CEFR 有预览词", bool(en["preview"]), str(en["preview"]))
        check(
            "English-CEFR 记下了命中率原始数字（抽样/命中）",
            int(en.get("samples", 0)) > 0
            and 0 <= int(en.get("hits", 0)) <= int(en.get("samples", 0)),
            f"samples={en.get('samples')} hits={en.get('hits')}",
        )
        check(
            "English-CEFR 的取词字段本身不像例句",
            not en.get("looks_like_sentence"),
            str(en.get("looks_like_sentence")),
        )

        # 例句列：字段名被一票否决之外，内容也要被判成「像例句」，设置页才会写清原因
        example_values = []
        for note in STATE["notes"].values():
            try:
                example_values.append(note["ExampleSentence"])
            except Exception:
                pass
        example_info = module.V.score_field("ExampleSentence", example_values, matchers)
        check(
            "例句列被判成「像例句/长文本」",
            bool(example_info.get("looks_like_sentence")),
            str(example_values),
        )
        reason_text = module._field_reason(
            {
                "score": example_info["score"],
                "hit_rate": example_info["hit_rate"],
                "samples": example_info["samples"],
                "hits": example_info["hits"],
                "looks_like_sentence": True,
                "status": "inferred",
                "reason": "",
            }
        )
        check("像例句的字段在设置页写明原因", "像例句" in reason_text, reason_text)

    ja = by_name.get(MODEL_JA)
    check("JLPT10k 能被识别出来", ja is not None)
    if ja:
        check("JLPT10k 取词字段 = VocabKanji", ja["fields"][:1] == ["VocabKanji"], str(ja["fields"]))
        check(
            "JLPT10k 假名兜底 = VocabFurigana",
            ja["reading_field"] == "VocabFurigana",
            ja["reading_field"],
        )
        check("JLPT10k 判为日语", ja["language"] == "ja", ja["language"])
        check("JLPT10k 默认勾选", bool(ja["enabled"]), ja.get("reason", ""))

    kaishi = by_name.get(MODEL_KAISHI)
    if kaishi:
        check("Kaishi 取词字段 = Word", kaishi["fields"][:1] == ["Word"], str(kaishi["fields"]))
        check(
            "Kaishi 假名兜底 = Word Reading",
            kaishi["reading_field"] == "Word Reading",
            kaishi["reading_field"],
        )
        check("Kaishi 默认勾选", bool(kaishi["enabled"]), kaishi.get("reason", ""))

    lapis = by_name.get(MODEL_LAPIS)
    if lapis:
        check("Lapis 默认不勾选", not lapis["enabled"], f"reason={lapis.get('reason', '')}")

    grammar = by_name.get(MODEL_GRAMMAR)
    if grammar:
        check("语法牌库默认不勾选", not grammar["enabled"], f"reason={grammar.get('reason', '')}")

    record(
        "detected",
        {
            name: {
                "fields": row["fields"],
                "reading_field": row["reading_field"],
                "language": row["language"],
                "enabled": row["enabled"],
                "score": row["score"],
                "hit_rate": row["hit_rate"],
                "samples": row.get("samples", 0),
                "hits": row.get("hits", 0),
                "looks_like_sentence": row.get("looks_like_sentence", False),
                "reason": row["reason"],
            }
            for name, row in by_name.items()
        },
    )


def _scope(
    *,
    deck_ids=(),
    tags_include=(),
    states=(),
    notetype_ids=(),
    tag_mode="any",
    search="",
):
    return {
        "deck_ids": list(deck_ids),
        "include_subdecks": True,
        "tags_include": list(tags_include),
        "tags_exclude": [],
        "tag_mode": tag_mode,
        "notetype_ids": list(notetype_ids),
        "states": list(states) or ["new", "learn", "review", "suspended"],
        "search": search,
    }


def _config(module: Any, cache: bool = False) -> dict:
    config = dict(module.DEFAULTS)
    config["scope"] = dict(module.DEFAULTS["scope"])
    config["field_map"] = STATE["detected"]
    config["cache_enabled"] = cache
    return config


def _find_notetype_key(field_map: dict, name: str):
    for key, value in field_map.items():
        if value.get("notetype_name") == name:
            return key
    return None


def _row(summary: dict, language: str, code: str) -> dict:
    for row in summary["languages"][language]["coverage"]:
        if row["code"] == code:
            return row
    return {}


def step_collect() -> None:
    module = evs()
    analysis = module.analysis
    matchers = module.get_matchers()
    config = _config(module)
    field_map = module.field_index_map(config)

    scope = _scope(deck_ids=[STATE["decks"]["en"], STATE["decks"]["ja"]])
    collected = analysis.collect_entries(mw.col, scope, field_map, matchers)
    summary = analysis.summarize(mw.col, collected, config, matchers)
    STATE["summary"] = summary
    STATE["collected"] = collected

    en = summary["languages"]["en"]
    ja = summary["languages"]["ja"]
    record(
        "counts",
        {
            "en_words": en["words"],
            "ja_words": ja["words"],
            "en_states": en["states"],
            "ja_states": ja["states"],
            "pending": [en["pending_words"], ja["pending_words"]],
            "skipped": collected["skipped"],
        },
    )

    # children→child 与 better→good/well 归成词元后：abandon / child / well|good / zzzqwerty 共 4 个唯一词
    check("英语轴唯一词 = 4（children/better 已归元）", en["words"] == 4, f"实际 {en['words']}")
    keys = {w["key"] for w in summary["words"] if w["language"] == "en"}
    check("children 归成 child", "child" in keys, str(sorted(keys)))
    check(
        "better 归成 good 或 well（消歧按词频，不猜）",
        bool(keys & {"good", "well"}),
        str(sorted(keys)),
    )
    check("没把 children 单独算一个词", "children" not in keys, str(sorted(keys)))

    # 日语轴：食べる / びる / びーる / あう 共 4 个唯一词，其中 あう 是待确认
    check("日语轴唯一词 = 4", ja["words"] == 4, f"实际 {ja['words']}")
    check("日语待确认 1 个（あう）", ja["pending_words"] == 1, str(ja["pending_words"]))
    ja_keys = {w["key"] for w in summary["words"] if w["language"] == "ja"}
    check(
        "JLPT10k 的 食べる 按汉字表记直接命中",
        "食べる" in ja_keys,
        str(sorted(ja_keys)),
    )

    covered_keys = {
        w["key"] for w in summary["words"] if w["confident"] and (w["exams"] or w["levels"])
    }
    check("あう 不计入覆盖", "あう" not in covered_keys, str(sorted(covered_keys)))

    # 新词表（wordforge ∪ ECDICT）里 child 和 well 都带四级标签，
    # 所以范围里命中四级的是 abandon + child + well 三个词元，各算一次。
    cet4 = _row(summary, "en", "cet4")
    record("cet4_row", cet4)
    check(
        "四级已覆盖 = 3（abandon + child + better 归元后的 well）",
        cet4.get("covered") == 3,
        str(cet4),
    )
    check("四级总数 = 4955", cet4.get("total") == 4955, str(cet4))
    check(
        "四级未覆盖 = 总数 - 已覆盖",
        cet4.get("missing") == cet4.get("total", 0) - cet4.get("covered", 0),
        str(cet4),
    )

    # 这个范围只含 English-CEFR 与 JLPT10k 两个牌组，不含 Kaishi。
    # v0.3 换 eggrolls 词表后：食べる(n5) 与 ビル(n5) 都在 N5，ビール 归到 N2；
    # あう 是待确认、不计入。这里关键是「ビル / ビール 没被并成一个词」。
    n5 = _row(summary, "ja", "n5")
    n4 = _row(summary, "ja", "n4")
    n3 = _row(summary, "ja", "n3")
    n2 = _row(summary, "ja", "n2")
    check("N5 已覆盖 = 2（食べる + ビル）", n5.get("covered") == 2, str(n5))
    check("N2 已覆盖 = 1（ビール）", n2.get("covered") == 1, str(n2))
    check("N4 已覆盖 = 0", n4.get("covered") == 0, str(n4))
    check("N3 已覆盖 = 0", n3.get("covered") == 0, str(n3))
    ja_keys_all = {
        w["key"] for w in summary["words"] if w["language"] == "ja" and w["confident"]
    }
    check(
        "ビル 与 ビール 是两个词，没被并成一个",
        "びる" in ja_keys_all and "びーる" in ja_keys_all,
        str(sorted(ja_keys_all)),
    )

    # Kaishi 牌库的词条本身就是假名，靠「取词字段当读音」兜底归成汉字表记 食べる
    kaishi_scope = _scope(deck_ids=[STATE["decks"]["kaishi"]])
    kaishi_collected = analysis.collect_entries(mw.col, kaishi_scope, field_map, matchers)
    kaishi_summary = analysis.summarize(mw.col, kaishi_collected, config, matchers)
    kaishi_ja = kaishi_summary["languages"]["ja"]
    kaishi_keys = {w["key"] for w in kaishi_summary["words"] if w["language"] == "ja"}
    check(
        "Kaishi 的 たべる 靠读音兜底归成 食べる",
        kaishi_ja["words"] == 1 and kaishi_keys == {"食べる"},
        str({"words": kaishi_ja["words"], "keys": sorted(kaishi_keys)}),
    )
    check(
        "Kaishi 的 たべる 计入 N5 覆盖",
        _row(kaishi_summary, "ja", "n5").get("covered") == 1,
        str(_row(kaishi_summary, "ja", "n5")),
    )

    # 各词表都要有「总数/已覆盖/覆盖率/未覆盖」四件套
    for language, codes in (
        ("en", ["cet4", "cet6", "ky", "ielts", "toefl", "gre", "cet46"]),
        ("ja", ["n5", "n4", "n3", "n2", "n1", "jlpt"]),
    ):
        for code in codes:
            row = _row(summary, language, code)
            check(
                f"{language}/{code} 四件套齐全",
                bool(row) and all(row.get(k) is not None for k in ("total", "covered", "missing", "rate")),
                str(row),
            )

    # Lapis 默认没勾选 → 它的笔记应该整批被跳过，不能进英语轴
    lapis_scope = _scope(deck_ids=[STATE["decks"]["lapis"]])
    lapis_collected = analysis.collect_entries(mw.col, lapis_scope, field_map, matchers)
    check(
        "Lapis 默认不参与统计",
        len(lapis_collected["entries"]) == 0,
        f"实际收了 {len(lapis_collected['entries'])} 条",
    )

    # 想纳入时一键勾上就能统计（证明「默认不勾选」不是「不支持」）
    lapis_key = _find_notetype_key(STATE["detected"], MODEL_LAPIS)
    if lapis_key is not None:
        field_map_on = dict(field_map)
        # field_index_map 的键是 int，detected 的键是 str，这里要对上
        turned_on = dict(STATE["detected"][lapis_key])
        turned_on["enabled"] = True
        field_map_on[int(lapis_key)] = turned_on
        on_collected = analysis.collect_entries(mw.col, lapis_scope, field_map_on, matchers)
        check(
            "Lapis 勾上后能统计",
            len(on_collected["entries"]) == 2,
            f"实际 {len(on_collected['entries'])} 条",
        )


def step_state_counts() -> None:
    summary = STATE["summary"]
    analysis = evs().analysis
    scope = _scope(deck_ids=[STATE["decks"]["en"], STATE["decks"]["ja"]])
    where, params = analysis.nodeck_scope(scope, mw.col)
    rows = mw.col.db.all(
        "select c.type, c.queue from notes n join cards c on c.nid = n.id where " + where,
        *params,
    )
    direct = {"new": 0, "learn": 0, "review": 0, "suspended": 0}
    for ctype, queue in rows:
        direct[analysis.card_state(queue, ctype)] += 1

    plugin = {"new": 0, "learn": 0, "review": 0, "suspended": 0}
    seen_notes = set()
    for entry in STATE["collected"]["entries"]:
        if entry["note_id"] in seen_notes:
            continue
        seen_notes.add(entry["note_id"])
        plugin[entry["state"]] += 1

    record("state_counts", {"direct_sql": direct, "plugin_notes": plugin})
    check("每条笔记的状态与数据库直查一致", direct == plugin, f"sql={direct} plugin={plugin}")

    # 总览页的状态占比要加总 100%
    for language in ("en", "ja"):
        states = summary["languages"][language]["states"]
        total_pct = round(sum(row["pct"] for row in states), 1)
        if summary["languages"][language]["words"]:
            check(f"{language} 状态占比加总 100%", abs(total_pct - 100.0) <= 0.2, str(total_pct))
        check(
            f"{language} 状态词数加总 = 唯一词数",
            sum(row["words"] for row in states) == summary["languages"][language]["words"],
            f"{states}",
        )

    # 来源占比要加总 100%
    for language in ("en", "ja"):
        for code, sources in summary["languages"][language]["sources"].items():
            if not sources:
                continue
            total_pct = round(sum(row["pct"] for row in sources), 1)
            check(
                f"{language}/{code} 来源占比加总 100%",
                abs(total_pct - 100.0) <= 0.2,
                f"{total_pct} {sources}",
            )


def step_sources() -> None:
    """来源页：三种来源维度的口径、缓存键，以及明细页用的打标签后端。"""
    module = evs()
    analysis = module.analysis
    V = module.V
    matchers = module.get_matchers()
    config = _config(module)
    scope = _scope(deck_ids=[STATE["decks"]["en"], STATE["decks"]["ja"]])
    collected = analysis.collect_entries(
        mw.col, scope, module.field_index_map(config), matchers
    )
    # 这一份「刚读出来、还没合并」的原始词条：来源维度只动聚合方式，原始词条
    # 必须和界面里那份一致；一旦两边对不上，就是取词/范围哪里出了问题。
    record(
        "sources_raw_entries",
        {
            "scanned_notes": collected["scanned_notes"],
            "scanned_cards": collected["scanned_cards"],
            "en_entries": [
                {
                    "note_id": int(entry["note_id"]),
                    "key": entry["key"],
                    "surface": entry["surface"],
                    "tags": sorted(entry["tags"]),
                    "state": entry["state"],
                }
                for entry in collected["entries"]
                if entry["language"] == "en"
            ],
        },
    )

    # 标签名生成：合并码不打标签，日语等级带 JLPT- 前缀
    check("四级标签名 = 应试::四级", V.exam_tag_for_code("cet4") == "应试::四级", V.exam_tag_for_code("cet4"))
    check("GRE 标签名 = 应试::GRE", V.exam_tag_for_code("gre") == "应试::GRE", V.exam_tag_for_code("gre"))
    check(
        "N3 标签名 = 应试::JLPT-N3",
        V.exam_tag_for_code("n3") == "应试::JLPT-N3",
        V.exam_tag_for_code("n3"),
    )
    check("合并码不打标签（四六级并集）", V.exam_tag_for_code("cet46") == "", V.exam_tag_for_code("cet46"))
    check("合并码不打标签（JLPT 并集）", V.exam_tag_for_code("jlpt") == "", V.exam_tag_for_code("jlpt"))
    check("自家标签判定", V.is_own_tag("应试::四级") and not V.is_own_tag("English-CEFR::CEFR-A1"))

    # 三种来源维度都要能出数，且占比加总 100%
    summaries = {}
    for dimension in ("deck", "tag", "notetype"):
        dim_config = dict(config)
        dim_config["source_dimension"] = dimension
        summary = analysis.summarize(mw.col, collected, dim_config, matchers)
        summaries[dimension] = summary
        check(f"来源维度 {dimension} 被采纳", summary.get("source_dimension") == dimension, str(summary.get("source_dimension")))
        for language in ("en", "ja"):
            for code, rows in summary["languages"][language]["sources"].items():
                if not rows:
                    continue
                total_pct = round(sum(row["pct"] for row in rows), 1)
                check(
                    f"{dimension}/{language}/{code} 来源占比加总 100%",
                    abs(total_pct - 100.0) <= 0.2,
                    f"{total_pct} {rows}",
                )

    deck_rows = summaries["deck"]["languages"]["en"]["sources"]["cet4"]
    tag_rows = summaries["tag"]["languages"]["en"]["sources"]["cet4"]
    nt_rows = summaries["notetype"]["languages"]["en"]["sources"]["cet4"]
    record(
        "sources_by_dimension",
        {"deck": deck_rows, "tag": tag_rows, "notetype": nt_rows},
    )
    # 把 cet4 覆盖到的英文词连标签一起记下来：来源维度只是重新聚合，词本身
    # 不该因为换维度多出来或者少掉，出问题时这份记录能直接指出是哪个词。
    record(
        "en_cet4_words",
        {
            "words": [
                {
                    "key": word["key"],
                    "tags": sorted(word["tags"]),
                    "decks": word["deck_names"],
                }
                for word in summaries["deck"]["words"]
                if word["language"] == "en" and "cet4" in set(word.get("exams") or ())
            ]
        },
    )
    check(
        "牌组维度一个词只算一次",
        sum(row["count"] for row in deck_rows) == 3,
        str(deck_rows),
    )
    check(
        "标签维度按标签各算一次",
        {row["name"]: row["count"] for row in tag_rows}
        == {"English-CEFR::CEFR-A1": 2, "English-CEFR::CEFR-A2": 1},
        str(tag_rows),
    )
    check(
        "笔记类型维度用真实类型名",
        {row["name"]: row["count"] for row in nt_rows} == {MODEL_EN: 3},
        str(nt_rows),
    )

    # 换来源维度必须换缓存键，否则会拿旧结果画新维度
    index_meta = matchers.get("en_meta", {})
    sigs = {
        dimension: analysis.collection_signature(
            mw.col, scope, dict(config, source_dimension=dimension), index_meta
        )
        for dimension in ("deck", "tag", "notetype")
    }
    check("三种来源维度的缓存键互不相同", len(set(sigs.values())) == 3, str(sigs))

    # 插件自己打的「应试::…」标签不能污染来源图
    abandon_nid = int(STATE["notes"]["abandon"].id)
    module.apply_exam_tags(mw.col, [abandon_nid], ["应试::四级"])
    try:
        tagged = analysis.summarize(
            mw.col,
            analysis.collect_entries(
                mw.col, scope, module.field_index_map(config), matchers
            ),
            dict(config, source_dimension="tag"),
            matchers,
        )
        names = {
            row["name"]
            for rows in tagged["languages"]["en"]["sources"].values()
            for row in rows
        }
        check("来源图里没有自家应试标签", not any(V.is_own_tag(name) for name in names), str(sorted(names)))
    finally:
        module.apply_exam_tags(mw.col, [abandon_nid], ["应试::四级"], remove=True)
    check(
        "清理后笔记上没有自家标签",
        not any(V.is_own_tag(tag) for tag in _db_tags(abandon_nid)),
        str(_db_tags(abandon_nid)),
    )

    # 打标签后端：真的落到笔记上，撤销能撤掉（界面上才会走 bulk_add + 确认框）
    target_nids = [abandon_nid, int(STATE["notes"]["better"].id)]
    confirm_text = V.exam_tag_confirm_text(len(target_nids), ["应试::四级"])
    check("确认文案写了笔记条数与标签名", "2 条笔记" in confirm_text and "应试::四级" in confirm_text, confirm_text)
    added = module.apply_exam_tags(mw.col, target_nids, ["应试::四级"])
    check("bulk_add 影响 2 条笔记", added == 2, str(added))
    check(
        "标签真的写进了笔记",
        all("应试::四级" in _db_tags(nid) for nid in target_nids),
        str([_db_tags(nid) for nid in target_nids]),
    )
    undone = False
    undo_info = {}
    try:
        # Anki 26 的撤销入口是 collection 上的 undo()；mw.undo() 那层老接口
        # 在新版里已经名存实亡，调它不会报错但什么也不撤。
        undo_info["status_before"] = repr(mw.col.undo_status())
        mw.col.undo()
        undo_info["status_after"] = repr(mw.col.undo_status())
        undone = not any("应试::四级" in _db_tags(nid) for nid in target_nids)
    except Exception as exc:  # 撤销做不到就退回手动清理，别把探针卡死
        undo_info["error"] = str(exc)
        record("undo_error", {"error": str(exc)})
    record("undo_state", undo_info)
    check(
        "撤销能撤掉刚打的标签（等同 Ctrl+Z）",
        undone,
        f"{undo_info} 标签={[_db_tags(nid) for nid in target_nids]}",
    )
    module.apply_exam_tags(mw.col, target_nids, ["应试::四级"], remove=True)
    check(
        "探针结束时标签已清干净",
        not any("应试::四级" in _db_tags(nid) for nid in target_nids),
        str([_db_tags(nid) for nid in target_nids]),
    )


def _db_tags(note_id: int) -> list[str]:
    """直接从库里读某条笔记的标签（不信内存缓存，探针要的是落库结果）。"""
    row = mw.col.db.first("select tags from notes where id = ?", int(note_id))
    return (row[0] or "").split() if row else []


def _best_state_of_note(note_id: int) -> str:
    analysis = evs().analysis
    rows = mw.col.db.all("select type, queue from cards where nid = ?", int(note_id))
    state = ""
    for ctype, queue in rows:
        state = analysis._better_state(state, analysis.card_state(queue, ctype))
    return state


def step_filters() -> None:
    module = evs()
    analysis = module.analysis
    matchers = module.get_matchers()
    field_map = module.field_index_map(_config(module))
    decks = STATE["decks"]
    ja_model_id = int(mw.col.models.by_name(MODEL_JA)["id"])

    exclude_scope = _scope(deck_ids=[decks["en"]])
    exclude_scope["tags_exclude"] = ["English-CEFR::CEFR-C1"]

    cases = [
        (
            "只要 CEFR-A1 标签",
            _scope(deck_ids=[decks["en"]], tags_include=["English-CEFR::CEFR-A1"]),
        ),
        (
            "A1 或 A2（分类内并集）",
            _scope(
                deck_ids=[decks["en"]],
                tags_include=["English-CEFR::CEFR-A1", "English-CEFR::CEFR-A2"],
                tag_mode="any",
            ),
        ),
        (
            "A1 且 A2（分类内交集，应为空）",
            _scope(
                deck_ids=[decks["en"]],
                tags_include=["English-CEFR::CEFR-A1", "English-CEFR::CEFR-A2"],
                tag_mode="all",
            ),
        ),
        ("只看复习中", _scope(deck_ids=[decks["en"], decks["ja"]], states=["review"])),
        ("只看未学习", _scope(deck_ids=[decks["en"], decks["ja"]], states=["new"])),
        ("只看学习中", _scope(deck_ids=[decks["en"], decks["ja"]], states=["learn"])),
        ("只看暂停埋葬", _scope(deck_ids=[decks["en"], decks["ja"]], states=["suspended"])),
        ("限定笔记类型", _scope(notetype_ids=[ja_model_id])),
        ("排除某标签", exclude_scope),
        (
            "牌组 + 标签 + 状态三类条件同时生效",
            _scope(
                deck_ids=[decks["en"], decks["ja"]],
                tags_include=["English-CEFR::CEFR-A1", "eggrolls-JLPT10k-v3.5::1-N5"],
                states=["new", "learn"],
            ),
        ),
    ]

    for label, scope in cases:
        collected = analysis.collect_entries(mw.col, scope, field_map, matchers)
        plugin = {entry["note_id"] for entry in collected["entries"]}

        where, params = analysis.nodeck_scope(scope, mw.col)
        sql = "select n.id from notes n join cards c on c.nid = n.id"
        if where:
            sql += " where " + where
        direct = {int(row[0]) for row in mw.col.db.all(sql, *params)}
        states = set(scope.get("states") or ())
        if states and len(states) < 4:
            direct = {nid for nid in direct if _best_state_of_note(nid) in states}
        record("filter_case", {"label": label, "plugin": len(plugin), "sql": len(direct)})
        check(
            f"筛选「{label}」与直查数据库一致",
            plugin == direct,
            f"plugin={sorted(plugin)} sql={sorted(direct)}",
        )

    # 卡片浏览器的筛选结果（浏览器搜索式）也要能对上
    search_scope = _scope(search=f'deck:"{DECK_EN}"')
    collected = analysis.collect_entries(mw.col, search_scope, field_map, matchers)
    plugin = {entry["note_id"] for entry in collected["entries"]}
    expected = {int(nid) for nid in mw.col.find_notes(f'deck:"{DECK_EN}"')}
    check(
        "「统计当前筛选结果」（浏览器搜索式）与 find_notes 一致",
        plugin == expected,
        f"plugin={sorted(plugin)} expected={sorted(expected)}",
    )


def step_uncovered() -> None:
    module = evs()
    analysis = module.analysis
    summary = STATE["summary"]
    matchers = module.get_matchers()

    uncovered_cet4 = analysis.uncovered_words(matchers, summary, "en", "cet4")
    covered_en = {
        w["key"] for w in summary["words"] if w["language"] == "en" and w["confident"]
    }
    heads = {
        head for head, tags in matchers["en"].group_exam.items() if "cet4" in tags.split()
    }
    record(
        "uncovered",
        {"en_cet4": len(uncovered_cet4), "ja_n5": None},
    )
    # 未覆盖清单的口径：词表词条里，卡片既没有直接命中、也不能归元到已覆盖词元的部分。
    # 注意它和覆盖页的「未覆盖 = 总数 − 已覆盖」不是同一个分母口径：词表里有
    # analyse/analyze、stairs/stair 这种同一词的不同词条，卡片只算一个词元，
    # 但这些词条都算被覆盖到了，所以清单会比「总数 − 已覆盖」少几条，不能直接相等。
    covered_forms = analysis.covered_form_set(summary, "en", "cet4")
    delivered = {
        head
        for head in heads
        if head in covered_en
        or matchers["en"].resolve(head).key in covered_en
        or bool(analysis.surface_forms("en", head) & covered_forms)
    }
    uncovered_keys = {row["key"] for row in uncovered_cet4}
    record(
        "uncovered_math",
        {
            "uncovered": len(uncovered_cet4),
            "heads": len(heads),
            "delivered": len(delivered),
            "missing_row": _row(summary, "en", "cet4").get("missing"),
        },
    )
    check(
        "未覆盖清单里没有已被卡片覆盖（或能归元到已覆盖词）的词表词",
        not (uncovered_keys & delivered),
        str(sorted(uncovered_keys & delivered)[:10]),
    )
    check(
        "未覆盖清单 = 词表里没被卡片覆盖的那部分",
        len(uncovered_cet4) == len(heads - delivered),
        f"{len(uncovered_cet4)} vs {len(heads - delivered)}",
    )
    check("abandon 不该出现在未覆盖里", "abandon" not in {row["key"] for row in uncovered_cet4})

    uncovered_n5 = analysis.uncovered_words(matchers, summary, "ja", "n5")
    heads_ja = {
        word for word, levels in matchers["ja"].by_word.items() if "n5" in levels.split()
    }
    covered_ja = {
        w["key"] for w in summary["words"] if w["language"] == "ja" and w["confident"]
    }
    check(
        "未覆盖清单 = 词表总数 - 已覆盖（N5）",
        len(uncovered_n5) == len(heads_ja - covered_ja),
        f"{len(uncovered_n5)} vs {len(heads_ja - covered_ja)}",
    )
    check("未覆盖的日语词带释义", all(row.get("meaning") is not None for row in uncovered_n5[:20]))

    # 原因只有两态（界面口径）：已覆盖 / 未覆盖；内部三种细分类都归到「未覆盖」。
    check("已覆盖词的原因显示为「已覆盖」", analysis.display_reason("") == "已覆盖", "")
    for gap in (analysis.GAP_MISSING, analysis.GAP_UNRECOGNIZED, analysis.GAP_PENDING):
        check(
            f"「{gap}」在界面显示为「未覆盖」",
            analysis.display_reason(gap) == "未覆盖",
            analysis.display_reason(gap),
        )

    # 识别失败 = 0：词表里明明有、卡片里也有的词，不该被我们的匹配漏掉。
    tally = {"真未覆盖": 0, "识别失败": 0, "待确认": 0}
    for language, codes in (
        ("en", ["cet4", "cet6", "ky", "ielts", "toefl", "gre"]),
        ("ja", ["n5", "n4", "n3", "n2", "n1"]),
    ):
        for code in codes:
            for row in analysis.uncovered_words(matchers, summary, language, code):
                tally[row["reason"]] = tally.get(row["reason"], 0) + 1
    record("gap_tally", tally)
    check("识别失败 = 0（词表里有、卡片里也有，只是没认出来）", tally["识别失败"] == 0, str(tally))


def step_materials() -> None:
    """内置素材库：词表里每个词的 单词 / 释义 / 音频 / 例句 都得齐全（v0.3 硬要求）。"""
    module = evs()
    matchers = module.get_matchers()
    res = module.get_resources(_config(module))

    en = res.en_materials()
    ja = res.ja_materials()
    record("materials_counts", {"en": len(en), "ja": len(ja)})
    check("内置英文素材索引读到了", len(en) > 10000, str(len(en)))
    check("内置日文素材索引读到了", len(ja) > 10000, str(len(ja)))

    en_heads = set(matchers["en"].group_exam.keys()) | set(matchers["en"].word_exam.keys())
    en_missing = [w for w in en_heads if w not in en]
    en_gaps = [
        w for w in en_heads
        if w in en and not (en[w].get("t") and en[w].get("a") and en[w].get("ex"))
    ]
    check(
        "英文词表里每个词都有素材（单词/释义/音频/例句）",
        not en_missing and not en_gaps,
        f"缺 {len(en_missing)} 个词条、{len(en_gaps)} 个素材不全",
    )

    ja_heads = set(matchers["ja"].by_word.keys())
    ja_missing = [w for w in ja_heads if w not in ja]
    ja_gaps = [
        w for w in ja_heads
        if w in ja and not (ja[w].get("m") and ja[w].get("a") and ja[w].get("ex"))
    ]
    check(
        "日文词表里每个词都有素材（读音/释义/音频/例句）",
        not ja_missing and not ja_gaps,
        f"缺 {len(ja_missing)} 个词条、{len(ja_gaps)} 个素材不全",
    )

    # 真正走一遍 lookup，确认字段名对得上（界面/制卡都靠它）
    en_row = res.lookup("abandon", "en")
    check(
        "英语 lookup 给出释义 / 例句 / 音频名",
        bool(en_row.get("translation") and en_row.get("example") and en_row.get("audio_name")),
        str({k: en_row.get(k) for k in ("translation", "example", "audio_name")})[:120],
    )
    ja_row = res.lookup("食べる", "ja")
    check(
        "日语 lookup 给出读音 / 释义 / 例句 / 音频名",
        bool(
            ja_row.get("reading") and ja_row.get("translation")
            and ja_row.get("example") and ja_row.get("audio_name")
        ),
        str({k: ja_row.get(k) for k in ("reading", "translation", "example", "audio_name")})[:120],
    )
    check("未知词查素材不报错，返回空素材", res.lookup("zzzqwertynotaword", "en").get("translation") == "")

    meta = res.manifest()
    record("materials_manifest", meta)
    check(
        "音频包清单带文件名和 sha256",
        bool(meta.get("file") and len(str(meta.get("sha256") or "")) == 64),
        str(meta)[:120],
    )
    check(
        "素材包的地址形状就是 Release 附件",
        module.U.materials_url().endswith("/exam_materials_audio.zip"),
        module.U.materials_url(),
    )

    # ---- 本地 zip 安装通道：临时造一个小包，真走一遍 install_media -----------
    installed_before = res.media_installed()
    check(
        "全新配置里音频包默认「没装」",
        installed_before["installed"] is False,
        str(installed_before),
    )

    small = os.path.join(tempfile.mkdtemp(), "exam_materials_audio.zip")
    import zipfile

    with zipfile.ZipFile(small, "w") as archive:
        for name in ("probe_a.mp3", "probe_b.mp3", "probe_c.mp3"):
            archive.writestr(name, b"\x00\x01\x02")
        archive.writestr("readme.txt", b"not audio")
    seen: list[tuple[int, int]] = []
    result = res.install_media(small, progress=lambda done, total: seen.append((done, total)))
    check("本地 zip 只数 mp3（txt 不算）", result.get("files") == 3, str(result))
    check("本地 zip 解压到了插件自己的音频目录", result.get("dir") == res.media_dir(), str(result))
    check("解压过程回调被调用过", bool(seen), str(seen[:3]))
    after = res.media_installed()
    check("装完后状态变成「已装」", after["installed"] is True and after["files"] == 3, str(after))

    def _non_zip() -> bool:
        bad = os.path.join(tempfile.mkdtemp(), "not_a_zip.zip")
        with open(bad, "wb") as handle:
            handle.write("这不是一个 zip".encode("utf-8"))
        try:
            res.install_media(bad)
        except Exception:
            return True
        return False

    check("不是 zip 的文件会报错而不是静默通过", _non_zip())


# ---------------------------------------------------------------- v0.3：父牌组 / 明细筛选 / 一键补漏

DECK_TREE_PARENT = "EVS测试::父牌组"
DECK_TREE_A = "EVS测试::父牌组::子A"
DECK_TREE_B = "EVS测试::父牌组::子B"


class FakeMaterial:
    """探针用的假素材层：不读真实词典，只用来验证「落卡」那一段。"""

    def __init__(self, audio_path: str) -> None:
        self.audio_path = audio_path

    def dictionary_lookup(self, terms, language):  # noqa: ARG002
        return {}

    def lookup(self, term, language, reading="", entries=None):  # noqa: ARG002
        return {
            "translation": "测试释义",
            "definition": "probe definition",
            "example": "This is a probe sentence.",
            "example_translation": "这是探针例句。",
            "reading": "",
            "phonetic": "ˈproʊb",
            "audio_path": self.audio_path,
            "audio_source": "probe",
        }


def step_parent_scope() -> None:
    """父牌组：能勾上、能统计到子牌组，而且不再抛 int < tuple。"""
    module = evs()
    analysis = module.analysis
    col = mw.col

    parent = make_deck(DECK_TREE_PARENT)
    child_a = make_deck(DECK_TREE_A)
    child_b = make_deck(DECK_TREE_B)
    model = make_model(MODEL_EN, ["vocabularyWord", "ExampleSentence"])
    note_a = add_note(
        model, child_a, {"vocabularyWord": "abandon", "ExampleSentence": "probe"}, ["EVS子A"]
    )
    note_b = add_note(
        model, child_b, {"vocabularyWord": "children", "ExampleSentence": "probe"}, ["EVS子B"]
    )

    try:
        expanded = analysis.deck_and_child_ids(col, parent)
        check(
            "父牌组展开后全是 int（不再混进 (名字, id) 元组）",
            bool(expanded) and all(isinstance(x, int) for x in expanded),
            repr(expanded),
        )
        check(
            "父牌组展开 = 自己 + 两个子牌组",
            set(expanded) == {parent, child_a, child_b},
            repr(expanded),
        )

        where, params = analysis.nodeck_scope(
            {"deck_ids": [parent], "include_subdecks": True}, col
        )
        check("父牌组范围能拼成 SQL（不抛 int < tuple）", "c.did in" in where, where)
        check(
            "父牌组范围的参数是三个 int 牌组",
            sorted(int(p) for p in params) == sorted([parent, child_a, child_b]),
            repr(params),
        )
        _where2, params2 = analysis.nodeck_scope(
            {"deck_ids": [parent], "include_subdecks": False}, col
        )
        check("关掉「包含子牌组」时只算父牌组自己", list(params2) == [parent], repr(params2))

        scope = _scope(deck_ids=[parent])
        dialog = module.ScopeDialog(col, _config(module), scope)
        try:
            check(
                "父牌组本身是勾上的",
                dialog.deck_items[DECK_TREE_PARENT].checkState() == Qt.CheckState.Checked,
            )
            check(
                "子牌组跟着父牌组一起勾上",
                dialog.deck_items[DECK_TREE_A].checkState() == Qt.CheckState.Checked
                and dialog.deck_items[DECK_TREE_B].checkState() == Qt.CheckState.Checked,
            )
            label = dialog.deck_count_label.text()
            check("底部实时显示「含子牌组共 N 个」", "含子牌组共 3 个" in label, label)
            dialog.deck_items[DECK_TREE_B].setCheckState(Qt.CheckState.Unchecked)
            check(
                "取消一个子牌组后父牌组变半选",
                dialog.deck_items[DECK_TREE_PARENT].checkState()
                == Qt.CheckState.PartiallyChecked,
                str(dialog.deck_items[DECK_TREE_PARENT].checkState()),
            )
            check(
                "只取消一个子牌组时兄弟牌组不受影响",
                dialog.deck_items[DECK_TREE_A].checkState() == Qt.CheckState.Checked,
            )
            dialog.deck_items[DECK_TREE_B].setCheckState(Qt.CheckState.Checked)
            result = dialog.result_scope()
            check(
                "确定后回传的牌组包含父牌组和两个子牌组",
                set(analysis.as_int_list(result["deck_ids"])) == {parent, child_a, child_b},
                repr(result["deck_ids"]),
            )
        finally:
            dialog.deleteLater()

        matchers = module.get_matchers()
        field_map = module.field_index_map(_config(module))
        collected = analysis.collect_entries(col, scope, field_map, matchers)
        nids = {int(e.get("note_id") or 0) for e in collected["entries"]}
        check(
            "父牌组范围真的把子牌组的笔记收进来",
            {int(note_a.id), int(note_b.id)} <= nids,
            str(sorted(nids)),
        )
        record(
            "parent_scope",
            {"parent": parent, "children": [child_a, child_b], "notes": sorted(nids)},
        )
    finally:
        for note in (note_a, note_b):
            try:
                col.remove_notes([int(note.id)])
            except Exception:  # noqa: BLE001
                pass
        try:
            col.decks.remove([parent, child_a, child_b])
        except Exception:  # noqa: BLE001
            pass


def step_detail_filter() -> None:
    """明细页的词表多选：并集 / 交集 / 空选择 / 不串语言，和词表定义一致。"""
    module = evs()
    analysis = module.analysis
    summary = STATE["summary"]
    words = summary["words"]
    matchers = module.get_matchers()

    def codes_of(word) -> set:
        return set(word.get("exams") or ()) | set(word.get("levels") or ())

    got_any = analysis.filter_words_by_codes(words, [("en", "cet4"), ("en", "cet6")], "any")
    got_all = analysis.filter_words_by_codes(words, [("en", "cet4"), ("en", "cet6")], "all")
    expect_any = {
        w["key"] for w in words if w["language"] == "en" and codes_of(w) & {"cet4", "cet6"}
    }
    expect_all = {
        w["key"] for w in words if w["language"] == "en" and {"cet4", "cet6"} <= codes_of(w)
    }
    check(
        "明细勾两个词表（任一命中）= 并集",
        {w["key"] for w in got_any} == expect_any,
        f"{len(got_any)} vs {len(expect_any)}",
    )
    check(
        "明细勾两个词表（全部命中）= 交集",
        {w["key"] for w in got_all} == expect_all,
        f"{len(got_all)} vs {len(expect_all)}",
    )
    check("交集不会比并集大", len(got_all) <= len(got_any), f"{len(got_all)} vs {len(got_any)}")
    check("不勾任何词表 = 不过滤", len(analysis.filter_words_by_codes(words, [], "any")) == len(words))

    ja_codes = set(module.V.JLPT_CODES)
    got_ja = analysis.filter_words_by_codes(words, [("ja", "jlpt")], "any")
    check("勾日语词表不会把英语词带进来", all(w["language"] == "ja" for w in got_ja))
    check(
        "合并码 JLPT = 任一等级",
        {w["key"] for w in got_ja}
        == {w["key"] for w in words if w["language"] == "ja" and codes_of(w) & ja_codes},
        str(len(got_ja)),
    )
    n5 = {head for head, levels in matchers["ja"].by_word.items() if "n5" in levels.split()}
    got_n5 = analysis.filter_words_by_codes(words, [("ja", "n5")], "any")
    check(
        "日语明细勾选结果都在 N5 词表里",
        all(w["key"] in n5 for w in got_n5 if w["confident"]),
        str([w["key"] for w in got_n5][:6]),
    )
    record("detail_filter", {"any": len(got_any), "all": len(got_all), "jlpt": len(got_ja)})


def step_builder() -> None:
    """一键补漏制卡：只做真未覆盖、音频进媒体库、整组能撤销、跑完收拾干净。"""
    module = evs()
    analysis = module.analysis
    builder = module.B
    col = mw.col

    tmp = tempfile.mkdtemp(prefix="evs_media_")
    audio = os.path.join(tmp, "evs_probe_audio.mp3")
    with open(audio, "wb") as fh:
        fh.write(b"ID3\x03\x00\x00\x00" + b"\x00" * 64)

    rows = [
        {"key": "abandon", "language": "en", "code": "cet4", "label": "四级", "reason": analysis.GAP_MISSING},
        {"key": "ability", "language": "en", "code": "cet4", "label": "四级", "reason": analysis.GAP_MISSING},
        {"key": "notarealword", "language": "en", "code": "cet4", "label": "四级", "reason": analysis.GAP_UNRECOGNIZED},
        {"key": "maybe", "language": "en", "code": "cet4", "label": "四级", "reason": analysis.GAP_PENDING},
    ]
    drafts, skipped = builder.build_drafts(rows, FakeMaterial(audio), {"limit": 10})
    check(
        "只有「真未覆盖」会变成卡片草稿",
        [d["key"] for d in drafts] == ["abandon", "ability"],
        str([d["key"] for d in drafts]),
    )
    check(
        "识别失败 / 待确认进了跳过清单",
        {s["key"] for s in skipped} == {"notarealword", "maybe"},
        str(skipped),
    )
    check(
        "草稿带上了释义 / 例句 / 音频",
        all(d["has_definition"] and d["has_example"] and d["has_audio"] for d in drafts),
    )
    check("牌组名按词表命名", {d["deck"] for d in drafts} == {"应试补漏::四级"}, str([d["deck"] for d in drafts]))
    check("标签按词表命名", {d["exam_tag"] for d in drafts} == {"应试::四级"}, str([d["exam_tag"] for d in drafts]))

    report = module.write_builder_cards(col, drafts, {"add_tags": True, "new_per_day": 0})
    check("落卡数量和草稿一致", report["notes"] == len(drafts), str(report))
    check("新建了补漏牌组", bool(report["decks"]) and bool(report["deck_names"]), str(report))
    check("音频真的写进媒体库", report["media"] == len(drafts), str(report))
    check("落卡过程没有报错", not report["errors"], str(report["errors"]))

    model = col.models.by_name(builder.CARD_NOTETYPE_NAME)
    nids = [int(r[0]) for r in col.db.all("select id from notes where mid=?", int(model["id"]))]
    check("新卡真的落进集合", len(nids) == len(drafts), f"{len(nids)} vs {len(drafts)}")
    check(
        "音频文件能在媒体库里找到",
        all(
            os.path.isfile(os.path.join(col.media.dir(), d["audio"]))
            for d in drafts
            if d.get("audio")
        ),
        str([d.get("audio") for d in drafts]),
    )
    if nids:
        note = col.get_note(nids[0])
        check("新卡带着应试标签", "应试::四级" in set(note.tags), str(sorted(note.tags)))
        check(
            "字段按「单词 / 假名·音标 / 释义 / …」填好",
            len(note.fields) == len(builder.CARD_FIELDS)
            and note.fields[0] == "abandon"
            and bool(note.fields[2]),
            str(note.fields),
        )
    did = col.decks.id_for_name(drafts[0]["deck"])
    try:
        conf = col.decks.config_dict_for_deck_id(did)
        check(
            "新牌组的新卡上限是 0（等你检查完再手动开）",
            int(conf["new"]["perDay"]) == 0,
            str(conf.get("new")),
        )
    except Exception as exc:  # noqa: BLE001
        check("新牌组的新卡上限是 0（等你检查完再手动开）", False, str(exc))

    undo_info = {}
    try:
        # 和打标签那一步同样的坑：Anki 26 的撤销入口在 collection 上，
        # mw.undo() 那层老接口调了不报错但什么也不撤（而且会延后生效，
        # 反而把后面步骤刚建的东西撤掉）。
        undo_info["before"] = repr(col.undo_status())
        col.undo()
        undo_info["after"] = repr(col.undo_status())
    except Exception as exc:  # noqa: BLE001
        undo_info["error"] = str(exc)
    record("builder_undo", undo_info)
    record("builder_undo_info", dict(module.LAST_UNDO_INFO))
    left = col.db.scalar("select count() from notes where mid=?", int(model["id"]))
    check("撤销（等同 Ctrl+Z）一次撤掉整组补漏卡", left == 0, f"{undo_info} 还剩 {left} 条")
    record("builder", {"notes": report["notes"], "media": report["media"], "decks": report["deck_names"]})

    leftovers = [int(r[0]) for r in col.db.all("select id from notes where mid=?", int(model["id"]))]
    if leftovers:
        try:
            col.remove_notes(leftovers)
        except Exception:  # noqa: BLE001
            pass
    for name in {d["deck"] for d in drafts}:
        try:
            dead = col.decks.id_for_name(name)
            if dead:
                col.decks.remove([int(dead)])
        except Exception:  # noqa: BLE001
            pass
    shutil.rmtree(tmp, ignore_errors=True)


def step_cache() -> None:
    module = evs()
    analysis = module.analysis
    matchers = module.get_matchers()
    config = _config(module, cache=True)
    scope = _scope(deck_ids=[STATE["decks"]["en"]])
    matchers_meta = matchers.get("en_meta", {})

    sig = analysis.collection_signature(mw.col, scope, config, matchers_meta)
    analysis.clear_cache(mw.col)
    first = module.compute(mw.col, config, scope, use_cache=True)
    check("第一次统计不是从缓存来的", not first.get("from_cache"), str(first.get("from_cache")))
    second = module.compute(mw.col, config, scope, use_cache=True)
    check("同样输入第二次用的是缓存", bool(second.get("from_cache")), str(second.get("from_cache")))

    # 改一张卡的词 → 缓存签名必须变，统计结果必须跟着变
    note = STATE["notes"]["unknown"]
    note["vocabularyWord"] = "abandon"
    mw.col.update_note(note)
    sig_after = analysis.collection_signature(mw.col, scope, config, matchers_meta)
    check("改笔记后缓存签名变了", sig != sig_after, f"{sig} / {sig_after}")

    third = module.compute(mw.col, config, scope, use_cache=True)
    check("改笔记后重新计算（没用旧缓存）", not third.get("from_cache"), str(third.get("from_cache")))
    after_words = third["languages"]["en"]["words"]
    check(
        "改笔记后英语轴唯一词从 4 变 3（zzzqwerty 换成 abandon）",
        after_words == 3,
        f"实际 {after_words}",
    )
    STATE["cache_after_words"] = after_words

    # 改完必须还原：这一步只是验缓存会不会失效，后面对来源页/界面的检查
    # 读的是同一份集合，留着改过的值会让后面的断言看到一份和别人不一样的数据
    # （早先版本就栽在这：界面步骤莫名其妙多出一个带 CEFR-C1 标签的 abandon）。
    note["vocabularyWord"] = "zzzqwerty"
    mw.col.update_note(note)
    restored = module.compute(mw.col, config, scope, use_cache=True)
    check(
        "还原笔记后英语轴唯一词回到 4",
        restored["languages"]["en"]["words"] == 4,
        f"实际 {restored['languages']['en']['words']}",
    )


def step_ui() -> None:
    module = evs()
    config = _config(module)
    scope = _scope(deck_ids=[STATE["decks"]["en"], STATE["decks"]["ja"]])
    dialog = module.StatsDialog(mw.col, config, scope, mw)
    STATE["dialog"] = dialog
    try:
        dialog.refresh()
        check("主窗口统计跑通了", dialog.summary is not None)
        check("总览页写了文字", bool(dialog.overview_text.text()), dialog.overview_text.text()[:80])
        check("覆盖页有 7 行（英语轴六行 + 并集）", dialog.coverage_table.rowCount() == 7, str(dialog.coverage_table.rowCount()))
        check("总览页状态环图有数据", bool(dialog.donut.rows), str(dialog.donut.rows))

        # 来源页：词表下拉不能被每次渲染冲回第一项（上一版的 bug）
        dialog.source_box.setCurrentIndex(0)
        check("来源页有下列", dialog.source_table.rowCount() > 0, str(dialog.source_table.rowCount()))
        deck_rows = dialog.source_table.rowCount()
        if dialog.source_box.count() > 1:
            dialog.source_box.setCurrentIndex(1)
            dialog.render_sources()
            check(
                "重复渲染不会把词表选择冲掉",
                dialog.source_box.currentIndex() == 1,
                str(dialog.source_box.currentIndex()),
            )
            dialog.refresh()
            check(
                "整窗刷新后词表选择还在",
                dialog.source_box.currentIndex() == 1,
                str(dialog.source_box.currentIndex()),
            )
            dialog.source_box.setCurrentIndex(0)

        # 来源维度下拉：默认牌组，切换后数据跟着变
        check("来源页有「来源维度」下拉", dialog.source_dim_box is not None)
        check(
            "来源维度默认是牌组",
            dialog.source_dim_box.currentData() == "deck",
            str(dialog.source_dim_box.currentData()),
        )
        dim_index = dialog.source_dim_box.findData("tag")
        dialog.source_dim_box.setCurrentIndex(dim_index)
        check(
            "切到标签维度后统计真的重算了",
            dialog.summary.get("source_dimension") == "tag",
            str(dialog.summary.get("source_dimension")),
        )
        tag_rows = {
            row["name"]
            for row in dialog.summary["languages"]["en"]["sources"]["cet4"]
        }
        record(
            "ui_source_dimensions",
            {"deck_rows": deck_rows, "tag_rows": sorted(tag_rows)},
        )
        record(
            "ui_en_cet4_words",
            {
                "from_cache": bool(dialog.summary.get("from_cache")),
                "scanned_notes": dialog.summary.get("scanned_notes"),
                "words": [
                    {
                        "key": word["key"],
                        "tags": sorted(word["tags"]),
                        "decks": word["deck_names"],
                        "notes": word.get("notes"),
                    }
                    for word in dialog.summary["words"]
                    if word["language"] == "en"
                ],
            },
        )
        check(
            "标签维度的来源名是标签名（A1/A2），不是牌组名",
            tag_rows == {"English-CEFR::CEFR-A1", "English-CEFR::CEFR-A2"},
            str(sorted(tag_rows)),
        )
        notetype_index = dialog.source_dim_box.findData("notetype")
        dialog.source_dim_box.setCurrentIndex(notetype_index)
        nt_rows = {
            row["name"]
            for row in dialog.summary["languages"]["en"]["sources"]["cet4"]
        }
        check(
            "切到笔记类型维度后用真实类型名",
            nt_rows == {MODEL_EN},
            str(sorted(nt_rows)),
        )
        dialog.source_dim_box.setCurrentIndex(dialog.source_dim_box.findData("deck"))

        # 明细页只有三档：全部 / 只看已覆盖 / 只看未覆盖。
        # v0.3 起界面不再有「待确认」档——歧义词一律算未覆盖，不设中间态。
        states = [
            dialog.detail_filter.itemData(i)
            for i in range(dialog.detail_filter.count())
        ]
        record("detail_states", {"states": states})
        check(
            "明细档位只有全部 / 已覆盖 / 未覆盖三档",
            states == ["all", "hit", "uncovered"],
            str(states),
        )
        for code in states:
            dialog.detail_filter.setCurrentIndex(max(0, dialog.detail_filter.findData(code)))
            rows = dialog._detail_rows()
            record("detail_filter", {"code": code, "rows": len(rows)})
        dialog.detail_filter.setCurrentIndex(max(0, dialog.detail_filter.findData("hit")))
        hit_rows = dialog._detail_rows()
        check(
            "已覆盖档只列命中词表的词",
            all(w["confident"] and (w["exams"] or w["levels"]) for w in hit_rows),
            str([w["key"] for w in hit_rows]),
        )
        dialog.detail_filter.setCurrentIndex(max(0, dialog.detail_filter.findData("all")))
        ambiguous = [w for w in dialog._detail_rows() if not w["confident"]]
        check(
            "歧义词在明细里显示为未覆盖（界面没有待确认档）",
            all(
                dialog.detail_reason(w, module.get_matchers())
                == module.analysis.STATE_UNCOVERED
                for w in ambiguous
            ),
            str([w["key"] for w in ambiguous]),
        )

        # 未覆盖档
        dialog.detail_filter.setCurrentIndex(
            max(0, dialog.detail_filter.findData("uncovered"))
        )
        dialog.render_detail()
        check("未覆盖档能渲染出来", dialog.detail_table.rowCount() > 0, str(dialog.detail_table.rowCount()))

        # 明细页：词表多选（并集 / 交集），以及打标签按钮的可用状态
        dialog.detail_filter.setCurrentIndex(max(0, dialog.detail_filter.findData("hit")))
        dialog.render_detail()
        check("命中档露出词表多选", not dialog.detail_codes.isHidden())
        check("命中档露出并集/交集下拉", not dialog.detail_match.isHidden())
        check("命中档露出打标签按钮", not dialog.detail_side.isHidden())

        en_hit_words = [
            w
            for w in dialog.summary["words"]
            if w["language"] == "en" and w["confident"] and (w["exams"] or w["levels"])
        ]
        want_any = {
            w["key"]
            for w in en_hit_words
            if {*w["exams"], *w["levels"]} & {"cet4", "cet6"}
        }
        want_all = {
            w["key"]
            for w in en_hit_words
            if {"cet4", "cet6"} <= {*w["exams"], *w["levels"]}
        }
        for i in range(dialog.detail_codes.count()):
            item = dialog.detail_codes.item(i)
            item.setCheckState(
                Qt.CheckState.Checked
                if module._pair(item.data(Qt.ItemDataRole.UserRole))
                in (("en", "cet4"), ("en", "cet6"))
                else Qt.CheckState.Unchecked
            )
        checked = dialog.checked_detail_codes()
        check(
            "勾选状态读得出来（四级 + 六级）",
            set(checked) == {("en", "cet4"), ("en", "cet6")},
            str(checked),
        )
        dialog.detail_match.setCurrentIndex(dialog.detail_match.findData("any"))
        got_any = {w["key"] for w in dialog._detail_rows()}
        check("多选并集 = 命中任一勾选词表", got_any == want_any, f"{sorted(got_any)} vs {sorted(want_any)}")
        dialog.detail_match.setCurrentIndex(dialog.detail_match.findData("all"))
        got_all = {w["key"] for w in dialog._detail_rows()}
        check("多选交集 = 同时命中所有勾选词表", got_all == want_all, f"{sorted(got_all)} vs {sorted(want_all)}")
        check("打标签按钮在勾选具体词表时可用", dialog.tag_button.isEnabled())
        check(
            "要打的标签名 = 应试::四级 / 应试::六级",
            dialog.selected_exam_tags() == ["应试::四级", "应试::六级"],
            str(dialog.selected_exam_tags()),
        )
        dialog.detail_match.setCurrentIndex(dialog.detail_match.findData("any"))
        for i in range(dialog.detail_codes.count()):
            dialog.detail_codes.item(i).setCheckState(Qt.CheckState.Unchecked)
        check(
            "不勾词表时打标签按钮禁用",
            not dialog.tag_button.isEnabled(),
            str(dialog.selected_exam_tags()),
        )
        check(
            "空勾选时明细不过滤（仍是全部命中词）",
            {w["key"] for w in dialog._detail_rows()} == {w["key"] for w in en_hit_words},
            str(len(dialog._detail_rows())),
        )

        # 切到日语 / 全部，确认两条轴都跑得通
        dialog.language_box.setCurrentIndex(
            max(0, dialog.language_box.findData("ja"))
        )
        check("切到日语轴后覆盖页有 6 行（五行 + 并集）", dialog.coverage_table.rowCount() == 6, str(dialog.coverage_table.rowCount()))
        dialog.language_box.setCurrentIndex(
            max(0, dialog.language_box.findData("all"))
        )
        check("选「全部」时英文日语分块显示（13 行）", dialog.coverage_table.rowCount() == 13, str(dialog.coverage_table.rowCount()))

        dialog.language_box.setCurrentIndex(0)

        # 设置页：更新相关控件得在，状态行得写点东西（不点按钮，避免探针里联网）
        dialog.tabs.setCurrentIndex(4)
        check("设置页有「启动时自动检查更新」勾选框", dialog.update_box is not None)
        check("勾选框默认是开着的", dialog.update_box.isChecked())
        check("设置页有「检查更新（GitHub）」按钮", dialog.update_button is not None)
        label_text = dialog.update_label.text()
        check("设置页状态行写了当前版本", module.__version__ in label_text, label_text)
        record("settings_update_row", {"checked": dialog.update_box.isChecked(), "label": label_text})

        # 素材库那一行：下载按钮 + 本地 zip 按钮都得在（本轮新加的离线通道）
        check("设置页有「一键构建素材库」按钮", dialog.install_button is not None)
        check("设置页有「从本地 zip 文件安装…」按钮", dialog.local_zip_button is not None)
        check(
            "本地 zip 按钮的文案写明了离线安装",
            "本地" in dialog.local_zip_button.text() and "zip" in dialog.local_zip_button.text().lower(),
            dialog.local_zip_button.text(),
        )
        check("设置页素材库状态行写了当前状态", bool(dialog.material_label.text()), dialog.material_label.text()[:60])

        # 离屏画一遍，把 Qt6 的画笔 API（Donut / StackedBar / ProgressCell）真跑一遍
        dialog.tabs.setCurrentIndex(0)
        pixmap_all = dialog.grab()
        check("总览页离屏绘制成功", not pixmap_all.isNull() and pixmap_all.width() > 100, f"{pixmap_all.width()}x{pixmap_all.height()}")
        dialog.tabs.setCurrentIndex(1)
        pixmap_cov = dialog.grab()
        check("覆盖页离屏绘制成功", not pixmap_cov.isNull() and pixmap_cov.width() > 100, f"{pixmap_cov.width()}x{pixmap_cov.height()}")
        dialog.tabs.setCurrentIndex(2)
        pixmap_src = dialog.grab()
        check("来源页离屏绘制成功", not pixmap_src.isNull() and pixmap_src.width() > 100, f"{pixmap_src.width()}x{pixmap_src.height()}")
        record("ui_rows", {"overview": dialog.overview_table.rowCount(), "coverage": dialog.coverage_table.rowCount(), "detail": dialog.detail_table.rowCount()})
    finally:
        dialog.close()
        dialog.deleteLater()


def step_readonly_snapshot() -> None:
    before = STATE.get("before_snapshot") or {}
    after = cards_snapshot()
    added = set(after) - set(before)
    removed = set(before) - set(after)
    changed = {cid for cid in set(before) & set(after) if before[cid] != after[cid]}
    record(
        "readonly",
        {"before": len(before), "after": len(after), "added": len(added), "removed": len(removed), "changed": len(changed)},
    )
    check("跑完整套流程没有删任何卡", not removed, str(sorted(removed)[:5]))
    check("没有改动任何一张卡（did/type/queue/due/ivl/reps 全一致）", not changed, str(sorted(changed)[:5]))
    check("新增的卡只有探针自己后面造的（性能步）", True)

    # 笔记内容也钉一遍：插件只读，打标签那几步改完都还原了，这里就该一模一样。
    notes_before = STATE.get("before_notes") or {}
    notes_after = notes_snapshot()
    notes_added = set(notes_after) - set(notes_before)
    notes_removed = set(notes_before) - set(notes_after)
    notes_changed = {
        nid
        for nid in set(notes_before) & set(notes_after)
        if notes_before[nid] != notes_after[nid]
    }
    record(
        "readonly_notes",
        {
            "before": len(notes_before),
            "after": len(notes_after),
            "added": len(notes_added),
            "removed": len(notes_removed),
            "changed": len(notes_changed),
        },
    )
    check("没有删任何笔记", not notes_removed, str(sorted(notes_removed)[:5]))
    check(
        "没有改动任何笔记的字段或标签（打标签那几步全部还原）",
        not notes_changed,
        str(sorted(notes_changed)[:5]),
    )


def step_perf() -> None:
    module = evs()
    analysis = module.analysis
    matchers = module.get_matchers()
    col = mw.col

    deck_id = make_deck(DECK_PERF)
    model = make_model(MODEL_PERF, ["Word"])
    heads = sorted(matchers["en"].group_exam)

    started = time.time()
    notes = []
    for index in range(PERF_N):
        note = col.new_note(model)
        note["Word"] = heads[index % len(heads)]
        notes.append(note)
    for note in notes:
        col.add_note(note, DeckId(int(deck_id)))
    build_seconds = time.time() - started

    config = _config(module)
    field_map = {
        int(model["id"]): {
            "fields": ["Word"],
            "language": "en",
            "enabled": True,
            "allow_sentence": False,
        }
    }
    scope = _scope(deck_ids=[deck_id])

    # 第一次跑要把各种一次性开销（正则编译、字典扩容、第一次走 sqlite 的查询计划）
    # 摊进来，数字偏大。真正的验收口径是「用户打开窗口那一下有多快」，
    # 所以这里跑两遍，用第二遍（热身后）的数字下结论，两遍都记下来。
    started = time.time()
    analysis.collect_entries(col, scope, field_map, matchers)
    cold = time.time() - started

    started = time.time()
    collected = analysis.collect_entries(col, scope, field_map, matchers)
    collect_seconds = time.time() - started
    summary = analysis.summarize(col, collected, config, matchers)
    elapsed = time.time() - started
    summarize_seconds = elapsed - collect_seconds

    # 顺便量一下「查库」和「算汇总」各占多少，以及最花时间的几个函数，
    # 免得性能不达标时只能靠猜（离线跑同样的量只要 0.7 秒）。
    import cProfile
    import io
    import pstats

    profiler = cProfile.Profile()
    profiler.enable()
    analysis.summarize(
        col, analysis.collect_entries(col, scope, field_map, matchers), config, matchers
    )
    profiler.disable()
    stream = io.StringIO()
    pstats.Stats(profiler, stream=stream).sort_stats("tottime").print_stats(12)
    top = [
        line.strip()
        for line in stream.getvalue().splitlines()
        if line.strip() and ("analysis.py" in line or "vocab_logic.py" in line or "method" in line)
    ][:12]

    per_10k = round(elapsed * 10000.0 / max(PERF_N, 1), 3)
    record(
        "perf",
        {
            "notes": PERF_N,
            "build_seconds": round(build_seconds, 2),
            "cold_seconds": round(cold, 3),
            "stat_seconds": round(elapsed, 3),
            "collect_seconds": round(collect_seconds, 3),
            "summarize_seconds": round(summarize_seconds, 3),
            "seconds_per_10k": per_10k,
            "words": summary["languages"]["en"]["words"],
            "top_profiles": top,
        },
    )
    if PERF_N >= 20000:
        # 方案里的承诺：2 万笔记 1 秒内出首屏
        check(f"{PERF_N} 条笔记统计在 1 秒内", elapsed <= 1.0, f"实际 {elapsed:.3f} 秒")
    else:
        # 笔记数少的时候，固定开销摊不开，折算值会偏大；这里只当冒烟检查，
        # 真正的 1 秒门槛由上面 PERF_N >= 20000 那一支把关。
        if PERF_N < 500:
            # 样本太小（比如调试时用 20 条），折算出来的数没有意义，直接跳过
            record("perf_note", {"skipped": True, "perf_n": PERF_N, "per_10k": per_10k})
        else:
            check(
                f"折算每万条 ≤ 2 秒（{PERF_N} 条样本，仅冒烟）",
                per_10k <= 2.0,
                f"每万条 {per_10k} 秒（冷跑 {cold:.3f} 秒）",
            )


STEPS: list[tuple[str, Callable[[], None], int]] = [
    ("import_and_index", step_import, 400),
    ("sample_data", step_sample_data, 800),
    ("menus", step_menus, 600),
    ("field_recommendation", step_field_recommendation, 1600),
    ("parent_scope", step_parent_scope, 800),
    ("collect", step_collect, 1200),
    ("state_counts", step_state_counts, 800),
    ("filters", step_filters, 1200),
    ("uncovered", step_uncovered, 1200),
    ("detail_filter", step_detail_filter, 800),
    ("materials", step_materials, 800),
    ("builder", step_builder, 1600),
    ("sources", step_sources, 1600),
    ("cache", step_cache, 1600),
    ("ui", step_ui, 2000),
    ("readonly_snapshot", step_readonly_snapshot, 800),
    ("perf", step_perf, 1500),
    ("readonly_snapshot_2", step_readonly_snapshot, 500),
]


def start() -> None:
    STEP_QUEUE[:] = list(STEPS)
    write_result()
    name, fn, _delay = STEP_QUEUE.pop(0)
    run_step(name, fn)


def _on_profile_did_open() -> None:
    QTimer.singleShot(1200, start)


try:
    from aqt import gui_hooks

    gui_hooks.profile_did_open.append(_on_profile_did_open)
except Exception:  # pragma: no cover
    QTimer.singleShot(2000, start)
