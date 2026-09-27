# -*- coding: utf-8 -*-
"""钩子签名防护（纯逻辑，不 import Anki，离线可测）。

背景：Anki 各版本的 ``gui_hooks`` 回调参数个数并不一致。实测 26.09 的
``browser_menus_did_init`` 只传 ``browser`` 一个参数，而插件以前按
``(browser, menu)`` 去挂，结果**一打开卡片浏览器就抛 TypeError**，整个
浏览窗口都打不开（就是这轮要修的崩溃）。

这里的做法很小：注册前用 ``inspect.signature(...).bind()`` 试一遍参数，
对不上就返回 ``None`` 并记一条日志，对得上就照常调用。只拦「参数个数 / 名字
对不上」这一种错；回调内部真正的报错照旧往上抛，免得把真 bug 藏起来。
"""

from __future__ import annotations

import functools
import inspect

# 记一笔「包过几次」，只给诊断用，不参与逻辑
REGISTRY: dict = {}


def _log(message: str) -> None:
    """日志走 print：Anki 会把它收进自己的日志，不弹窗、不打扰用户。"""
    print(f"[应试词汇覆盖统计] {message}")


def bind_ok(callback, args, kwargs=None) -> bool:
    """这组参数能不能塞进 callback 的签名里（只做签名检查，不执行）。"""
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        # 拿不到签名（内建函数、C 扩展）时不做判断，交给调用方
        return True
    try:
        signature.bind(*args, **(kwargs or {}))
    except TypeError:
        return False
    return True


def guard(callback):
    """包一层：参数对不上就跳过并记日志，对得上就正常调用并回传结果。"""
    name = getattr(callback, "__name__", repr(callback))

    @functools.wraps(callback)
    def wrapper(*args, **kwargs):
        if not bind_ok(callback, args, kwargs):
            _log(
                f"跳过钩子 {name}：Anki 这次传了 {len(args)} 个位置参数，"
                f"和回调签名对不上（多半是 Anki 版本改了参数），界面不受影响。"
            )
            return None
        return callback(*args, **kwargs)

    # 留个记号，探针/测试用它区分「包过」和「没包过」的回调
    wrapper.__guarded__ = True
    wrapper.__guarded_name__ = name
    REGISTRY[name] = REGISTRY.get(name, 0) + 1
    return wrapper
