# -*- coding: utf-8 -*-
"""GUI 灾备状态机：其他更新路径全部失效时，像人一样操作官方启动器。

识别优先级（用户要求）：UIA 控件树 → OCR（整图 → 滑窗多层）→ 图像模板。
状态判断：启动器弹窗优先；更新器连续消失五分钟后，再检查游戏完成状态。

四家画像基于启动器解包事实：
  库洛 WPF（UIA 优先）       updater 进程 launcher_updater.exe
  米哈游 Qt+CEF（OCR 为主）  updater 进程 HYUpdater.exe
  鹰角 Qt+QtWebEngine        updater 进程 Updater.exe
  异环 完美系内嵌浏览器       updater 进程 NTEUpdate.exe
"""
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field

from . import input as inp
from . import ocr as ocr_mod
from . import uia, window

log = logging.getLogger("disaster")


def _box_center(box):
    """OCR 四点框或 (x1,y1,x2,y2) 模板框 -> 中心点（窗口相对坐标）。"""
    if isinstance(box, (list, tuple)) and len(box) == 4 and isinstance(box[0], (list, tuple)):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        return sum(xs) / 4, sum(ys) / 4
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


@dataclass
class GUIProfile:
    vendor: str
    launcher_exe: str            # 启动器完整路径
    window_keywords: list        # 窗口标题关键词
    updater_processes: list      # 更新器进程名（状态主干）
    extra_processes: list = field(default_factory=list)
    # 启动器 UI/伴生进程（如米哈游 HYP.exe：launcher.exe 只是转发壳，
    # 启动后即退出，真正窗口宿主是 HYP.exe）。仅在清理/收尾时一并结束，
    # 不作为「更新器运行中」的保持信号，否则伴生长驻进程会把收尾卡死。
    update_texts: list = field(default_factory=lambda: ["立即更新", "更新游戏", "更新"])
    done_texts: list = field(default_factory=lambda: ["开始游戏", "进入游戏", "启动游戏"])
    retry_texts: list = field(default_factory=lambda: ["重试", "确定", "继续"])
    # 二次确认弹窗按钮（实测米哈游预下载：点「预下载」后弹「确认下载」对话框，
    # 不点确认下载永远不会开始；为空表示该厂商无此交互）
    confirm_texts: list = field(default_factory=list)
    # 资源校验阶段文本：进度到 100% 只代表下载段结束，之后可能（也可能不）
    # 进入校验；校验文本在期间一律视为未完成。
    verifying_texts: list = field(default_factory=lambda: ["校验", "验证中", "资源验证"])
    # 状态文案排除：含「完成」的文本（如「预下载已完成」标签）不是按钮，禁止点击
    update_exclude: tuple = ("完成",)
    # 下载被暂停后的恢复按钮（用户手动暂停/异常中断后界面显示「已暂停」）。
    # 阶段1/阶段2 都会识别并点击恢复；resume_click_cap 限制恢复次数，
    # 超过后认为用户有意暂停，尊重用户不再自动恢复。
    resume_texts: list = field(default_factory=list)
    resume_click_cap: int = 3
    # 「预下载」入口在场时，「开始游戏」等完成按钮不算完成标志——完成按钮
    # 是次优先信号，当且仅当预下载不存在时才代表更新完成（实测多家启动器
    # 完成按钮与预下载入口同屏常驻）。
    predownload_texts: list = field(default_factory=lambda: ["预下载"])
    # 更新/下载进行中文案：在场一律视为未完成（观察模式的主要保持信号）。
    updating_texts: list = field(default_factory=lambda: [
        "更新中", "下载中", "正在更新", "正在下载", "解压中", "安装中",
        "下载资源", "更新资源"])
    # 二次确认弹窗点击上限：启动器更新可能多次弹确认/完成/重启提示
    confirm_click_cap: int = 6
    # 启动器弹窗优先于背景游戏按钮；继续使用先拼接、边界拼接、锚点恢复。
    launcher_context_texts: list = field(default_factory=lambda: [
        "启动器更新", "更新启动器", "发现新版本", "检测到新版本", "新版本可用"])
    launcher_action_texts: list = field(default_factory=lambda: [
        "立即更新", "更新启动器", "确认更新", "开始更新", "重启并更新",
        "重启启动器", "立即重启", "更新", "确定", "确认", "继续", "重试", "完成"])
    launcher_busy_texts: list = field(default_factory=lambda: [
        "正在更新启动器", "启动器更新中", "正在下载", "下载中", "正在安装",
        "安装中", "更新中", "正在解压", "解压中"])
    updater_window_keywords: list = field(default_factory=list)
    launcher_restart_cap: int = 3
    launcher_quiet_seconds: float = 300.0
    launcher_click_cap: int = 20
    launcher_click_interval: float = 5.0
    # ---- 只观察模式（click_updates=false，异环）----
    # 异环两阶段：1.启动器更新（多次弹确认类提示，点击后 NTEUpdate.exe
    # 运行，完成后可能还要点完成/重启并重启启动器）；2.游戏更新（启动器
    # 最新后自动进行，完成后出现「开始游戏」）——只有游戏更新完成才算
    # 任务完成。注意：每次启动 NTEUpdate.exe 都会做更新检查（一瞬到几
    # 分钟），进程存活 ≠ 更新中，完成判定以界面为准、进程仅作参考。
    # observer_grace：启动后首轮更新检查的窗口期，期内不做完成判定；
    # done_stable_rounds：完成按钮需连续稳定命中的轮数；
    # observer_confirm_cap：观察模式确认弹窗点击上限。
    observer_grace: float = 120.0
    done_stable_rounds: int = 3
    observer_confirm_cap: int = 20
    prefer_uia: bool = False
    templates: dict = field(default_factory=dict)   # {"update_btn": "path.png"} 可选
    games: dict = field(default_factory=dict)       # {game_key: biz} 多游戏切页
    # 多游戏启动器切页附加参数模板：{biz} 会被替换为 games 节该游戏的 biz
    # （米哈游实测 --game=hk4e_cn，单实例再调一次即切页）。新厂商参数格式
    # 不同（如 -g={biz}）在 YAML 里覆盖即可；单游戏启动器用不到此字段。
    cli_switch_arg: str = "--game={biz}"
    click_updates: bool = True   # False=不点任何更新按钮（异环：启动器自更新）
    # 启动器启动附加参数（YAML 按列表写，每个元素独立一个 argv）。
    # 终末地实测：桌面快捷方式为 Launcher.exe --game=endfield --reason=4，
    # 无参数重启会停在明日方舟页。严禁把带空格的整串写成一个元素——
    # Windows 下会被引号包住，对方程序按单个参数解析导致参数失效。
    launch_args: list = field(default_factory=list)
    # OCR 找按钮时丢弃超长文本的阈值：去空白后长度 > max_text_len 的识别
    # 结果直接丢弃（按钮文案都是短词，公告长句第一轮即排除）。默认 6；
    # 某厂商更新按钮文案超过 6 字时在 YAML 该厂商节覆盖此值。
    max_text_len: int = 6
    # OCR 拆框合并阈值（同一行字被检测模型拆成多框时拼回一行再匹配词表）：
    # 字高差 ≤ merge_band_height_ratio×min(字高) 且中心点纵向差 ≤
    # merge_band_center_ratio×min(字高) → 判定同一水平带；带内水平间隙 ≤
    # merge_gap_ratio×两段平均字高 → 拼接。YAML defaults 节统一调，厂商节可覆盖。
    merge_band_height_ratio: float = 0.06
    merge_band_center_ratio: float = 0.05
    merge_gap_ratio: float = 1.2
    # 整词复查邻域（drop_fragment_exact）：整词候选（如「更新」）同带水平
    # exact_neighbor_ratio×字高内还有其他文字时判定为碎片丢弃。合并管
    # 「切得整齐的拆框」，这道管「切得参差的碎片」，与 _EXACT_MIN_SCORE
    # 置信度门槛互补。YAML defaults 节统一调，厂商节可覆盖。
    exact_neighbor_ratio: float = 3.0
    # ---- 按钮识别增强（2026-09-24 起，主匹配逻辑之后的追加层）----
    # boundary_group：带内边界拼接层。主路径（先拼后配）未命中时，把同带
    #   离散短碎片（单/双/三字）按按钮边界归组拼接再过词典——字距大到
    #   几何合并失效时，按钮边界是物理分组依据，边界外同行文字天然排除。
    # anchor_recover：部分命中碎片（关键词真子串）为锚 → 边缘定按钮边界
    #   → 边界内放大重识别，治整图尺度漏检；书法错字/淡色缺字治不了。
    # YAML defaults 节统一调，厂商节可覆盖。
    boundary_group: bool = True
    anchor_recover: bool = True


