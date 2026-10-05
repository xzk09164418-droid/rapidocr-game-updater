# -*- coding: utf-8 -*-
"""输入注入（参考 March7thAssistant 的 input 抽象：本地 SendInput 通道）。

点击策略：不再原地直接点击目标——先把鼠标归位到「休息位」，再沿缓动
轨迹移动到目标位置后才按下，模拟真人从「空闲位」移动到按钮上的过程。

休息位 = 屏幕左边缘中部（x≈2, y=屏幕高一半）：
  启动器是窗口化程序，常占屏幕中部，鼠标停在中心会遮挡界面文字、
  还会被截进截图干扰 OCR；屏幕边缘则基本不会被任何窗口盖住。
  截图前必须先把鼠标归位（window.screenshot 已内置 park()）。
"""
import time

# 轨迹参数：从休息位到目标的移动时长与采样步数
MOVE_DURATION = 0.5
MOVE_STEPS = 12


def _backend():
    """优先 pyautogui（支持 duration 缓动），否则退回 pywinauto.mouse。"""
    try:
        import pyautogui
        return 'pyautogui', pyautogui
    except ImportError:
        from pywinauto import mouse
        return 'pywinauto', mouse


def _screen_size():
    backend, mod = _backend()
    if backend == 'pyautogui':
        return mod.size()
    import win32api
    return win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)


def rest_position():
    """鼠标休息位：屏幕左边缘中部（不被窗口化启动器遮挡的位置）。"""
    w, h = _screen_size()
    return 2, h // 2


def park():
    """立即把鼠标归位到休息位（无轨迹、无点击）。

    截图 / 文字识别之前调用：指针悬在界面中央会挡住文字、
    甚至把光标截进图里污染 OCR。
    """
    backend, mod = _backend()
    x, y = rest_position()
    if backend == 'pyautogui':
        mod.moveTo(x, y)
    else:
        mod.move(coords=(x, y))


def screen_center():
    """当前主屏幕中心坐标。（历史兼容；休息位请用 rest_position）"""
    w, h = _screen_size()
    return w // 2, h // 2


def _ease(t: float) -> float:
    """smoothstep 缓动：起步减速收尾，轨迹更像人手。"""
    return t * t * (3 - 2 * t)


def waypoints(x: int, y: int, steps: int = MOVE_STEPS):
    """从休息位到目标的完整轨迹点（含起点休息位、终点目标）。

    移动与测试标注共用同一份轨迹，保证「画出来的」就是「走过」的。
    """
    rx, ry = rest_position()
    pts = [(rx, ry)]
    for i in range(1, steps + 1):
        t = _ease(i / steps)
        pts.append((round(rx + (x - rx) * t), round(ry + (y - ry) * t)))
    return pts


def move_from_center(x: int, y: int, duration: float = MOVE_DURATION,
                     points: list = None):
    """从休息位出发移动到目标位置（点击前的标准动作）。

    函数名沿用了早期版本的 move_from_center，起点自屏幕中心改为
    屏幕边缘休息位（2026-09 按遮挡问题调整），调用处无需改动。
    """
    backend, mod = _backend()
    pts = points if points is not None else waypoints(x, y)
    sleep = duration / max(1, len(pts) - 1)
    if backend == 'pyautogui':
        for px, py in pts:
            mod.moveTo(px, py)
            time.sleep(sleep)
        return
    # pywinauto 后端
    for px, py in pts:
        mod.move(coords=(px, py))
        time.sleep(sleep)


def move(x: int, y: int):
    """普通移动（非点击场景）：同样从休息位起跳。"""
    move_from_center(x, y)


def click(x: int, y: int):
    move_from_center(x, y)
    backend, mod = _backend()
    if backend == 'pyautogui':
        mod.click(x, y)
    else:
        mod.click(button='left', coords=(x, y))
    park()   # 点击后归位，别让指针挡着后续文字识别


def click_box_center(box):
    """box 为四点坐标 [[x,y]...]（OCR 结果）或 (x1,y1,x2,y2)。"""
    if isinstance(box, (list, tuple)) and len(box) == 4 and isinstance(box[0], (list, tuple)):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        x, y = sum(xs) / 4, sum(ys) / 4
    else:
        x, y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    click(int(x), int(y))
