"""应试词汇覆盖统计 · GitHub 自动更新的纯逻辑层。

这一层**不 import anki**，只做可以离线测的东西：版本号比较、下载地址拼装、
分块下载与重试、分发包完整性校验、提示文案。
真正弹窗、装插件、记状态在 ``__init__.py`` 里。

更新只碰两个文件，都在仓库根目录：

- ``version.txt``：当前发布版本号，例如 ``0.3.0``；很小，直接放仓库根目录，走 raw。
- ``exam_vocab_stats.ankiaddon``：分发包本体。12.5MB，超过仓库单文件 5MB 的闸门，
  所以**不进仓库**，只挂在 GitHub Release 上，走 ``releases/latest/download``。

两条通道都是「主路 + 兜底」：版本号先 raw 再 Release，分发包先 Release 再 raw
（raw 这条路在自建仓库把包塞进去时才有用）。除这几个地址外插件不联网，
也不发送任何卡片内容。
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Optional

# 发布仓库。改这里之前先确认仓库真存在，否则每次启动都会静默失败一次。
UPDATE_REPO = "creeperboo/anki-exam-vocab-stats"
UPDATE_BRANCH = "main"
# 仓库根目录与 Release 附件用同一个文件名（插件内置的兜底地址认这个名）
UPDATE_ASSET = "exam_vocab_stats.ankiaddon"
UPDATE_VERSION_FILE = "version.txt"
# 制卡素材里的音频包（300MB 级别，仓库本体放不下，只能挂 Release 附件）
MATERIALS_ASSET = "exam_materials_audio.zip"
USER_AGENT_PREFIX = "anki-exam-vocab-stats"

# 启动时最多一天查一次
UPDATE_INTERVAL_SECONDS = 86400

# 小文件（版本号）给短超时；分发包大一点，但 raw 卡住时也别让用户干等
VERSION_TIMEOUT = 8
PACKAGE_TIMEOUT = 25
PACKAGE_RETRIES = 1
# 音频包 200MB 级别，链路慢得多：超时放宽、允许更多次重试 + 断点续传
MATERIALS_TIMEOUT = 60
MATERIALS_RETRIES = 3
CHUNK_SIZE = 64 * 1024
MIN_PACKAGE_BYTES = 512

# 「下载函数」的统一形状：吃一个 url，吐 bytes。测试里注入假的。
DownloadFn = Callable[[str], bytes]


def _log(message: str) -> None:
    """日志走 print：Anki 会把它收进自己的日志，不弹窗、不打扰用户。"""
    print(f"[应试词汇覆盖统计] {message}")


# ------------------------------------------------------------------ 版本号


def version_tuple(text: Any) -> tuple:
    """把 "0.10.2" 这种版本号变成可比较的元组；认不出就当 0。"""
    numbers = re.findall(r"\d+", str(text or ""))
    if not numbers:
        return (0,)
    return tuple(int(number) for number in numbers[:4])


def is_newer(remote: Any, local: Any) -> bool:
    """远端版本是不是比本地新。"""
    return version_tuple(remote) > version_tuple(local)


def parse_version(text: str) -> str:
    """从 __init__.py 的正文里抠出 __version__ = "x.y.z"。"""
    match = re.search(r"""^__version__\s*=\s*["']([^"']+)["']""", text or "", re.M)
    return match.group(1).strip() if match else ""


# ------------------------------------------------------------------ 地址


def repo_ready(repo: str = UPDATE_REPO) -> bool:
    """仓库地址填好没有（没填好就别联网了）。"""
    return bool(repo) and "TODO" not in repo


def raw_url(
    name: str, repo: str = UPDATE_REPO, branch: str = UPDATE_BRANCH
) -> str:
    """raw.githubusercontent 上的文件。"""
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{name}"


def release_asset_url(name: str, repo: str = UPDATE_REPO) -> str:
    """最新 Release 的附件地址（raw 传一半断掉时的兜底通道）。"""
    return f"https://github.com/{repo}/releases/latest/download/{name}"


def materials_url(repo: str = UPDATE_REPO) -> str:
    """音频包下载地址：只走 Release 附件（体积大，仓库本体放不下）。"""
    return release_asset_url(MATERIALS_ASSET, repo)


def download_urls(
    name: str, repo: str = UPDATE_REPO, branch: str = UPDATE_BRANCH
) -> list:
    """先 raw，再 Release 附件。"""
    if not repo_ready(repo):
        return []
    return [raw_url(name, repo, branch), release_asset_url(name, repo)]


def package_urls(
    asset: str = UPDATE_ASSET, repo: str = UPDATE_REPO, branch: str = UPDATE_BRANCH
) -> list:
    """分发包的下载地址：**先 Release 附件，再 raw 兜底**。

    和 ``version.txt`` 不一样：分发包 12.5MB，超过仓库单文件 5MB 的发布闸门，
    所以仓库本体里根本没有这个文件，raw 那条路只会 404。Release 附件才是正路，
    raw 留着只是给「有人把包塞进了仓库」这种自建情况兜底。
    """
    if not repo_ready(repo):
        return []
    return [release_asset_url(asset, repo), raw_url(asset, repo, branch)]


def release_url(latest: str, repo: str = UPDATE_REPO) -> str:
    """新版本对应的 Release 页面（标签按 v0.2.0 约定，取不到就退回仓库 releases 页）。"""
    if not repo_ready(repo):
        return ""
    base = f"https://github.com/{repo}/releases"
    tag = str(latest or "").strip()
    if not tag:
        return base
    if not tag.startswith("v"):
        tag = "v" + tag
    return f"{base}/tag/{tag}"


# ------------------------------------------------------------------ 下载


def fetch(
    url: str,
    *,
    timeout: int = 10,
    retries: int = 2,
    version: str = "",
) -> bytes:
    """下载一个小文件；分块读 + 失败重试（raw 偶尔会卡住）。"""
    import urllib.request

    user_agent = f"{USER_AGENT_PREFIX}/{version}" if version else USER_AGENT_PREFIX
    last_error: Optional[BaseException] = None
    for attempt in range(max(0, retries) + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": user_agent})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                chunks: list = []
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks)
        except Exception as exc:
            last_error = exc
            _log(f"下载失败（第 {attempt + 1} 次）：{url}（{exc}）")
            if attempt < retries:
                time.sleep(1.0 + attempt)
    if last_error is not None:
        raise last_error
    return b""


def download_to_file(
    url: str,
    path: str,
    *,
    timeout: int = PACKAGE_TIMEOUT,
    retries: int = PACKAGE_RETRIES,
    version: str = "",
    progress=None,
    resume: bool = False,
) -> int:
    """把大文件分块写到磁盘（音频包级别），返回写入的字节数。

    ``progress(done, total)`` 可选，``total`` 拿不到 Content-Length 时是 0。
    ``resume=True`` 时重试会带 ``Range: bytes=<已下载>-`` 续写同一个临时文件
    （断点续传）；所有重试都失败才把残留文件删掉，免得留下半截包被当成好包。
    """
    import os
    import urllib.request

    user_agent = f"{USER_AGENT_PREFIX}/{version}" if version else USER_AGENT_PREFIX
    last_error: Optional[BaseException] = None
    for attempt in range(max(0, retries) + 1):
        start = 0
        if resume and os.path.exists(path):
            try:
                start = os.path.getsize(path)
            except OSError:
                start = 0
        if not start:
            # 不续传（或没有残留）时，先清干净，按全新文件写
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        done = start
        headers = {"User-Agent": user_agent}
        if start:
            headers["Range"] = f"bytes={start}-"
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", None)
                if start and status not in (206, None):
                    # 服务器不认 Range（返回 200 整个文件）：从头写
                    start = done = 0
                length = int(response.headers.get("Content-Length") or 0)
                total = (start + length) if length else 0
                if progress:
                    try:
                        progress(done, total)
                    except Exception:  # noqa: BLE001
                        pass
                with open(path, "ab" if start else "wb") as handle:
                    while True:
                        chunk = response.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        handle.write(chunk)
                        done += len(chunk)
                        if progress:
                            try:
                                progress(done, total)
                            except Exception:  # noqa: BLE001
                                pass
            return done
        except Exception as exc:
            last_error = exc
            _log(f"下载大文件失败（第 {attempt + 1} 次）：{url}（{exc}）")
            if attempt < retries:
                time.sleep(1.0 + attempt)
    # 所有重试都没成：把残留清掉，免得半截包被当成完整包
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
    if last_error is not None:
        raise last_error
    return 0


def fetch_latest_version(
    download: Optional[DownloadFn] = None,
    repo: str = UPDATE_REPO,
    branch: str = UPDATE_BRANCH,
    version: str = "",
) -> str:
    """取线上版本号；两条地址都拿不到就返回空串。"""
    if not repo_ready(repo):
        return ""
    download = download or (
        lambda url: fetch(url, timeout=VERSION_TIMEOUT, version=version)
    )
    for url in download_urls(UPDATE_VERSION_FILE, repo, branch):
        try:
            text = download(url).decode("utf-8", "replace").strip()
        except Exception as exc:
            _log(f"取版本号失败（{url}）：{exc}")
            continue
        if text:
            return text
    return ""


def looks_like_package(data: bytes) -> bool:
    """下到的东西是不是一个像样的 .ankiaddon（防半截包）。"""
    if not data or len(data) < MIN_PACKAGE_BYTES or not data.startswith(b"PK"):
        return False
    try:
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except Exception:
        return False
    return "manifest.json" in names and "__init__.py" in names


def fetch_package(
    download: Optional[DownloadFn] = None,
    repo: str = UPDATE_REPO,
    branch: str = UPDATE_BRANCH,
    asset: str = UPDATE_ASSET,
    version: str = "",
) -> bytes:
    """下载分发包；raw 挂了就换 Release 附件。两条都不行才报错。"""
    if not repo_ready(repo):
        raise RuntimeError("没有配置更新仓库")
    download = download or (
        lambda url: fetch(
            url,
            timeout=PACKAGE_TIMEOUT,
            retries=PACKAGE_RETRIES,
            version=version,
        )
    )
    last_error: Optional[BaseException] = None
    for url in package_urls(asset, repo, branch):
        try:
            data = download(url)
        except Exception as exc:
            last_error = exc
            _log(f"下载分发包失败（{url}）：{exc}")
            continue
        if looks_like_package(data):
            _log(f"分发包下载成功（{url}，{len(data)} 字节）")
            return data
        last_error = RuntimeError(f"下载到的分发包不完整（{len(data)} 字节）")
        _log(f"分发包不完整（{url}，{len(data)} 字节），换个地址再试")
    if last_error is not None:
        raise last_error
    raise RuntimeError("没有可用的下载地址")


# ------------------------------------------------------------------ 文案


def update_prompt_text(
    latest: str, current: str, repo: str = UPDATE_REPO
) -> str:
    """更新提示的正文。"""
    text = f"应试词汇覆盖统计有新版本：{latest}（当前 {current}）"
    url = release_url(latest, repo)
    if url:
        text += f"\n\n这次改了什么：{url}"
    return text + "\n\n现在下载并安装吗？装完要重启 Anki 才生效。"


def pending_restart_text(version: str, current: str) -> str:
    """新版已经装到磁盘、但还没重启时的提示。"""
    return (
        f"应试词汇覆盖统计：新版 {version} 已经装好了，重启 Anki 后生效"
        f"（当前运行中的还是 {current}）"
    )


def describe_state(state: dict, current: str) -> str:
    """设置页那一行状态文字。state 里没有的项就显示「?」。"""
    state = state or {}
    checked = state.get("checked_at")
    try:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(checked)))
    except (TypeError, ValueError):
        when = "还没查过"
    latest = state.get("latest") or "未知"
    return f"当前版本 {current}　|　上次检查 {when}　|　线上版本 {latest}"


def materials_failure_text(urls, error: Optional[BaseException] = None) -> str:
    """音频包下载失败时给用户看的话：说清试过哪些地址，并指向离线通道。"""
    lines = ["音频包下载失败：仓库或 Release 附件现在取不到。", "", "试过这些地址："]
    lines += [f"　{url}" for url in (urls or []) if url]
    lines += [
        "",
        "常见原因：网络被挡、仓库刚建好附件还没传完、下载中途被掐断。",
        "可以改走离线通道：设置页 →「从本地 zip 文件安装…」，",
        "选中本机的 exam_materials_audio.zip 即可，全程不联网。",
    ]
    if error is not None:
        lines.append(f"（错误信息：{error}）")
    return "\n".join(lines)