# 画像配置集中在 updater/win-games.yaml（含逐字段中文注释）；
# 词表含义与匹配规则见该文件头部说明。
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "win-games.yaml")


def load_profiles(path: str = None) -> dict:
    """从 YAML 加载四家启动器画像。

    defaults 节为四家共用词表，厂商节逐字段覆盖；games 节在 YAML 里是
    富结构（biz/template/logo/not_logo），这里只取 biz 组成
    {game_key: biz}（切页验证词表由 gameswitch.load_games 读取同一文件）。
    """
    import yaml
    cfg = yaml.safe_load(
        open(path or _CONFIG_PATH, encoding="utf-8").read())
    defaults = cfg.get("defaults") or {}
    profiles = {}
    for vendor, spec in (cfg.get("vendors") or {}).items():
        if spec.get("enabled", True) is False:      # 厂商级停用
            log.info("厂商 %s 已在 YAML 中停用", vendor)
            continue
        merged = {**defaults, **{k: v for k, v in spec.items() if k != "games"}}
        merged.pop("enabled", None)
        games = spec.get("games") or {}
        merged["games"] = {g: (info.get("biz") if isinstance(info, dict) else info)
                           for g, info in games.items()
                           if not (isinstance(info, dict)
                                   and info.get("enabled", True) is False)}
        if "update_exclude" in merged:
            merged["update_exclude"] = tuple(merged["update_exclude"])
        profiles[vendor] = GUIProfile(vendor=vendor, **merged)
    return profiles


