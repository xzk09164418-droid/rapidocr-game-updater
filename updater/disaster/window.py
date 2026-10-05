# -*- coding: utf-8 -*-
"""窗口查找与截图（Windows）。依赖懒加载，非 Windows 环境导入不报错。"""
import ctypes
import logging
import time
from pathlib import Path

log = logging.getLogger("window")

# 截图存档：每次截图自动等比压缩到 1920x1200 以内（只缩不放），JPEG 存进
# logs/screenshots/，供事后排查误识别；只保留最近 SHOT_KEEP 张，防止长时间
# 运行塞满磁盘。存档失败只告警，绝不影响主流程。
SHOT_DIR = Path(__file__).resolve().parents[2] / "logs" / "screenshots"
SHOT_MAX_SIZE = (1920, 1200)
SHOT_KEEP = 50

# 识别区域内缩像素：窗口矩形含 DWM 隐形边框，叠加启动动画偏差后，边缘会把
# 背后的其他窗口截进图里（实测 OCR 误命中背景编辑器里的「更新」两字并把
# 点击落到了窗外）。截图四边统一内缩 SHOT_INSET 像素；点击/悬停坐标必须
# 经 to_screen() 加回同一偏移。
SHOT_INSET = 20


def _archive_screenshot(img):
    """压缩并保存截图到 logs/screenshots/，淘汰最旧的超出保留量文件。"""
    try:
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        if img.width > SHOT_MAX_SIZE[0] or img.height > SHOT_MAX_SIZE[1]:
            img = img.copy()            # thumbnail 原地修改，不动调用方的图
            img.thumbnail(SHOT_MAX_SIZE)
        now = time.time()
        name = (time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
                + f"-{int(now * 1000) % 1000:03d}.jpg")
        img.convert("RGB").save(SHOT_DIR / name, "JPEG", quality=80)
        shots = sorted(SHOT_DIR.glob("*.jpg"))
        for old in shots[:-SHOT_KEEP]:
            old.unlink()
    except Exception as exc:
        log.warning("截图存档失败: %s", exc)

_DPI_READY = False


def _check_windows():
    import sys
    if sys.platform != "win32":
        raise RuntimeError("GUI 灾备层仅支持 Windows（需真实桌面会话）")


def _ensure_dpi_awareness():
    """截图坐标与窗口坐标统一为物理像素。

    高 DPI 缩放（如 200%）下，进程默认 DPI 不感知：pygetwindow 返回
    逻辑坐标，而 ImageGrab / SendInput 使用物理像素，两者相差一个
    缩放系数，会导致截图只裁到窗口一角、点击坐标偏移。
    声明 PER_MONITOR 感知后，窗口几何、截图、鼠标输入全部对齐。
    """
    global _DPI_READY
    if _DPI_READY:
        return
    _DPI_READY = True
    try:
        # 2 = PROCESS_PER_MONITOR_DPI_AWARE；HRESULT，忽略返回值
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def find_window(keywords: list, timeout: int = 60):
    """按标题关键词找窗口，返回 pygetwindow 窗口对象。"""
    _check_windows()
    _ensure_dpi_awareness()
    import pygetwindow as gw
    deadline = time.time() + timeout
    while time.time() < deadline:
        for w in gw.getAllWindows():
            if w.visible and any(k in w.title for k in keywords):
                return w
        time.sleep(1)
    raise RuntimeError(f"未找到窗口: {keywords}")


def _force_foreground(hwnd):
    """绕过 SetForegroundWindow 的进程前台限制。

    Windows 只允许前台进程把别的窗口置前；后台进程直接调用返回 0
    （pygetwindow 会报 Error code 0 这种自相矛盾的异常）。标准做法是
    AttachThreadInput 把当前线程挂到前台窗口的输入队列上，调用即生效。
    """
    import win32api
    import win32gui
    import win32process
    foreground = win32gui.GetForegroundWindow()
    fg_thread = win32process.GetWindowThreadProcessId(foreground)[0]
    cur_thread = win32api.GetCurrentThreadId()
    attached = fg_thread != cur_thread
    if attached:
        win32process.AttachThreadInput(cur_thread, fg_thread, True)
    try:
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
    finally:
        if attached:
            win32process.AttachThreadInput(cur_thread, fg_thread, False)


def _activate_window(win, timeout: float = 30.0):
    """激活窗口，失败反复重试直至 timeout 秒超时。

    窗口被最小化/遮挡/目标进程忙时 activate() 可能瞬时失败；
    这里是唯一能拿到正确截图与点击坐标的前提，所以必须重试而不是退回。
    判定成功只看实际结果（GetForegroundWindow == 目标），
    pygetwindow 的 error-0 伪异常不能作为失败依据。
    """
    import win32gui
    hwnd = win._hWnd
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            if win.isMinimized:
                win.restore()
                time.sleep(0.3)
            try:
                win.activate()
            except Exception as exc:
                last = exc
                _force_foreground(hwnd)  # 后台进程直调被拒时换挂输入队列
            time.sleep(0.3)
            if win32gui.GetForegroundWindow() == hwnd:
                return
        except Exception as exc:
            last = exc
            time.sleep(0.5)
    raise RuntimeError(f"窗口 {timeout:.0f}s 内无法激活: {getattr(win, 'title', win)!r} ({last})")


