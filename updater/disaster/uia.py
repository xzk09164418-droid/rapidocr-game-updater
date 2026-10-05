# -*- coding: utf-8 -*-
"""UIA 控件树通道（优先使用）。
库洛启动器是 WPF（原生 UIA，包内自带 UIAutomationProvider.dll），控件树可读；
Qt/CEF/WebView2 自绘界面拿不到控件时返回 None，由 OCR/模板层接管。"""
import logging

log = logging.getLogger("uia")


def find_by_text(window_keywords: list, texts: list, control_types=("Button", "Text"),
                 window_handle=None, exact=False, enabled_only=False):
    """在窗口控件树中按文字找控件，返回屏幕坐标 (x1,y1,x2,y2)；找不到返回 None。"""
    try:
        import uiautomation as uia
    except ImportError:
        log.info("未安装 uiautomation（pip install uiautomation），跳过 UIA 通道")
        return None
    windows = ([uia.ControlFromHandle(window_handle)] if window_handle is not None
               else uia.GetRootControl().GetChildren())
    for win in windows:
        title = getattr(win, "Name", "") or ""
        if not any(k in title for k in window_keywords):
            continue
        for ctrl in win.GetChildren():
            hit = _walk(ctrl, texts, control_types, depth=0,
                        exact=exact, enabled_only=enabled_only)
            if hit:
                return hit
    return None


def _walk(ctrl, texts, control_types, depth, exact=False, enabled_only=False):
    if depth > 12:
        return None
    try:
        name = getattr(ctrl, "Name", "") or ""
        ctype = getattr(ctrl, "ControlTypeName", "")
        text_matches = ("".join(name.split()) in texts if exact
                        else any(t in name for t in texts))
        enabled = not enabled_only or getattr(ctrl, "IsEnabled", False)
        if text_matches and enabled and any(c in ctype for c in control_types):
            r = ctrl.BoundingRectangle
            if r.width() > 0 and r.height() > 0:
                return (r.left, r.top, r.right, r.bottom)
        for child in ctrl.GetChildren():
            hit = _walk(child, texts, control_types, depth + 1, exact, enabled_only)
            if hit:
                return hit
    except Exception:
        return None
    return None
