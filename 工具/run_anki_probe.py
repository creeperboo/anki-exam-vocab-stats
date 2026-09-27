"""开发用：在隔离的临时用户配置里启动真实 Anki，跑一遍集成检查。

用法（PowerShell）：

    $py = "C:\\Program Files\\Lenovo\\ModelMgr\\Plugins\\Image\\python.exe"
    & $py "工具\\run_anki_probe.py"                    # 小样本，跑完自动清理临时目录
    & $py "工具\\run_anki_probe.py" --keep             # 保留临时目录和结果，方便排查
    & $py "工具\\run_anki_probe.py" --perf-n 20000     # 性能步骤按 2 万条笔记跑

它做四件事：

1. 用 mktemp 风格建一个临时 user base（**不碰**你真实的 %APPDATA%\\Anki2）；
2. 把 `源码\\*` 当成插件放进 `<base>\\addons21\\exam_vocab_stats\\`（含 data 里的词库索引），
   把 `工具\\anki_probe.py` 当成探针插件放进 `<base>\\addons21\\zz_probe\\`；
3. 造一个干净的 `prefs21.db`（firstRun=False、zh_CN、不联网更新、无同步账号），
   然后用 `Anki.exe -b <base> -p probe` 启动；
4. 等探针把结果 JSON 写出来，打印每一步的结论与 errors。

退出码：0 = 探针跑完且没有断言失败；1 = 有失败或超时（会打印原始错误）。
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "源码"
PROBE = Path(__file__).resolve().parent / "anki_probe.py"
ADDON_PACKAGE = "exam_vocab_stats"

# 放进插件目录的文件（和安装脚本保持一致，不拷测试与打包产物）
ADDON_FILES = (
    "__init__.py",
    "analysis.py",
    "vocab_logic.py",
    "builder.py",
    "resources.py",
    "update_logic.py",
    "hook_guard.py",
    "manifest.json",
    "config.json",
    "config.md",
    "README.md",
    "version.txt",
    "操作指南.txt",
    "data/exam_index.json.gz",
    "data/lemma_index.json.gz",
    "data/jlpt_index.json.gz",
    "data/ja_deform.json.gz",
    "data/en_materials.json.gz",
    "data/ja_materials.json.gz",
    "data/materials_manifest.json",
)

PROFILE_NAME = "probe"


def find_anki() -> Path:
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Anki" / "Anki.exe",
        Path(r"C:\Program Files\Anki\Anki.exe"),
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise SystemExit("找不到 Anki.exe，请用 --anki 指定路径。")


def write_prefs(base: Path) -> Path:
    """造一个「已经用过的」用户配置库，跳过首次运行的语言选择，并且不联网。"""
    db_path = base / "prefs21.db"
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            "create table profiles"
            "(name text primary key collate nocase, data blob not null)"
        )
        meta = {
            "ver": 0,
            "updates": False,  # 不检查更新
            "created": int(time.time()),
            "id": 2026092601,
            "lastMsg": 0,
            "suppressUpdate": "99.0.0",
            "firstRun": False,  # 跳过首次运行的欢迎/语言选择
            "defaultLang": "zh_CN",
            "last_run_version": 0,
            "last_loaded_profile_name": PROFILE_NAME,
            "last_addon_update_check": 0,
        }
        # 这个 profile 里没有任何同步账号信息，所以不会触发同步
        profile = {"numBackups": 0}
        con.execute(
            "insert into profiles values (?, ?)", ("_global", pickle.dumps(meta))
        )
        con.execute(
            "insert into profiles values (?, ?)",
            (PROFILE_NAME, pickle.dumps(profile)),
        )
        con.commit()
    finally:
        con.close()
    return db_path


def unpack_addon(addon_path: Path, dest: Path) -> Path:
    """把打包好的 .ankiaddon 解出来，用它当验证对象（而不是源码目录）。"""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(addon_path) as archive:
        archive.extractall(dest)
    return dest


def stage(base: Path, src: Path = SRC) -> Path:
    addons = base / "addons21"
    package_dir = addons / ADDON_PACKAGE
    probe_dir = addons / "zz_probe"
    package_dir.mkdir(parents=True, exist_ok=True)
    probe_dir.mkdir(parents=True, exist_ok=True)

    missing = [name for name in ADDON_FILES if not (src / name).is_file()]
    if missing:
        raise SystemExit(f"{src} 里缺少文件：{'、'.join(missing)}")
    for name in ADDON_FILES:
        target = package_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / name, target)
    shutil.copy2(PROBE, probe_dir / "__init__.py")
    return base / "evs_probe.json"


def read_result(out_file: Path) -> dict | None:
    try:
        with open(out_file, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="在临时用户配置里跑 Anki 集成探针")
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    parser.add_argument("--base", type=Path, default=None, help="指定临时 base 目录")
    parser.add_argument("--anki", type=Path, default=None, help="Anki.exe 路径")
    parser.add_argument(
        "--perf-n",
        type=int,
        default=2000,
        help="性能步骤造多少条笔记（默认 2000；验收时用 20000）",
    )
    parser.add_argument(
        "--from-ankiaddon",
        type=Path,
        default=None,
        help="用打包好的 .ankiaddon 里的文件来跑（默认用源码目录）",
    )
    parser.add_argument(
        "--from-dir",
        type=Path,
        default=None,
        help="用某个已安装的插件目录（如 %%APPDATA%%\\Anki2\\addons21\\exam_vocab_stats）来跑",
    )
    parser.add_argument("--timeout", type=int, default=600, help="等待秒数")
    args = parser.parse_args()

    anki = args.anki or find_anki()
    base = args.base or Path(tempfile.mkdtemp(prefix="evs_probe_"))
    base.mkdir(parents=True, exist_ok=True)
    src = SRC
    if args.from_ankiaddon:
        if not args.from_ankiaddon.is_file():
            raise SystemExit(f"找不到安装包：{args.from_ankiaddon}")
        src = unpack_addon(args.from_ankiaddon, base / "unpacked_addon")
        print(f"用安装包里的文件验证：{args.from_ankiaddon}")
    elif args.from_dir:
        if not args.from_dir.is_dir():
            raise SystemExit(f"找不到插件目录：{args.from_dir}")
        src = args.from_dir
        print(f"用已安装目录里的文件验证：{src}")
    out_file = stage(base, src)
    write_prefs(base)
    log_file = base / "anki_stdout.log"

    env = dict(os.environ)
    env["EVS_PROBE_OUT"] = str(out_file)
    env["EVS_PROBE_PERF_N"] = str(args.perf_n)
    # 避免和正在运行的 Anki 抢单实例锁
    env["ANKI_SINGLE_INSTANCE_KEY"] = f"evs_probe_{os.getpid()}_{int(time.time())}"

    print(f"临时用户配置：{base}")
    print(f"Anki 可执行文件：{anki}")
    print(f"探针结果：{out_file}")
    print(f"性能步骤笔记数：{args.perf_n}")
    sys.stdout.flush()

    with open(log_file, "wb") as log:
        proc = subprocess.Popen(
            [str(anki), "-b", str(base), "-p", PROFILE_NAME, "-l", "zh_CN"],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    result: dict | None = None
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        time.sleep(1.0)
        data = read_result(out_file)
        if data is not None:
            result = data
            if data.get("finished"):
                break
        if proc.poll() is not None and data is not None and data.get("finished"):
            break

    # 探针自己会关窗口；再给 20 秒让它体面退出
    for _ in range(20):
        if proc.poll() is not None:
            break
        time.sleep(1.0)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    result = read_result(out_file) or result
    print("-" * 60)
    if result is None:
        print("失败：探针没有写出任何结果。")
        print(f"Anki 输出见：{log_file}")
        tail = ""
        try:
            tail = log_file.read_text(encoding="utf-8", errors="replace")[-4000:]
        except Exception:
            pass
        if tail:
            print(tail)
        return 1

    print(f"Anki 版本：{result.get('anki_version') or '未知'}")
    print(f"跑完全部步骤：{bool(result.get('finished'))}")
    print(f"断言条数：{result.get('checks')}")
    for step in result.get("steps", []):
        name = step.get("name")
        if name in (
            "counts",
            "state_counts",
            "perf",
            "readonly",
            "detected",
            "cet4_row",
            "builder",
            "builder_undo",
            "gap_tally",
            "detail_filter",
            "detail_states",
            "materials_counts",
            "materials_manifest",
            "ui_rows",
            "update_meta",
            "hook_names",
            "browser_menu_info",
        ):
            print(f"  · {name}：{json.dumps({k: v for k, v in step.items() if k not in ('name', 'step')}, ensure_ascii=False)}")
        elif name == "filter_case":
            print(f"  · 筛选 {step.get('label')}：插件 {step.get('plugin')} / 直查 {step.get('sql')}")
    errors = result.get("errors") or []
    if errors:
        print(f"断言失败 {len(errors)} 条：")
        for line in errors:
            print(f"  ! {line}")
    else:
        print("断言失败：0 条")
    print(f"ok = {bool(result.get('ok'))}")

    if result.get("finished") and not errors:
        if not args.keep:
            shutil.rmtree(base, ignore_errors=True)
            print("已清理临时用户配置。")
        return 0

    print(f"临时用户配置保留在：{base}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