def _ensure_on_screen(win, rect):
    """窗口大部分在屏外时拉回屏幕中央可见区域。

    启动器会记住上次关闭时的窗口位置：用户把窗口拖到屏外/ tray 化后，
    下次启动恢复在屏外，截图只裁到一条、点击坐标全部落空。
    可见面积不足 60% 即视为屏外，SetWindowPos 移回（不改大小/层级）。
    """
    import win32api
    import win32gui
    left, top, right, bottom = rect
    sw = win32api.GetSystemMetrics(0)
    sh = win32api.GetSystemMetrics(1)
    vis_w = max(0, min(right, sw) - max(left, 0))
    vis_h = max(0, min(bottom, sh) - max(top, 0))
    if vis_w >= (right - left) * 0.6 and vis_h >= (bottom - top) * 0.6:
        return rect
    nw, nh = right - left, bottom - top
    nl = 0 if nw >= sw else max(0, (sw - nw) // 2)
    nt = 0 if nh >= sh else max(0, (sh - nh) // 2)
    try:
        # 0x0001=SWP_NOSIZE 0x0004=SWP_NOZORDER
        win32gui.SetWindowPos(win._hWnd, 0, nl, nt, 0, 0, 0x0001 | 0x0004)
        time.sleep(0.3)
        log.info("窗口屏外(l=%s t=%s)，已移回 (%s,%s)", left, top, nl, nt)
        return nl, nt, nl + nw, nt + nh
    except Exception:
        return rect


def _inset_rect(rect):
    """窗口矩形四边内缩 SHOT_INSET 像素（窗口过小时不缩，防反转）。"""
    left, top, right, bottom = rect
    if right - left > SHOT_INSET * 2 and bottom - top > SHOT_INSET * 2:
        return (left + SHOT_INSET, top + SHOT_INSET,
                right - SHOT_INSET, bottom - SHOT_INSET)
    return rect


def to_screen(win, x, y):
    """截图坐标 → 屏幕坐标。截图区域四边内缩了 SHOT_INSET，这里统一加回
    偏移；所有基于截图位置的点击/悬停都必须走这个换算。"""
    return win.left + SHOT_INSET + int(x), win.top + SHOT_INSET + int(y)


def _window_rect(win):
    """取窗口屏幕区域（物理像素），四边内缩 SHOT_INSET。失败就抛异常，
    绝不静默退回全屏——全屏截图会把任务栏电量等无关百分比当成下载进度，
    点击坐标也会落到桌面上完全无关的位置。"""
    try:
        _activate_window(win)
        if win.width > 0 and win.height > 0:
            rect = (win.left, win.top, win.left + win.width, win.top + win.height)
            return _inset_rect(_ensure_on_screen(win, rect))
    except RuntimeError:
        raise
    except Exception:
        pass
    try:
        import win32gui
        left, top, right, bottom = win32gui.GetWindowRect(win._hWnd)
        if right > left and bottom > top:
            return _inset_rect(_ensure_on_screen(win, (left, top, right, bottom)))
    except Exception:
        pass
    raise RuntimeError(f"窗口区域不可获取，拒绝全屏截图: {getattr(win, 'title', win)!r}")


def screenshot(win=None, region=None, park=True):
    """窗口截图（PIL ImageGrab）；win 给出时先激活并裁剪窗口区域。

    截图前先把鼠标归位到屏幕边缘休息位：指针悬在界面中央会遮挡文字、
    还可能把光标截进图里污染 OCR。
    park=False 时不动鼠标——用于悬停后抓拍 tooltip/悬停态进度等内容。
    """
    _check_windows()
    _ensure_dpi_awareness()
    if park:
        try:
            from . import input as _inp
            _inp.park()
            time.sleep(0.15)
        except Exception:
            pass
    from PIL import ImageGrab
    if win is not None and region is None:
        region = _window_rect(win)
    img = ImageGrab.grab(bbox=region)
    _archive_screenshot(img)
    return img


def process_running(names: list) -> bool:
    """更新器进程是否存活——状态机主干信号，比 OCR 进度条可靠。

    按映像名【精确】匹配：子串匹配会把 NTELauncher.exe 误判成
    launcher.exe（米哈游/鸣潮启动器进程名都是 launcher.exe），
    异环启动器开着时米哈游流程会误以为启动器已运行。
    """
    import csv
    import io
    import subprocess
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True).stdout
    wanted = {n.lower() for n in names}
    for row in csv.reader(io.StringIO(out)):
        if row and row[0].strip().strip('"').lower() in wanted:
            return True
    return False


def find_updater_windows(names, keywords=()):
    """非阻塞枚举更新器及其子进程的可见窗口；可额外配置专用标题。"""
    import psutil
    import pygetwindow as gw
    import win32gui
    import win32process
    wanted = {name.lower() for name in names}
    pids = set()
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info["name"] or "").lower() in wanted:
                pids.add(proc.pid)
                pids.update(child.pid for child in proc.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    result = []
    for win in gw.getAllWindows():
        try:
            if not win32gui.IsWindowVisible(win._hWnd) or win.width <= 0 or win.height <= 0:
                continue
            pid = win32process.GetWindowThreadProcessId(win._hWnd)[1]
            if pid in pids or any(word in win.title for word in keywords):
                result.append(win)
        except Exception:
            continue
    return result