PROFILES = load_profiles()


class DisasterUpdater:
    def __init__(self, profile: GUIProfile, game: str = None,
                 poll_interval: float = 3.0, template_dir: str = "",
                 restart_delay: float = 8.0,
                 final_for_vendor: bool = True, skip_pending_scan=()):
        self.p = profile
        self.game = game
        self.poll = poll_interval
        self.template_dir = template_dir
        self.restart_delay = restart_delay  # 杀进程后等多久再启动（等端口/句柄释放）
        # final_for_vendor=False：本厂商在计划里还有后续游戏要跑，收尾逐页
        # 扫描与关启动器都跳过——下一轮 run 开头会杀进程重启启动器，
        # 扫出来的结果立刻作废，纯属 N² 浪费（4 子项扫 16 次）。
        # skip_pending_scan：本会话已成功更新的同厂商游戏，最后一轮收尾
        # 扫描时不再复查它们的页面（刚验证过，再扫一遍无意义）。
        self.final_for_vendor = final_for_vendor
        self.skip_pending_scan = set(skip_pending_scan)
        self._launcher_clicks = 0
        self._launcher_last_click = float("-inf")
        self._launcher_pending = False
        self._launcher_absent_since = None

    def _launcher_process_settled(self):
        """任何配置的更新器再次出现，都重新计算连续消失的五分钟。"""
        now = time.monotonic()
        if window.process_running(self.p.updater_processes):
            self._launcher_absent_since = None
            self._launcher_pending = True
            return False
        if self._launcher_absent_since is None:
            self._launcher_absent_since = now
            log.info("更新器进程未运行，开始 %.0f 秒稳定观察", self.p.launcher_quiet_seconds)
        return now - self._launcher_absent_since >= self.p.launcher_quiet_seconds

    def _click_launcher_action(self, win, hit, left, top):
        self._launcher_pending = True
        if time.monotonic() - self._launcher_last_click < self.p.launcher_click_interval:
            return
        if self._launcher_clicks >= self.p.launcher_click_cap:
            raise RuntimeError("启动器更新弹窗点击达到上限，界面仍未完成")
        x, y = _box_center(hit[0])
        inp.click(*window.to_screen(win, x + left, y + top))
        self._launcher_clicks += 1
        self._launcher_last_click = time.monotonic()
        # 确定/完成可能触发下一阶段或重启，不能沿用之前的静默时间。
        self._launcher_absent_since = None
        log.info("启动器中间操作 %s/%s: %s", self._launcher_clicks,
                 self.p.launcher_click_cap, hit[1])

    def _handle_launcher_update(self, win, shot):
        """检查中央弹窗特征；返回 True 时禁止检查背景游戏完成按钮。

        标题与操作分开识别。操作仅搜索标题下方的中央区域；通用
        “更新”必须同时有“取消”才构成弹窗特征。“取消”从不点击。
        所有识别复用 _find_text 的散字/按钮边界恢复流水线。
        """
        # 确定无需上下文；完成仅作为中间操作，绝不据此宣告成功。
        direct = self._find_text(shot, ["确定", "完成"], sliding=False)
        if direct and "".join(direct[1].split()) in {"确定", "完成"}:
            self._click_launcher_action(win, direct, 0, 0)
            return True
        w, h = shot.size
        left, top = int(w * .18), int(h * .12)
        panel = shot.crop((left, top, int(w * .82), int(h * .90)))
        context = self._find_text(panel, self.p.launcher_context_texts,
                                  sliding=False, max_text_len=32)
        cancel = self._find_text(panel, ["取消", "稍后", "暂不更新"], sliding=False)
        update = self._find_text(panel, ["更新", "立即更新", "确认更新"], sliding=False)
        busy = self._find_text(panel, self.p.launcher_busy_texts,
                               sliding=False, max_text_len=32)
        if not (context or cancel or busy):
            return False
        # 未识别用途的取消弹窗也阻止提前成功，但不擅自点击确定。
        if not (context or (cancel and update)):
            return True
        self._launcher_pending = True
        if busy:
            log.info("启动器更新状态: %s", busy[1])
            return True
        action_top = 0
        if context:
            box = context[0]
            action_top = int(max(pt[1] for pt in box)) if isinstance(box[0], (list, tuple)) else int(box[3])
        actions = panel.crop((0, action_top, panel.width, panel.height))
        hit = self._find_text(actions, self.p.launcher_action_texts, sliding=False)
        if not hit and cancel and context and context[1].replace(" ", "") == "更新启动器":
            # 独立确认窗可能仅有“更新启动器 / 取消”，没有另一个标题。
            hit = context
            action_top = 0
        if hit:
            self._click_launcher_action(win, hit, left, top + action_top)
        return True

    def _wait_launcher_ready(self, win, deadline):
        """轮询更新器和启动器；五分钟静默后恢复未自动重启的启动器。"""
        rounds = 0
        restarts = 0
        while time.time() < deadline:
            settled = self._launcher_process_settled()
            updater_windows = window.find_updater_windows(
                self.p.updater_processes, self.p.updater_window_keywords)
            if updater_windows:
                rounds = 0
                self._launcher_pending = True
                for updater_win in updater_windows:
                    try:
                        shot = window.screenshot(updater_win)
                    except RuntimeError:
                        continue  # 退出/重启期间的失效句柄，下轮重新枚举
                    # 独立更新器本身就是上下文；全窗口搜索按钮，保留拼接流水线。
                    hit = self._find_text(shot, self.p.launcher_action_texts, sliding=False)
                    if hit and "".join(hit[1].split()) in self.p.launcher_action_texts:
                        self._click_launcher_action(updater_win, hit, 0, 0)
                time.sleep(self.poll)
                continue
            # 每轮重新获取启动器句柄，避免旧句柄和自动重启后的窗口混淆。
            try:
                win = window.find_window(self.p.window_keywords, timeout=1)
            except RuntimeError:
                rounds = 0
                if settled:
                    if restarts >= self.p.launcher_restart_cap:
                        raise RuntimeError("更新器已退出，但启动器多次恢复仍无可用窗口")
                    argv = [self.p.launcher_exe, *self.p.launch_args]
                    log.info("更新器稳定退出且启动器未恢复，主动启动 (%s/%s): %s",
                             restarts + 1, self.p.launcher_restart_cap, argv)
                    subprocess.Popen(argv, cwd=os.path.dirname(self.p.launcher_exe))
                    restarts += 1
                    self._launcher_absent_since = None
                    self._launcher_pending = True
                time.sleep(self.poll)
                continue
            try:
                shot = window.screenshot(win)
            except RuntimeError:
                rounds = 0
                time.sleep(self.poll)
                continue  # 窗口存在但暂时无法截图，不启动重复实例
            blocked = self._handle_launcher_update(win, shot)
            ready = None if blocked else self._find_text(
                shot, list(self.p.done_texts) + list(self.p.update_texts)
                + list(self.p.updating_texts) + list(self.p.resume_texts), sliding=False)
            rounds = rounds + 1 if ready else 0
            if settled and not blocked and rounds >= self.p.done_stable_rounds:
                self._launcher_pending = False
                return win
            time.sleep(self.poll)
        raise TimeoutError("启动器/更新器交接未完成或界面未就绪")

    def _kill_launcher_processes(self):
        """结束启动器全部相关进程（含托盘驻留、窗口移出屏幕的实例）。

        先按窗口标题反查宿主进程整树结束——启动器 UI 常跑在子进程里
        （实测鹰角窗口宿主是 Games.exe，只杀 Launcher.exe 杀不掉窗口）；
        再按映像名（启动器 + 更新器）兜底。与其兼容这类残留实例，不如
        运行前一律清掉，等 restart_delay 秒后再启动。
        """
        try:
            import pygetwindow as gw
            import win32process
            pids = set()
            for w in gw.getAllWindows():
                if any(k in w.title for k in self.p.window_keywords):
                    _, pid = win32process.GetWindowThreadProcessId(w._hWnd)
                    if pid:
                        pids.add(pid)
            for pid in pids:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, text=True)
            if pids:
                log.info("已按窗口结束进程树: %s", sorted(pids))
        except Exception as exc:
            log.warning("按窗口杀进程失败: %s", exc)
        names = ([self.p.launcher_exe.split("\\")[-1]]
                 + list(self.p.updater_processes) + list(self.p.extra_processes))
        for name in dict.fromkeys(names):   # 去重，顺序稳定
            subprocess.run(["taskkill", "/IM", name, "/F", "/T"],
                           capture_output=True, text=True)

    # ---- 识别：UIA → OCR（含滑窗）→ 模板 ----
    def _find_text(self, shot, texts, **kw):
        """find_text 统一入口：默认套用画像的 max_text_len 与拆框合并阈值
        （YAML defaults/厂商节可调）。各识别路径都从这里走，保证阈值全链路一致。"""
        kw.setdefault("max_text_len", self.p.max_text_len)
        kw.setdefault("merge_height_ratio", self.p.merge_band_height_ratio)
        kw.setdefault("merge_center_ratio", self.p.merge_band_center_ratio)
        kw.setdefault("merge_gap_ratio", self.p.merge_gap_ratio)
        kw.setdefault("exact_neighbor_ratio", self.p.exact_neighbor_ratio)
        kw.setdefault("boundary_group", self.p.boundary_group)
        kw.setdefault("anchor_recover", self.p.anchor_recover)
        return ocr_mod.find_text(shot, texts, **kw)

    def _find_button(self, win, shot, texts):
        """返回截图（窗口相对）坐标系的命中框；三种通道统一口径。"""
        if self.p.prefer_uia:
            box = uia.find_by_text(self.p.window_keywords, texts)
            if box:
                log.info("UIA 命中: %s", texts)
                # UIA 返回屏幕绝对坐标，换算成截图坐标（截图四边内缩
                # SHOT_INSET），与 OCR/模板命中保持同一坐标系
                ox, oy = win.left + window.SHOT_INSET, win.top + window.SHOT_INSET
                return (box[0] - ox, box[1] - oy, box[2] - ox, box[3] - oy)
        # exclude 掉「预下载已完成」这类状态标签：含「完成」的不是按钮，不能点
        hit = self._find_text(shot, texts, sliding=True, exclude=self.p.update_exclude)
        if hit:
            box, text, score = hit
            log.info("OCR 命中: %s (%.2f)", text, float(score))
            return box
        tpl = self.p.templates.get("update_btn")
        if tpl:
            from . import vision
            m = vision.match_template(shot, tpl)
            if m:
                log.info("模板命中 (%.2f)", m[1])
                return m[0]
        return None

    def _ensure_game_page(self, win) -> bool:
        """多游戏启动器切到目标游戏页：附加参数优先，模板匹配保底。"""
        p = self.p
        if not self.game or self.game not in (p.games or {}):
            return True
        from . import gameswitch
        ok = gameswitch.switch_game(
            win, p.launcher_exe, self.game, self.template_dir,
            click_icon=lambda x, y: inp.click(x, y),
            vendor=p.vendor, arg_template=p.cli_switch_arg)
        log.info("切页结果: %s", "成功" if ok else "失败")
        return ok

    def _pending_update_items(self, win):
        """扫描启动器是否还有未完成的更新项（预下载/立即更新/更新…）。

        多游戏启动器（米哈游）用附加参数逐页检查各游戏（全程无点击）；
        单游戏启动器只查当前页。返回值语义：
          - 确认有更新项的游戏 key 列表；
          - 【查不动（页面未就绪/切页未确认）也计入返回】——保守策略，
            宁可保持启动器运行，绝不因扫描失败误杀进程。
        """
        p = self.p
        words = list(dict.fromkeys(list(p.update_texts) + ["预下载"]))
        engine = ocr_mod.default_engine()
        all_games = list((p.games or {}).keys())
        if not all_games:
            hit = None
            for _ in range(3):
                time.sleep(1.5)
                shot = window.screenshot(win)
                hit = self._find_text(shot, words, engine=engine, sliding=False,
                                        exclude=self.p.update_exclude)
                if hit:
                    break
            return [p.vendor] if hit else []

        # 本会话已成功更新的游戏页不再复查：每轮都全扫一遍的话 4 个子项
        # 就是 16 次页面识别；只有最后一轮才扫描，且只扫没验证过的页
        games = [g for g in all_games if g not in self.skip_pending_scan]
        if not games:
            log.info("[%s] 其余游戏本会话均已更新并验证，跳过逐页收尾扫描", p.vendor)
            return []

        from . import gameswitch
        vendor, arg_tpl = p.vendor, p.cli_switch_arg
        # 刚启动的实例页面还在渲染，先等 UI 就绪再扫描
        deadline = time.monotonic() + 60
        ready = False
        while time.monotonic() < deadline:
            time.sleep(2.5)
            try:
                shot0 = window.screenshot(win)
            except RuntimeError:
                continue
            if any(gameswitch.on_game_page(shot0, g, engine, vendor=vendor)
                   for g in games):
                ready = True
                break
        if not ready:
            log.warning("[%s] 启动器 UI 60s 未就绪，更新项扫描弃权（保守：不结束进程）", p.vendor)
            return games

        hold = []   # 有更新项 + 查不动的，都会阻止结束进程
        for g in games:
            if not gameswitch.switch_via_cli(p.launcher_exe, g, vendor=vendor,
                                             arg_template=arg_tpl):
                hold.append(g)
                continue
            deadline = time.monotonic() + 25
            on = False
            while time.monotonic() < deadline:
                time.sleep(2.5)
                try:
                    shot = window.screenshot(win)
                except RuntimeError:
                    continue          # 切页瞬间窗口可能闪失
                if gameswitch.on_game_page(shot, g, engine, vendor=vendor):
                    on = True
                    break
            if not on:
                log.warning("[%s] %s 切页未确认，按未知处理（保守：不结束进程）", p.vendor, g)
                hold.append(g)
                continue
            # Logo 先渲染、按钮后渲染：确认页面后多试几轮再下结论
            hit = None
            for _ in range(3):
                time.sleep(1.5)
                shot = window.screenshot(win)
                hit = self._find_text(shot, words, engine=engine, sliding=False,
                                        exclude=self.p.update_exclude)
                if hit:
                    break
            if hit:
                log.info("[%s] %s 仍有更新项: %s (%.2f)", p.vendor, g, hit[1], hit[2])
                hold.append(g)
        # 检查完切回目标游戏页
        if self.game in (p.games or {}):
            gameswitch.switch_via_cli(p.launcher_exe, self.game, vendor=vendor,
                                      arg_template=arg_tpl)
        return hold

    def _finish(self, win, ok: bool) -> bool:
        """更新成功后的收尾：满足「更新器进程已退出 且 启动器无其余
        子任务更新项」才结束启动器进程树；否则保持运行，留给后续流程。"""
        if window.process_running(self.p.updater_processes):
            log.info("[%s] 更新器进程仍在运行，保持启动器", self.p.vendor)
            return ok
        if not self.final_for_vendor:
            # 计划里本厂商还有后续游戏：下一轮 run 开头会杀进程重启启动器，
            # 现在扫描/关进程都立刻作废，直接跳过（逐页扫描留到最后一轮）
            log.info("[%s] 计划内还有本厂商后续游戏，跳过收尾扫描与关启动器",
                     self.p.vendor)
            return ok
        pending = self._pending_update_items(win)
        if pending:
            log.info("[%s] 启动器还有其他更新项 %s，保持运行", self.p.vendor, pending)
            return ok
        self._shutdown(win)
        return ok

    def _shutdown(self, win):
        """检测到开始游戏等完成标志后，taskkill 退出启动器整棵进程树
        （启动器含多个子项游戏，/T 把子进程一并结束）。"""
        name = self.p.launcher_exe.split("\\")[-1]
        log.info("结束启动器进程树: %s", name)
        try:
            import win32process
            _, pid = win32process.GetWindowThreadProcessId(win._hWnd)
            if pid:
                r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   capture_output=True, text=True)
                log.info("taskkill /PID %s: %s", pid, r.stdout.strip() or r.stderr.strip())
        except Exception as exc:
            log.warning("按窗口 PID 结束失败（%s），退回按映像名", exc)
            subprocess.run(["taskkill", "/IM", name, "/T", "/F"],
                           capture_output=True, text=True)
        # 伴生进程兜底：如 HYPHelper 可能挂在已退出的 launcher.exe 下，
        # 不属于窗口进程树，/PID /T 杀不到，按映像名补刀
        for extra in dict.fromkeys(self.p.extra_processes):
            subprocess.run(["taskkill", "/IM", extra, "/F", "/T"],
                           capture_output=True, text=True)

    def run(self, timeout: int = 7200) -> bool:
        p = self.p
        log.info("[%s] 灾备更新启动", p.vendor)
        # 运行前：清掉托盘/屏外残留实例，等进程真正退出后再启动——
        # 否则单实例启动器可能只在托盘响应，窗口不可见/屏外，后续全错。
        self._kill_launcher_processes()
        time.sleep(self.restart_delay)
        argv = [p.launcher_exe, *p.launch_args]
        log.info("启动启动器: %s", " ".join(argv))
        subprocess.Popen(argv, cwd=p.launcher_exe.rsplit("\\", 1)[0])
        deadline = time.time() + timeout
        win = self._wait_launcher_ready(None, deadline)
        if not self._ensure_game_page(win):
            raise RuntimeError(f"[{p.vendor}] 无法切换到游戏页: {self.game}")
        if not p.click_updates:
            return self._run_observer(win, timeout)   # 异环：只识别，不点击
        deadline = time.time() + timeout
        done_rounds = 0
        clicked = False
        confirm_clicks = 0      # 二次确认弹窗点击次数（防死循环）
        resume_clicks = 0       # 「已暂停」恢复点击次数（超限尊重用户暂停）
        pct100_seen = False     # 见过进度 100%（下载段结束）
        verify_seen = False     # 见过「校验中」（100% 后可能进入资源校验，也可能没有）
        no_pct_rounds = 0       # 连续读不到进度的轮数
        while time.time() < deadline:
            self._launcher_process_settled()
            if self._launcher_pending:
                win = self._wait_launcher_ready(win, deadline)
                if not self._ensure_game_page(win):
                    raise RuntimeError("启动器恢复后无法确认目标游戏页")
                done_rounds = 0
            try:
                shot = window.screenshot(win)
            except RuntimeError:
                # 启动器自更新时 UI 进程会整体退出（实测鹰角：Updater.exe
                # 存活期间窗口消失），等更新器结束、窗口回来再继续
                done_rounds = 0
                self._launcher_pending = True
                log.info("窗口丢失，重新查找")
                time.sleep(self.poll)
                continue
            if self._handle_launcher_update(win, shot):
                done_rounds = 0
                time.sleep(self.poll)
                continue
            if self._launcher_pending:
                win = self._wait_launcher_ready(win, deadline)
                if not self._ensure_game_page(win):
                    raise RuntimeError("启动器更新后无法确认目标游戏页")
                done_rounds = 0
                continue
            busy = self._find_text(shot, list(p.updating_texts) + list(p.verifying_texts), sliding=False)
            if not clicked:                                  # 阶段1：找更新按钮并点击
                # resume_texts 一并搜索：启动器可能带着「已暂停」的下载进来
                box = self._find_button(win, shot, list(p.update_texts) + list(p.resume_texts))
                if box:
                    cx, cy = _box_center(box)
                    inp.click(*window.to_screen(win, cx, cy))
                    clicked = True
                    log.info("已点击更新按钮")
                    time.sleep(5)
                    continue
                if not busy and self._find_text(shot, p.done_texts, sliding=False):
                    # 完成按钮是次优先信号：当且仅当「预下载」不在场才算最新
                    if self._find_text(shot, p.predownload_texts, sliding=False,
                                       exclude=p.update_exclude):
                        done_rounds = 0
                        log.info("完成按钮在但仍有预下载入口，不视为最新，继续识别")
                    else:
                        done_rounds += 1
                        if done_rounds >= p.done_stable_rounds:
                            log.info("完成按钮稳定出现且无更新弹窗，游戏已是最新")
                            return self._finish(win, True)
                else:
                    done_rounds = 0
            else:                                            # 阶段2：轮询更新进度
                alive = window.process_running(p.updater_processes)
                pct = ocr_mod.read_progress(shot)
                if pct < 0:
                    # 进度可能悬停才显示：鼠标移到下载状态文本上抓拍 tooltip 再读
                    anchor = self._find_text(
                        shot, ["下载中", "校验中", "更新中", "已暂停", "恢复下载"],
                        sliding=False)
                    if anchor:
                        ax, ay = _box_center(anchor[0])
                        inp.move(*window.to_screen(win, ax, ay))
                        time.sleep(0.8)
                        try:
                            pct = ocr_mod.read_progress(
                                window.screenshot(win, park=False))
                            if pct >= 0:
                                log.info("悬停读取进度: %.1f%%", pct)
                        except RuntimeError:
                            pass
                        inp.park()
                log.info("更新中: updater进程=%s 进度=%.1f%%", alive, pct)
                # 「开始游戏」等完成文字与预下载按钮同屏常驻，
                # 必须确认更新按钮已消失，才能认定更新完成。
                update_still = self._find_text(shot, p.update_texts, sliding=False,
                                                 exclude=p.update_exclude)
                done_hit = self._find_text(shot, p.done_texts, sliding=False)
                verifying = self._find_text(shot, p.verifying_texts, sliding=False)
                # 「预下载」入口在场时完成按钮不算数（次优先信号的前置条件）
                predown = self._find_text(shot, p.predownload_texts, sliding=False,
                                          exclude=p.update_exclude)
                # 进度语义：0~99% 一律「下载/校验中」（校验阶段也有自己的百分比）；
                # 100% 只代表下载段结束，之后可能进校验、也可能直接完成；-1 为读不到。
                if 0 <= pct < 100:
                    no_pct_rounds = 0
                elif pct >= 100:
                    pct100_seen = True
                    no_pct_rounds = 0
                else:
                    no_pct_rounds += 1
                if verifying:
                    verify_seen = True
                    log.info("资源校验中: %s", verifying[1])
                # 下载被暂停（用户手动暂停/中断后）：点「已暂停/继续下载」恢复。
                # 超过 resume_click_cap 次仍在暂停 → 用户有意暂停，尊重不再恢复。
                if p.resume_texts:
                    rsm = self._find_text(shot, p.resume_texts, sliding=False,
                                            exclude=p.update_exclude)
                    if rsm:
                        if resume_clicks < p.resume_click_cap:
                            cx, cy = _box_center(rsm[0])
                            inp.click(*window.to_screen(win, cx, cy))
                            resume_clicks += 1
                            log.info("检测到下载已暂停，点击恢复 (%s/%s): %s",
                                     resume_clicks, p.resume_click_cap, rsm[1])
                            time.sleep(3)
                        else:
                            log.warning("已恢复 %s 次仍被暂停，视为用户有意暂停，不再自动恢复",
                                        resume_clicks)
                        continue
                # 二次确认弹窗（实测：预下载点「预下载」后弹「确认下载」，不点则
                # 永远卡在 0%）。下载未起步（进度<=0/读不到）或下载段结束
                # （>=100% 后的完成/重启提示）时都尝试，上限 confirm_click_cap 次。
                if (p.confirm_texts and (pct <= 0 or pct >= 100)
                        and confirm_clicks < p.confirm_click_cap):
                    cfm = self._find_text(shot, p.confirm_texts, sliding=False)
                    if cfm:
                        cx, cy = _box_center(cfm[0])
                        inp.click(*window.to_screen(win, cx, cy))
                        confirm_clicks += 1
                        log.info("已点击二次确认按钮: %s", cfm[1])
                        time.sleep(3)
                        continue
                # 完成判定四道闸（缺一即继续等）：
                #  1. 更新按钮消失 且 完成文字在 且 不在校验中 且 预下载入口不在
                #     （完成按钮是次优先信号，当且仅当预下载不存在时才生效）；
                #  2. 当前进度不在 0~99（下载/校验进行中绝不算完成）；
                #  3. 见过终点迹象（到过 100% / 经历过校验 / 连续 3 轮读不到进度），
                #     防止「开始游戏」常驻 + OCR 抖动在 0% 时误判完成。
                if (not update_still and done_hit and not busy and not verifying and not predown
                        and not (0 <= pct < 100)
                        and (pct100_seen or verify_seen or no_pct_rounds >= 3)):
                    done_rounds += 1
                    if done_rounds >= p.done_stable_rounds:
                        log.info("更新完成")
                        ok = self._verify(win)
                        return self._finish(win, ok)
                else:
                    done_rounds = 0
                err = self._find_text(shot, p.retry_texts, sliding=False)
                if err and not alive:                        # 错误弹窗：点重试
                    log.warning("检测到弹窗，点击重试")
                    cx, cy = _box_center(err[0])
                    inp.click(*window.to_screen(win, cx, cy))
            time.sleep(self.poll)
        raise TimeoutError(f"[{p.vendor}] 灾备更新超时 {timeout}s")

    def _run_observer(self, win, timeout: int) -> bool:
        """异环模式：两阶段都由启动器主导，这里只点确认类弹窗，不点更新入口。

        实测流程：
          1. 启动器更新：可能多次弹「确认更新/确定」等提示，点击后
             NTEUpdate.exe 运行；完成后可能还要点「完成/重启启动器」等
             按钮，启动器随之重启（窗口可能整体消失再回来）；
          2. 每次启动 NTEUpdate.exe 都会做更新检查（一瞬到几分钟），
             进程存活 ≠ 更新中——只看进程会把「检查」误判成更新；
          3. 游戏更新由启动器自动进行（更新中/下载中/正在更新…），完成后
             出现「开始游戏」——只有游戏更新完成才算任务完成。

        完成判定（连续 done_stable_rounds 轮全部满足才收尾）：
          完成按钮在 且 预下载入口不在 且 无更新中文案 且 进度不在 0~99
          且没有待处理启动器弹窗，首轮界面观察时间已到。
          确认按钮不受进程限制；启动器阶段须通过五分钟进程消失检查。
        """
        p = self.p
        deadline = time.time() + timeout
        grace = time.time() + p.observer_grace
        done_rounds = 0              # 完成按钮连续稳定命中轮数
        confirm_clicks = 0
        confirm_words = list(p.confirm_texts) or list(p.retry_texts)
        updating_words = list(p.updating_texts) + list(p.verifying_texts)
        while time.time() < deadline:
            self._launcher_process_settled()
            if self._launcher_pending:
                win = self._wait_launcher_ready(win, deadline)
                done_rounds = 0
            alive = window.process_running(p.updater_processes)
            try:
                shot = window.screenshot(win)
            except RuntimeError:
                # 重启后的新实例重新获取窗口，不依赖更新器进程。
                done_rounds = 0
                self._launcher_pending = True
                log.info("窗口丢失，重新查找")
                time.sleep(self.poll)
                continue
            if self._handle_launcher_update(win, shot):
                done_rounds = 0
                time.sleep(self.poll)
                continue
            if self._launcher_pending:
                win = self._wait_launcher_ready(win, deadline)
                done_rounds = 0
                continue
            pct = ocr_mod.read_progress(shot)
            updating = self._find_text(shot, updating_words, sliding=False)
            predown = self._find_text(shot, p.predownload_texts, sliding=False,
                                      exclude=p.update_exclude)
            done_hit = self._find_text(shot, p.done_texts, sliding=False)
            log.info("观察中: updater进程=%s 进度=%.1f%% 更新中=%s 预下载=%s 完成按钮=%s",
                     alive, pct,
                     updating[1] if updating else "-",
                     predown[1] if predown else "-",
                     done_hit[1] if done_hit else "-")
            if updating or 0 <= pct < 100:
                # 更新/下载进行中：只等，不点任何按钮
                done_rounds = 0
                time.sleep(self.poll)
                continue
            if done_hit and not predown and time.time() > grace:
                done_rounds += 1
                log.info("完成按钮稳定命中 %s/%s 轮", done_rounds, p.done_stable_rounds)
                if done_rounds >= p.done_stable_rounds:
                    log.info("游戏更新完成（完成按钮稳定出现且无预下载项）")
                    return self._finish(win, True)
            else:
                done_rounds = 0
                # 非完成态：识别并点击确认类弹窗（启动器更新各阶段的
                # 确认/完成/重启提示，可能多次）；启动器弹窗已经优先处理。
                if (not done_hit
                        and confirm_clicks < p.observer_confirm_cap):
                    hit = self._find_text(shot, confirm_words, sliding=False)
                    if hit:
                        cx, cy = _box_center(hit[0])
                        inp.click(*window.to_screen(win, cx, cy))
                        confirm_clicks += 1
                        log.info("已点击确认弹窗 (%s/%s): %s",
                                 confirm_clicks, p.observer_confirm_cap, hit[1])
                        time.sleep(2)
                        continue
            time.sleep(self.poll)
        raise TimeoutError(f"[{p.vendor}] 启动器更新观察超时 {timeout}s")

    def _verify(self, win) -> bool:
        ver = ocr_mod.read_version(window.screenshot(win))
        log.info("界面版本号: %s", ver or "未读到")
        return bool(ver)
