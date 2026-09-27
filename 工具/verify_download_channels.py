"""用**插件自己的下载代码**真拉一次 GitHub 附件，证明「新设备一键构建素材库」这条路走得通。

为什么需要它：本机网络连不上 github.com:443，raw.githubusercontent.com 也时常 ECONNRESET，
只有 api.github.com 稳定。所以插件里加了「问 API 要附件接口地址 + Accept: application/octet-stream」
的备用通道。这个脚本不模拟、不假设，直接调 ``源码/update_logic.py`` 里的函数下载真文件。

跑法（联网，需要授权）：
    python 工具\\verify_download_channels.py            # 只验分发包（5.4 MB）
    python 工具\\verify_download_channels.py --audio    # 再加一次音频包的「Range 取头部」验证
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
SRC = os.path.join(PROJECT, "源码")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import update_logic as U  # noqa: E402


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def head_of(url: str, accept: str, want: int = 64) -> tuple:
    """只取前 want 个字节，确认附件流真的能读（不下整包）。"""
    import urllib.request

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"{U.USER_AGENT_PREFIX}/verify",
            "Accept": accept,
            "Range": f"bytes=0-{want - 1}",
        },
    )
    with urllib.request.urlopen(request, timeout=U.MATERIALS_TIMEOUT) as response:
        status = getattr(response, "status", 200)
        declared = int(response.headers.get("Content-Range", "bytes 0-0/0").split("/")[-1] or 0)
        return status, response.read(want), declared


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", action="store_true", help="额外验证音频包的头部能读到")
    args = parser.parse_args()
    problems: list = []

    # 1) 版本号：raw 优先，连不上就走 API（至少要知道线上是哪个 tag）
    version = U.fetch_latest_version(version="0.3.0")
    print(f"线上版本号（raw 或 API）：{version or '取不到'}")
    if not version:
        problems.append("版本号取不到：raw 和 Release 都不通")
        try:
            import json

            payload = json.loads(U.fetch_json(U.release_api_url()).decode("utf-8", "replace"))
            version = str(payload.get("tag_name") or "").lstrip("v")
            print(f"　→ 但 API 拿到了 tag：{payload.get('tag_name')}（可用作兜底）")
            problems.pop()
        except Exception as exc:  # noqa: BLE001
            print(f"　→ API 也拿不到：{exc}")

    # 2) 分发包：走 package_urls_with_api（Release 直链 → raw → API 附件接口）
    api_url = U.api_asset_url(U.UPDATE_ASSET, version="0.3.0")
    print(f"\nAPI 给出的分发包附件地址：{api_url or '（拿不到）'}")
    if not api_url:
        problems.append("API 拿不到分发包附件地址")
    else:
        target = os.path.join(tempfile.mkdtemp(), U.UPDATE_ASSET)
        try:
            wrote = U.download_to_file(
                api_url,
                target,
                timeout=U.PACKAGE_TIMEOUT,
                retries=1,
                version="0.3.0",
                accept=U.BINARY_ACCEPT,
                resume=True,
            )
            local = os.path.join(PROJECT, U.UPDATE_ASSET)
            same = os.path.isfile(local) and sha256_of(target) == sha256_of(local)
            print(f"下载分发包：{wrote} 字节；和本机打包的那份 sha256 一致 = {same}")
            if not same:
                problems.append("下载到的分发包和本机打包的不一致")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"分发包下载失败：{exc}")
            print(f"下载分发包失败：{exc}")

    # 3) 音频包：只取头部，证明附件流能读（整包 208 MB 不适合在验证脚本里拉）
    if args.audio:
        audio_url = U.api_asset_url(U.MATERIALS_ASSET, version="0.3.0")
        print(f"\nAPI 给出的音频包附件地址：{audio_url or '（拿不到）'}")
        if not audio_url:
            problems.append("API 拿不到音频包附件地址")
        else:
            try:
                status, head, declared = head_of(audio_url, U.BINARY_ACCEPT)
                print(
                    f"音频包头部：HTTP {status}，取到 {len(head)} 字节，"
                    f"声明总大小 {declared} 字节（清单里是 218311152）"
                )
                if declared and declared != 218311152:
                    problems.append(f"音频包大小对不上：{declared}")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"音频包头部读取失败：{exc}")
                print(f"音频包头部读取失败：{exc}")

    print("\n结论：" + ("全部通过" if not problems else "有问题 → " + "；".join(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
