"""把「应试词汇覆盖统计」插件安装到 Anki，并重新打包 .ankiaddon。

用法（在本文件夹里执行）：
    python 安装到Anki.py                     # 自动找 Anki 插件目录并安装
    python 安装到Anki.py --addons "路径"      # 手动指定 addons21 目录
    python 安装到Anki.py --package-only      # 只重新打包，不安装

装完记得重启 Anki。插件只在 Anki 启动时加载。
"""

import argparse
import os
import shutil
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "源码")
# 分发包文件名：仓库根目录和 Release 附件都用这个名，插件里的更新地址认它
# （源码/tests/test_update_logic.py 会核对它和 update_logic.UPDATE_ASSET 一致）
PACKAGE_NAME = "exam_vocab_stats.ankiaddon"
PACKAGE = os.path.join(HERE, PACKAGE_NAME)
SOURCE_ZIP = os.path.join(HERE, "应试词汇统计插件-源码.zip")
FOLDER_NAME = "exam_vocab_stats"
VERSION_FILE = os.path.join(HERE, "version.txt")

# 放进 .ankiaddon 的文件（路径用正斜杠，zip 条目也必须是正斜杠）
ADDON_FILES = [
    "manifest.json",
    "config.json",
    "config.md",
    "README.md",
    "__init__.py",
    "analysis.py",
    "vocab_logic.py",
    "builder.py",
    "resources.py",
    "update_logic.py",
    "hook_guard.py",
    "version.txt",
    "操作指南.txt",
    "data/exam_index.json.gz",
    "data/lemma_index.json.gz",
    "data/jlpt_index.json.gz",
    "data/ja_deform.json.gz",
    "data/en_materials.json.gz",
    "data/ja_materials.json.gz",
    "data/materials_manifest.json",
]

# 源码压缩包里额外包含的东西
SOURCE_EXTRA = [
    "安装到Anki.py",
    "安装到Anki.cmd",
    "测试.cmd",
    "一键上传到GitHub.cmd",
    ".gitignore",
    "项目说明.md",
    "应试词汇统计插件-设计方案.md",
    "工具/构建词库索引.py",
    "工具/构建素材库.py",
    "工具/词典提取.py",
    "工具/提取变形规则.py",
    "工具/查看词库索引.py",
    "工具/本机素材探针.py",
    "工具/本机素材探针2.py",
    "工具/词典探针.py",
    "工具/缺口探针.py",
    "工具/缺口分表统计.py",
    "工具/例句缺口探针.py",
    "工具/英语缺口分类.py",
    "工具/fetch.mjs",
    "工具/索引构建记录.md",
    "工具/素材库构建记录.md",
    "工具/anki_probe.py",
    "工具/run_anki_probe.py",
    "工具/真实集合抽查.py",
    "源码/tests/test_vocab_logic.py",
    "源码/tests/test_update_logic.py",
    "源码/tests/test_v03.py",
    "源码/tests/test_v05.py",
    "源码/tests/test_v06.py",
    "_维护记录/当前状态.md",
]

# 面向使用者的文本文件（双击打开的那种），统一成 Windows 记事本友好的写法
TEXT_WITH_BOM = ["操作指南.txt"]
TEXT_WITHOUT_BOM = ["安装到Anki.cmd", "测试.cmd", "一键上传到GitHub.cmd"]


def normalize_text(path, bom=True):
    """统一成 UTF-8（可选 BOM）+ CRLF，避免记事本里乱码或整段挤成一行。"""
    with open(path, "rb") as fh:
        data = fh.read()
    text = data.decode("utf-8-sig")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    with open(path, "wb") as fh:
        fh.write(("\ufeff" if bom else "").encode("utf-8") + text.encode("utf-8"))


def tidy_text_files():
    """整理文本编码，并在根目录放一份操作指南与版本号副本。

    根目录的 version.txt 是插件更新时从 GitHub 读的那个文件（raw 路径直接指向
    仓库根目录），所以它必须和源码里的那一份一模一样——这里统一由脚本生成，
    省得两边手改改歪。
    """
    for name in TEXT_WITH_BOM:
        path = os.path.join(SRC, *name.split("/"))
        if os.path.isfile(path):
            normalize_text(path, bom=True)
    guide = os.path.join(SRC, "操作指南.txt")
    if os.path.isfile(guide):
        shutil.copyfile(guide, os.path.join(HERE, "操作指南.txt"))
    version = os.path.join(SRC, "version.txt")
    if os.path.isfile(version):
        with open(version, "r", encoding="utf-8") as fh:
            text = fh.read().strip()
        with open(VERSION_FILE, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print("已写出根目录版本号：%s（%s）" % (VERSION_FILE, text))
    for name in TEXT_WITHOUT_BOM:
        path = os.path.join(HERE, *name.split("/"))
        if os.path.isfile(path):
            normalize_text(path, bom=False)


def default_addons_dir():
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return os.path.join(appdata, "Anki2", "addons21")
    for candidate in (
        os.path.expanduser("~/Library/Application Support/Anki2/addons21"),
        os.path.expanduser("~/.local/share/Anki2/addons21"),
    ):
        if os.path.isdir(candidate):
            return candidate
    return None


def write_zip(path, entries):
    """entries: [(zip 内路径, 磁盘上的绝对路径)]

    压缩包里的时间戳固定成一个常量，这样同样的输入每次打出来的包字节完全一致；
    以后重建只要哈希不变，就能确定内容没动过。
    """
    if os.path.exists(path):
        os.remove(path)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for arcname, disk_path in entries:
            info = zipfile.ZipInfo(arcname, date_time=(2026, 9, 26, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(disk_path, "rb") as fh:
                archive.writestr(info, fh.read())
    print("已打包：%s（%d 字节）" % (path, os.path.getsize(path)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--addons", help="Anki 的 addons21 目录")
    parser.add_argument("--package-only", action="store_true", help="只重新打包，不安装")
    parser.add_argument("--no-source-zip", action="store_true", help="不生成源码压缩包")
    args = parser.parse_args()

    if not os.path.isdir(SRC):
        raise SystemExit("找不到「源码」目录：" + SRC)

    missing = [
        f for f in ADDON_FILES if not os.path.isfile(os.path.join(SRC, *f.split("/")))
    ]
    if missing:
        raise SystemExit("源码目录缺少文件：" + "、".join(missing))

    # 0) 整理文本编码，并生成根目录的操作指南副本
    tidy_text_files()

    # 1) 打包 .ankiaddon
    write_zip(
        PACKAGE,
        [(rel, os.path.join(SRC, *rel.split("/"))) for rel in ADDON_FILES],
    )

    # 2) 顺便打包一份源码（含安装脚本、测试脚本和工具）
    if not args.no_source_zip:
        entries = [(rel, os.path.join(SRC, *rel.split("/"))) for rel in ADDON_FILES]
        for rel in SOURCE_EXTRA:
            disk = os.path.join(HERE, *rel.split("/"))
            if os.path.isfile(disk):
                entries.append((rel, disk))
        write_zip(SOURCE_ZIP, entries)

    if args.package_only:
        return

    # 3) 安装到 addons21
    addons = args.addons or default_addons_dir()
    if not addons or not os.path.isdir(addons):
        raise SystemExit("找不到 Anki 的 addons21 目录，请用 --addons 指定")
    target = os.path.join(addons, FOLDER_NAME)
    os.makedirs(target, exist_ok=True)
    for rel in ADDON_FILES:
        dst = os.path.join(target, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(os.path.join(SRC, *rel.split("/")), dst)
    print("已安装到：%s" % target)
    print("重启 Anki 后生效。")


if __name__ == "__main__":
    main()
