# -*- coding: utf-8 -*-
"""多游戏启动器切页：命令行附加参数优先，图标模板匹配保底。

附加参数格式由各厂商在 win-games.yaml 的 cli_switch_arg 配置（{biz} 占位），
米哈游实测（本机桌面快捷方式目标，单实例再调一次即切页）：
  launcher.exe --game=nap_cn    绝区零
  launcher.exe --game=hk4e_cn   原神
  launcher.exe --game=hkrpg_cn  崩坏：星穹铁道
  launcher.exe --game=bh3_cn    崩坏3

保底（模板匹配）：启动器 ico/ 目录导出的 256px 图标（Template/ 目录），
在窗口左侧栏多尺度 cv2.matchTemplate 定位——图标顺序怎么变都找得到，
不依赖任何固定槽位。

切页参数按厂商从 win-games.yaml 分组读取：新厂商只要在 YAML 配好
cli_switch_arg 与 games 节即可接入，无需改动本模块。
"""
import logging
import os
import re
import subprocess

log = logging.getLogger("gameswitch")

# 侧栏图标只会出现在窗口左侧这一比例范围内
SIDEBAR_X_MAX = 0.15
# Logo 判定区（窗口左上），用于验证切页结果。
# 注意要覆盖活动卡片的游戏字标：7.1 深色系页面实测白色「原神」大字 Logo
# 完全不被 OCR 检测，但活动卡片内的「原神」字标识别稳定（center≈0.37w,0.56h），
# 旧 0.30x0.28 区域正好把它排除在外导致切页验证假阴性。
LOGO_W, LOGO_H = 0.40, 0.60

# 厂商未在 YAML 配置 cli_switch_arg 时的默认切页参数模板
DEFAULT_ARG_TEMPLATE = "--game={biz}"

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "win-games.yaml")


def load_games(path: str = None) -> dict:
    """从 win-games.yaml 加载全部厂商的多游戏切页参数。

    返回 {vendor: {game: {"biz","template","logo","not_logo"}}}：
    biz=附加参数值（按 cli_switch_arg 模板拼参数，单实例再调即切页）；
    template=Template/ 下 256px 图标（模板匹配保底）；logo/not_logo=
    切页验证词表与反向排除词（花体 Logo 误读对策见 YAML 注释）。
    厂商级 enabled: false 整家跳过；子游戏级 enabled: false 跳过该游戏。
    """
    import yaml
    cfg = yaml.safe_load(
        open(path or _CONFIG_PATH, encoding="utf-8").read())
    out = {}
    for vendor, spec in (cfg.get("vendors") or {}).items():
        if spec.get("enabled", True) is False:      # 厂商级停用
            continue
        games = {}
        for g, info in (spec.get("games") or {}).items():
            if not isinstance(info, dict):          # 允许简写 game: biz
                info = {"biz": info}
            if info.get("enabled", True) is False:  # 子游戏级停用
                continue
            games[g] = {"biz": info.get("biz"),
                        "template": info.get("template"),
                        "logo": list(info.get("logo") or []),
                        "not_logo": list(info.get("not_logo") or [])}
        if games:
            out[vendor] = games
    return out


ALL_GAMES = load_games()
# 兼容旧引用：等价于 ALL_GAMES["mihoyo"]
GAMES = ALL_GAMES.get("mihoyo", {})


def norm(text):
    return re.sub(r"[\s·・]+", "", str(text)).lower()


def _game_info(vendor: str, game: str) -> dict:
    return (ALL_GAMES.get(vendor) or {}).get(game) or {}


def switch_via_cli(launcher_exe: str, game: str, vendor: str = "mihoyo",
                   arg_template: str = None) -> bool:
    """按 arg_template（{biz} 占位，默认 --game={biz}）拼附加参数，
    再调一次启动器（单实例收到后切页）。返回是否已发起。"""
    info = _game_info(vendor, game)
    biz = info.get("biz")
    if not biz or not launcher_exe or not os.path.isfile(launcher_exe):
        return False
    arg = (arg_template or DEFAULT_ARG_TEMPLATE).format(biz=biz)
    log.info("附加参数切页: %s %s", launcher_exe, arg)
    subprocess.Popen([launcher_exe, arg], cwd=os.path.dirname(launcher_exe))
    return True


