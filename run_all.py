#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键入口：OCR 图像识别更新 Windows 游戏 + MuMu 手游官网 APK 更新。

流程：
  1. Win 游戏（图像识别路径，真实点击更新按钮）：
       米哈游启动器：绝区零 → 原神 → 崩铁 → 崩坏3（附加参数切页，
       失败时 Template 图标模板匹配保底；命中「开始游戏」且无其余
       子任务更新项才结束启动器进程）
       鸣潮 / 鹰角：单游戏启动器直更
       异环：观察模式——启动器更新阶段只点确认类弹窗，游戏更新由
       启动器自动完成；「开始游戏」稳定出现且无预下载项才收尾
  2. MuMu 手游（URL/API 路径，无界面）：
       官网 API 探测新版本 → 下载 APK（断点续传+校验）→
       MuMu 实例 adb 安装 → 验证版本号
       → 收尾：脚本自启的实例 shutdown（用户自开的实例不动）

用法：
  python run_all.py             完整跑一遍
  python run_all.py --dry       只检查不下载/不点击
  python run_all.py --apk-only  只跑 MuMu APK 部分
  python run_all.py --win-only  只跑 Win 游戏部分
"""
import argparse
import logging
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))              # updater 包（OCR 图像识别路径）
sys.path.insert(0, str(BASE / "apkupd"))   # MuMu APK 模块（平铺相互导入）

import yaml  # noqa: E402

from updater.disaster import fsm  # noqa: E402

def load_win_plan(path: str = None) -> list:
    """执行计划从 updater/win-games.yaml 的 plan 节读取（path 供测试注入）。

    增删游戏只改 YAML：删除 plan 条目或 enabled: false 即不再更新，
    无需改动任何代码。plan 条目写法见 win-games.yaml 头部注释。
    """
    cfg_path = Path(path) if path else BASE / "updater" / "win-games.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    plan = []
    for item in cfg.get("plan") or []:
        if item.get("enabled", True) is False:
            continue
        plan.append((item["vendor"], item.get("game")))
    return plan


def run_win_games(dry: bool, only: set = None) -> tuple:
    """返回 (是否全部成功, 失败描述列表)；失败描述供末尾 Server 酱汇总推送。

    only：只跑指定条目（{"vendor"} 或 {"vendor/game"} 字符串集合），
    供分段实测；为 None 跑全计划。过滤在 last_index 计算之前，
    收尾扫描语义与全量运行一致。
    """
    ok_all = True
    failures = []
    plan = load_win_plan()
    if only:
        plan = [(v, g) for v, g in plan
                if v in only or (f"{v}/{g}" if g else v) in only]
        if not plan:
            print(f"[plan] --only 未匹配任何条目: {sorted(only)}")
            return False, [f"--only 未匹配任何条目: {sorted(only)}"]
    # 每个厂商在计划里最后一次出现时（last_index）才做收尾逐页扫描：
    # 中间轮跳过扫描也跳过关启动器（下一轮开头会重启），最后一轮
    # 也只扫本会话没更新成功的游戏页——4 个子项不再识别 16 次。
    last_index = {v: i for i, (v, _) in enumerate(plan)}
    completed = {}          # vendor -> 本会话已成功更新的 game 集合
    for i, (vendor, game) in enumerate(plan):
        label = f"{vendor}/{game}" if game else vendor
        p = fsm.PROFILES.get(vendor)
        if p is None:
            print(f"[{label}] 跳过：win-games.yaml 中无此厂商或已停用")
            continue
        if game and game not in (p.games or {}):
            print(f"[{label}] 跳过：win-games.yaml 中无此游戏切页参数或已停用")
            continue
        try:
            if dry:
                final = (i == last_index[vendor])
                args_txt = " " + " ".join(p.launch_args) if p.launch_args else ""
                print(f"[{label}] dry-run：将启动 {p.launcher_exe}{args_txt}"
                      f"（{'观察+点确认弹窗' if not p.click_updates else '识别并点击更新'}"
                      f"{'' if final else '，跳过收尾扫描'}）")
                continue
            up = fsm.DisasterUpdater(
                p, game=game, template_dir=str(BASE / "Template"),
                final_for_vendor=(i == last_index[vendor]),
                skip_pending_scan=completed.get(vendor, ()))
            ok = up.run()
            if ok and game:
                completed.setdefault(vendor, set()).add(game)
            print(f"[{label}] {'更新完成' if ok else '未完成（见日志）'}")
            ok_all = ok_all and bool(ok)
            if not ok:
                failures.append(f"{label}：未完成（超时或识别失败，详见 logs/run.log）")
        except Exception as exc:  # 一家失败不阻塞后面
            if exc.__class__.__name__ == "FailSafeException":
                # pyautogui 防呆：人工把鼠标甩到屏幕左上角触发的紧急停止。
                # 尊重中止意图，整场运行不再继续（含后续厂商与 APK 阶段）。
                print("!! 检测到紧急停止（鼠标位于屏幕左上角角落，"
                      "pyautogui fail-safe）：已中止全部剩余任务；"
                      "移动鼠标后重新运行即可。")
                logging.warning("[%s] pyautogui fail-safe 触发，中止全部剩余任务", label)
                return False
            logging.exception("[%s] 异常", label)
            print(f"[{label}] 失败：{exc}")
            ok_all = False
            failures.append(f"{label}：异常 {exc}")
    return ok_all, failures


def run_apk(dry: bool) -> tuple:
    """返回 (是否全部成功, 失败描述列表)。attention 为运行级异常，同样计入失败。"""
    from android_update import run_android

    cfg_path = BASE / "apkupd" / "game-update.yaml"
    config = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    settings = config["mumu"]
    results = []

    def report(item, status, detail):
        results.append((item, status, detail))
        print(f"[APK][{status}] {item}：{detail}")

    try:
        run_android(settings, report, dry, BASE, config)
    except Exception as exc:
        logging.exception("[APK] 异常")
        print(f"[APK] 失败：{exc}")
        return False, [f"APK 阶段：异常 {exc}"]
    failures = [f"手游 {item}：{detail}" for item, status, detail in results
                if status in {"failed", "attention"}]
    return not failures, failures


def main():
    ap = argparse.ArgumentParser(description="OCR Win 游戏 + MuMu APK 一键更新")
    ap.add_argument("--dry", action="store_true", help="只检查不执行")
    ap.add_argument("--apk-only", action="store_true")
    ap.add_argument("--win-only", action="store_true")
    ap.add_argument("--only", default="",
                    help="只跑指定 Win 条目，逗号分隔（vendor 或 vendor/game），"
                         "如 --only mihoyo/zzz,kuro；供分段实测")
    args = ap.parse_args()
    only = {s.strip() for s in args.only.split(",") if s.strip()} or None

    (BASE / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(BASE / "logs" / "run.log", encoding="utf-8")],
    )
    ok = True
    failures = []
    if not args.apk_only:
        print("===== 第一阶段：Win 游戏（图像识别）=====")
        ok_win, f = run_win_games(args.dry, only=only)
        failures += f
        ok = ok_win and ok
    if not args.win_only:
        print("===== 第二阶段：MuMu 手游（URL/API APK）=====")
        ok_apk, f = run_apk(args.dry)
        failures += f
        ok = ok_apk and ok
    print("=====", "全部完成" if ok else "部分失败（见 logs/run.log）", "=====")
    if not args.dry:
        # 仅失败推送：failures 为空时只补发上轮落盘消息，不产生新通知
        try:
            import notify
            notify.notify_failures(failures)
        except Exception:
            logging.exception("Server酱推送异常")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