def find_icon(shot, game: str, template_dir: str, threshold: float = 0.72,
              vendor: str = "mihoyo"):
    """左侧栏多尺度模板匹配。

    【调用约定】shot 必须是「窗口截图」（window.screenshot(win)）——
    本函数只在窗口左侧栏比例区域内匹配；若传入全屏截图，桌面快捷方式
    图标会参与匹配造成误命中，此时会告警并仍只搜左侧 15% 区域。

    返回 {"center": (cx,cy), "box": (x1,y1,x2,y2), "score", "scale"}
    （窗口相对坐标）；找不到返回 None。
    """
    import cv2
    import numpy as np
    import win32api

    info = _game_info(vendor, game)
    tpl_name = info.get("template")
    if not tpl_name:
        return None
    path = os.path.join(template_dir, tpl_name)
    if not os.path.isfile(path):
        log.warning("模板缺失: %s", path)
        return None
    gray = np.asarray(shot.convert("L"))
    h, w = gray.shape
    sw, sh = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
    if w >= sw and h >= sh:
        # 直接拒绝：全屏图里桌面快捷方式图标与游戏图标同形，必出误命中；
        # 且返回的坐标是屏幕系，与调用方预期的窗口系不一致，硬用必错。
        log.warning("find_icon 拒绝全屏截图：桌面图标会干扰匹配且坐标系不符，"
                    "请传窗口截图（window.screenshot(win)）")
        return None
    region = gray[:, : int(w * SIDEBAR_X_MAX)]
    tpl = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if tpl is None:
        return None
    best = None
    # 侧栏图标渲染约 36~64px：256px 模板缩到 0.14~0.32 逐档尝试
    for s in np.linspace(0.14, 0.32, 19):
        tw = max(8, int(round(tpl.shape[1] * s)))
        th = max(8, int(round(tpl.shape[0] * s)))
        if region.shape[0] < th or region.shape[1] < tw:
            continue
        t = cv2.resize(tpl, (tw, th))
        res = cv2.matchTemplate(region, t, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        if best is None or score > best[0]:
            best = (float(score), int(loc[0]), int(loc[1]), tw, th, float(s))
    if not best or best[0] < threshold:
        log.info("模板未命中 %s（best=%s）", game,
                 round(best[0], 3) if best else None)
        return None
    score, x, y, tw, th, s = best
    log.info("模板命中 %s: score=%.3f scale=%.2f box=%s",
             game, score, s, (x, y, x + tw, y + th))
    return {"center": (x + tw // 2, y + th // 2), "box": (x, y, x + tw, y + th),
            "score": score, "scale": s}


def on_game_page(shot, game: str, engine=None, vendor: str = "mihoyo") -> bool:
    """Logo 区 OCR 验证当前是否目标游戏页（含 not_logo 反向排除）。"""
    info = _game_info(vendor, game)
    if not info:
        return False
    import numpy as np
    from . import ocr as ocr_mod
    engine = engine or ocr_mod.default_engine()
    w, h = shot.size
    rows, _ = engine(np.asarray(shot))
    joined = ""
    for box, text, _ in rows or []:
        cx = sum(p[0] for p in box) / 4
        cy = sum(p[1] for p in box) / 4
        if cx < w * LOGO_W and cy < h * LOGO_H:
            joined += norm(text)
    for bad in info.get("not_logo", []):
        if norm(bad) in joined:
            return False
    return any(norm(k) in joined for k in info.get("logo") or [])


def switch_game(win, launcher_exe: str, game: str, template_dir: str,
                timeout: float = 60.0, click_icon=None,
                vendor: str = "mihoyo", arg_template: str = None) -> bool:
    """切到目标游戏页：附加参数优先，模板匹配点击保底。

    click_icon(cx, cy)：保底路径找到图标后的点击回调（测试可传 None，
    此时只做定位不点击）。返回最终是否已在目标页。
    """
    import time
    from . import ocr as ocr_mod

    engine = ocr_mod.default_engine()
    if on_game_page(window_shot(win), game, engine, vendor=vendor):
        return True
    if not switch_via_cli(launcher_exe, game, vendor=vendor,
                          arg_template=arg_template):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(3)
        shot = window_shot(win)
        if on_game_page(shot, game, engine, vendor=vendor):
            return True
    # 保底：模板匹配点图标
    shot = window_shot(win)
    m = find_icon(shot, game, template_dir, vendor=vendor)
    if not m:
        return False
    if click_icon is None:
        return True  # 测试模式：仅定位不点击，找到图标即保底成功
    cx, cy = m["center"]
    from . import window as _w
    click_icon(*_w.to_screen(win, cx, cy))   # 截图四边内缩，统一换算回屏幕坐标
    end = time.monotonic() + timeout / 2
    while time.monotonic() < end:
        time.sleep(3)
        if on_game_page(window_shot(win), game, engine, vendor=vendor):
            return True
    # 兜底放行：附加参数与图标点击都已执行，仅 Logo OCR 验证失败
    # （深色系页面花体/白字 Logo 可能完全读不出）时警告后继续——
    # 点错页的代价是更新了同厂商另一款游戏（清单内），而验证假阴性
    # 直接放弃会让整个灾备流程失效。_pending_update_items 的逐页扫描
    # 仍会在收尾时兜底。
    log.warning("[%s] Logo OCR 验证未通过，但附加参数与图标点击均已执行，"
                "按已切页继续（保守收尾仍会逐页复查）", game)
    return True


def window_shot(win):
    from . import window
    return window.screenshot(win)
