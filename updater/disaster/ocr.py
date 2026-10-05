# -*- coding: utf-8 -*-
"""OCR 通道：引擎封装 + 滑窗扫描（集成 sliding_ocr.py）。

引擎为 rapidocr 3.x（默认 PP-OCRv6 det/rec 模型；PP-OCRv5/v4 可配），
onnxruntime / openvino 推理。旧版 rapidocr-onnxruntime 包已停止维护
（1.2.3 为最后一版），本模块通过适配层把新版输出对象统一成旧行格式
[(box, text, score)], elapse，下游 sliding_ocr / desktop_update 无需改动。

加速器选择（GPU / NPU）：
  环境变量 OCR_ACCELERATOR = auto | gpu | npu | cpu（默认 auto）
  - gpu：优先 CUDAExecutionProvider（RTX 4060，需 onnxruntime-gpu），
    其次 DmlExecutionProvider（任意 DX12 显卡，需 onnxruntime-directml）。
  - npu：OpenVINOExecutionProvider（Core Ultra NPU，需 onnxruntime-openvino）。
  - auto：gpu 候选全部探测后再 npu 候选，全部不可用退回 CPU。
  执行器「被列出」不代表「能用」（CUDA/cuDNN 版本错配会静默退回 CPU），
  建引擎后会实测会话真实执行器，失配的进拒绝名单并按剩余候选重选。
"""
import logging
import os
import re

from . import sliding_ocr

log = logging.getLogger("ocr")

_engine = None
_CUDA_PATH_READY = False

# 被实测否决的执行器（如 CUDA 被列出但版本错配实际加载失败），检测时跳过
_REJECTED_EPS = set()

# (执行器名称, 类别, 安装提示)
_GPU_EPS = [
    ("CUDAExecutionProvider", "gpu", "pip install onnxruntime-gpu（需匹配 CUDA/cuDNN）"),
    ("DmlExecutionProvider", "gpu", "pip install onnxruntime-directml"),
]
_NPU_EPS = [
    ("OpenVINOExecutionProvider", "npu", "pip install onnxruntime-openvino"),
    ("VitisAIExecutionProvider", "npu", "pip install onnxruntime-vitisai"),
]


def _available_eps():
    import onnxruntime as ort
    return set(ort.get_available_providers())


def detect_accelerator(preference=None):
    """按可用执行器探测加速器。返回 (类别, 执行器名)；CPU 返回 ('cpu', 'CPUExecutionProvider')。"""
    preference = (preference or os.environ.get("OCR_ACCELERATOR", "auto")).lower()
    available = _available_eps()
    if preference not in ("auto", "gpu", "npu", "cpu"):
        log.warning("未知 OCR_ACCELERATOR=%s，按 auto 处理", preference)
        preference = "auto"
    if preference == "cpu":
        return "cpu", "CPUExecutionProvider"
    groups = []
    if preference in ("auto", "gpu"):
        groups.extend(_GPU_EPS)
    if preference in ("auto", "npu"):
        groups.extend(_NPU_EPS)
    for ep, kind, hint in groups:
        if ep in available and ep not in _REJECTED_EPS:
            log.info("OCR 加速器命中：%s（%s）", ep, kind)
            return kind, ep
    if _REJECTED_EPS:
        log.info("OCR 加速器 %s 已被实测否决", sorted(_REJECTED_EPS))
    else:
        log.info("未检测到 GPU/NPU 执行器，OCR 使用 CPU；可通过 %s 启用加速",
                 "；".join(hint for _, _, hint in _GPU_EPS + _NPU_EPS))
    return "cpu", "CPUExecutionProvider"


def _ensure_cuda_dll_path():
    """把 CUDA toolkit 的 bin 目录加入 DLL 搜索路径。

    onnxruntime-gpu 不自带 cublas/cuDNN，会话创建时才动态加载；
    若启动环境没继承 PATH（如计划任务、某些终端），这里用 CUDA_PATH
    环境变量和默认安装目录兜底，保证 CUDA 引擎在任何上下文都能初始化。
    """
    global _CUDA_PATH_READY
    if _CUDA_PATH_READY:
        return
    _CUDA_PATH_READY = True
    import glob
    candidates = []
    for key, value in os.environ.items():
        if key.upper().startswith("CUDA_PATH") and value:
            candidates.append(os.path.join(value, "bin"))
    candidates += glob.glob(r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v*\bin")
    for path in candidates:
        if not os.path.isdir(path):
            continue
        if path not in os.environ["PATH"].split(os.pathsep):
            os.environ["PATH"] = path + os.pathsep + os.environ["PATH"]
        try:
            os.add_dll_directory(path)
        except (OSError, AttributeError):
            pass


def _build_rapidocr(ep: str):
    """按执行器建 rapidocr 3.x 引擎（原生支持 CUDA / DML / OpenVINO）。"""
    try:
        from rapidocr import RapidOCR
    except ImportError:
        raise RuntimeError("OCR 需 pip install rapidocr（rapidocr-onnxruntime 已停止维护）")
    if ep == "CPUExecutionProvider":
        return RapidOCR()
    if ep == "CUDAExecutionProvider":
        return RapidOCR(params={"EngineConfig.onnxruntime.use_cuda": True})
    if ep == "DmlExecutionProvider":
        return RapidOCR(params={"EngineConfig.onnxruntime.use_dml": True})
    if ep == "OpenVINOExecutionProvider":
        return RapidOCR(params={"Global.engine_type": "openvino"})
    raise RuntimeError(f"rapidocr 未内置执行器 {ep} 的配置方式")


class _RapidOCRAdapter:
    """rapidocr 3.x 输出适配回旧行格式 [(box, text, score)], elapse。

    box 转成纯 Python 列表（[[x,y]...]），下游 click_box_center /
    sliding_ocr / desktop_update 的坐标计算无需任何改动。
    """

    def __init__(self, engine):
        self._engine = engine

    def __call__(self, img):
        out = self._engine(img)
        if out is None or getattr(out, "boxes", None) is None:
            return [], 0.0
        rows = [(box.tolist() if hasattr(box, "tolist") else box, text, float(score))
                for box, text, score in zip(out.boxes, out.txts, out.scores)]
        return rows, float(getattr(out, "elapse", 0.0) or 0.0)


def _session_providers(engine):
    """取引擎各模型会话实际使用的执行器（结构随 rapidocr 版本变化，尽力探测）。

    get_available_providers() 只说明「装了这个执行器」，不代表能用——
    CUDA/cuDNN 版本错配时会话创建阶段就静默退回 CPU，从这里才能看出真相。
    """
    try:
        providers = set()
        for name in ("text_det", "text_cls", "text_rec"):
            part = getattr(engine, name, None)
            sess = getattr(part, "session", None)
            sess = getattr(sess, "session", sess)
            if hasattr(sess, "get_providers"):
                providers.update(sess.get_providers())
        return sorted(providers) if providers else None
    except Exception:
        return None


def default_engine():
    global _engine
    if _engine is None:
        _ensure_cuda_dll_path()
        # 每轮：按候选建引擎 → 实测会话执行器；初始化抛错或被静默退回的
        # EP 进拒绝名单后重选。迭代上限 = 全部加速候选 + CPU，保证终止。
        for _ in range(len(_GPU_EPS) + len(_NPU_EPS) + 1):
            kind, ep = detect_accelerator()
            try:
                raw = _build_rapidocr(ep)
            except Exception as exc:
                log.warning("OCR 加速器 %s 初始化失败（%s）", ep, exc)
                if kind == "cpu":
                    raise
                _REJECTED_EPS.add(ep)
                continue
            actual = _session_providers(raw)
            if actual and ep not in actual:
                log.warning("OCR 请求 %s，但实际会话执行器为 %s；执行器被列出但加载失败，"
                            "移出候选后重选", ep, actual)
                _REJECTED_EPS.add(ep)
                continue
            if kind != "cpu":
                log.info("OCR 会话已启用 %s", ep)
            _engine = _RapidOCRAdapter(raw)
            break
        if _engine is None:
            _engine = _RapidOCRAdapter(_build_rapidocr("CPUExecutionProvider"))
    return _engine


# 裸短词只允许整词相等：「更新」若按子串/前缀匹配，会误命中公告里的
# 「版本更新后」「更新公告」等长句（按钮文案都是独立短词）。
_EXACT_ONLY_KEYWORDS = {"更新"}
# 整词相等的置信度下限要更高：真实按钮文字衬在纯色按钮上置信度普遍
# 0.9+；而公告长句被检测模型切碎后恰好读成独立「更新」的碎片，置信度
# 实测只有 0.75（2026-09-24 鹰角页把「版本更新说明」切碎后误点公告列表）。
# 低于此下限的整词命中一律视为碎片误读，不放行。
_EXACT_MIN_SCORE = 0.85


def _row_bounds(box):
    """OCR 四点框 -> (xmin, xmax, ymin, ymax)。"""
    xs = [float(p[0]) for p in box]
    ys = [float(p[1]) for p in box]
    return min(xs), max(xs), min(ys), max(ys)


def _cluster_bands(rows, height_ratio, center_ratio):
    """并查集把识别行聚成水平带（merge_rows / window_rows 共用）。

    返回 (bands, geo, h)：bands = [行号列表]，geo 为各行轴对齐边界
    (xmin, xmax, ymin, ymax)，h 为各行字高。
    """
    geo = [_row_bounds(box) for box, _, _ in rows]
    h = [ymax - ymin for _, _, ymin, ymax in geo]
    cy = [(ymin + ymax) / 2 for _, _, ymin, ymax in geo]

    def same_band(i, j):
        base = min(h[i], h[j])
        return (abs(h[i] - h[j]) <= height_ratio * base
                and abs(cy[i] - cy[j]) <= center_ratio * base)

    # 并查集：两两同带即归并，聚出全部水平带
    parent = list(range(len(rows)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if same_band(i, j):
                parent[find(i)] = find(j)
    bands = {}
    for i in range(len(rows)):
        bands.setdefault(find(i), []).append(i)
    return list(bands.values()), geo, h


def merge_rows(rows, height_ratio=0.06, center_ratio=0.05, gap_ratio=1.2):
    """把同一行但被检测模型拆成多框的文字拼回一行（find_text 预处理）。

    判定规则（阈值均由 win-games.yaml 的 merge_* 配置，单位是相对值）：
      - 同一水平带：两段字高差 ≤ height_ratio×min(字高)，且中心点纵向差
        ≤ center_ratio×min(字高)（字高 = 框 ymax-ymin，均以较小者为基准）；
      - 带内按 xmin 排序后贪心拼接：相邻两段水平间隙 ≤ gap_ratio×两段
        平均字高时直接拼接（文本无分隔拼接，score 取各段最小值）。
    未参与合并的行原样保留。返回 [(box, text, score)]（拼接行的 box 为
    合并后的轴对齐四边形，点击中心仍在文字区域中间）。
    """
    rows = [(box, str(text), float(score)) for box, text, score in (rows or [])]
    if len(rows) < 2:
        return rows
    bands, geo, h = _cluster_bands(rows, height_ratio, center_ratio)

    def join(idxs):
        if len(idxs) == 1:
            return rows[idxs[0]]
        x0 = min(geo[i][0] for i in idxs)
        x1 = max(geo[i][1] for i in idxs)
        y0 = min(geo[i][2] for i in idxs)
        y1 = max(geo[i][3] for i in idxs)
        box = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
        return (box, "".join(rows[i][1] for i in idxs),
                min(rows[i][2] for i in idxs))

    out = []
    for idxs in bands:
        idxs.sort(key=lambda i: (geo[i][0], geo[i][2]))
        cur = [idxs[0]]
        for k in idxs[1:]:
            a = cur[-1]
            if geo[k][0] - geo[a][1] <= gap_ratio * (h[a] + h[k]) / 2:
                cur.append(k)           # 间隙够小：并入当前段
            else:
                out.append(join(cur))   # 间隙过大：当前段收尾，另起新段
                cur = [k]
        out.append(join(cur))
    return out


def drop_fragment_exact(rows, keywords, neighbor_ratio=3.0):
    """整词命中的碎片复查：同带邻框存在即丢弃。

    背景：公告长句被检测模型切碎后，碎片可能恰好读成独立的整词（如
    「更新」）；merge_rows 只合并间隙 ≤1.2×字高的「整齐拆框」，参差碎片
    会幸存并靠 _EXACT_MIN_SCORE 兜底。这里用更宽的邻域复查：整词候选框
    的同视觉行（中心点纵向差 ≤ 0.5×max 字高）上、水平 neighbor_ratio×
    候选框字高范围内还存在其他文字时，判定为碎片而非按钮，丢弃之。
    真实按钮文字周围同带通常无其他文字行，不受影响。
    """
    exact = [k for k in keywords if k in _EXACT_ONLY_KEYWORDS]
    if not exact or len(rows) < 2:
        return list(rows or [])
    geo = [_row_bounds(box) for box, _, _ in rows]
    h = [g[3] - g[2] for g in geo]
    cy = [(g[2] + g[3]) / 2 for g in geo]
    keep = []
    for i, (box, text, score) in enumerate(rows):
        t = re.sub(r"\s+", "", str(text))
        if t in exact:                      # 整词候选才复查（置信度门槛在 matches 内）
            for j in range(len(rows)):
                if j == i:
                    continue
                if abs(cy[j] - cy[i]) > 0.5 * max(h[i], h[j]):
                    continue                # 不在同一条视觉行
                # 合并行与覆盖它的窗口行互为包含（中心互在对方框内），
                # 是同一文字区域的重复候选，不算「其他文字」
                cx_i = (geo[i][0] + geo[i][1]) / 2
                cx_j = (geo[j][0] + geo[j][1]) / 2
                if (geo[i][0] <= cx_j <= geo[i][1]
                        and geo[i][2] <= cy[j] <= geo[i][3]):
                    continue
                if (geo[j][0] <= cx_i <= geo[j][1]
                        and geo[j][2] <= cy[i] <= geo[j][3]):
                    continue
                gap = max(geo[j][0] - geo[i][1], geo[i][0] - geo[j][1])
                if gap <= neighbor_ratio * h[i]:
                    log.info("整词复查丢弃碎片: %r（同带 %.1f 倍字高内有其他文字）",
                             t, neighbor_ratio)
                    break
            else:
                keep.append(rows[i])
            continue
        keep.append(rows[i])
    return keep


def _assemble_by_boundary(img_arr, rows, keywords, match_text, min_score,
                          max_text_len, height_ratio, center_ratio):
    """第二层「带内边界拼接」：离散短碎片按按钮边界归组后配词典。

    主路径（先拼后配）未命中时启用，不改动主路径任何行为。流程：
    同带碎片中枚举连续候选组（拼接文本须为某关键词的子串，右扩遇到
    非子串即剪枝——更长的串不可能是子串；单碎片已成整词关键词的组
    跳过，那是主路径 + 整词复查的职责，本层只负责拼接）；以组外包框
    为锚用 button.find_boundary 检测按钮边界；边界检出后，收集中心
    落在边界内、且字高与组相容（差 ≤ 0.5×min 字高，滤掉边界内的
    图标/小字，如汉堡图标）的全部碎片，按 x 序拼接过 match_text。

    边界是物理分组依据：字距大到几何合并失效（merge_gap_ratio 怎么
    调都两难）时，同一按钮内的碎片照样归组；边界外的同行文字（公告
    行、徽章）天然排除。命中的 box 为按钮边界四点框，点击中心更准。
    """
    from . import button
    rows = [(box, str(t), float(s)) for box, t, s in rows or []]
    if not rows:
        return None
    bands, geo, h = _cluster_bands(rows, height_ratio, center_ratio)
    centers = [((g[0] + g[1]) / 2, (g[2] + g[3]) / 2) for g in geo]
    tried = set()
    for idxs in bands:
        idxs.sort(key=lambda i: geo[i][0])
        frags = []
        for i in idxs:
            t = re.sub(r"\s+", "", rows[i][1])
            if t and rows[i][2] >= min_score and len(t) <= max_text_len:
                frags.append((i, t))
        n = len(frags)
        for a in range(n):
            text = ""
            for b in range(a, n):
                text += frags[b][1]
                if len(text) > max_text_len:
                    break
                if not any(text in k for k in keywords):
                    break               # 非子串：再右扩也不可能是子串
                if len(frags[a:b + 1]) == 1 and text in _EXACT_ONLY_KEYWORDS:
                    continue            # 单碎片整词归主路径与整词复查管
                gidx = tuple(f[0] for f in frags[a:b + 1])
                if gidx in tried:
                    continue
                tried.add(gidx)
                x0 = min(geo[i][0] for i in gidx)
                x1 = max(geo[i][1] for i in gidx)
                y0 = min(geo[i][2] for i in gidx)
                y1 = max(geo[i][3] for i in gidx)
                boundary = button.find_boundary(
                    img_arr, [[x0, y0], [x1, y0], [x1, y1], [x0, y1]])
                if boundary is None:
                    continue
                l, t2, r, b2 = boundary
                gh = max(h[i] for i in gidx)
                inside = []
                for i in range(len(rows)):
                    cx, cy = centers[i]
                    if not (l <= cx <= r and t2 <= cy <= b2):
                        continue
                    if abs(h[i] - gh) > 0.5 * min(h[i], gh):
                        continue        # 字高不相容：图标/小字不并入
                    if rows[i][2] >= min_score:
                        inside.append(i)
                inside.sort(key=lambda i: geo[i][0])
                text2 = "".join(re.sub(r"\s+", "", rows[i][1])
                                for i in inside)
                if match_text(text2):
                    lf, tf, rf, bf = (float(v) for v in boundary)
                    return ([[lf, tf], [rf, tf], [rf, bf], [lf, bf]],
                            text2, min(rows[i][2] for i in inside))
    return None


def find_text(shot, keywords: list, engine=None, sliding: bool = True,
              min_score: float = 0.6, max_text_len: int = 6, exclude: tuple = (),
              merge_height_ratio: float = 0.06,
              merge_center_ratio: float = 0.05,
              merge_gap_ratio: float = 1.2,
              exact_neighbor_ratio: float = 3.0,
              boundary_group: bool = True,
              anchor_recover: bool = True):
    """四层流水线：先拼后配（原逻辑）→ 带内边界拼接 → 锚点重识别 → 滑窗。

    第一层（主匹配逻辑，保持原样）：merge_rows 拆框合并 →
    drop_fragment_exact 整词复查 → matches 过滤（置信度 ≥ min_score；
    去空白长度 ≤ max_text_len；不含 exclude 子串；词表前缀匹配，
    _EXACT_ONLY_KEYWORDS 整词相等且需 ≥ _EXACT_MIN_SCORE）。
    后续层只在第一层未命中时依次追加，不回溯修改第一层的判定：
      - boundary_group：同带离散短碎片（单/双/三字）按按钮边界归组
        拼接后过同一套文本判定（_assemble_by_boundary，见其 docstring）；
      - anchor_recover：部分命中碎片（关键词真子串）为锚，边缘检测定
        按钮边界，边界内放大重 OCR 再拼接判定（button.recover_by_anchor，
        治整图尺度漏检；书法字错读、淡色字像素缺失治不了）；
      - 滑窗多层扫描（sliding_ocr，第一层逻辑）。
    返回 (box, text, score)；找不到返回 None。"""
    import numpy as np
    from . import button
    engine = engine or default_engine()

    def prepare(rows):
        merged = merge_rows(rows, merge_height_ratio, merge_center_ratio,
                            merge_gap_ratio)
        return drop_fragment_exact(merged, keywords, exact_neighbor_ratio)

    def match_text(text):
        """纯文本判定（长度/排除词/词表），边界拼接与锚点重识别复用。"""
        text = re.sub(r"\s+", "", str(text))
        if not text or len(text) > max_text_len:
            return False
        if any(x in text for x in exclude):
            return False
        for k in keywords:
            if k in _EXACT_ONLY_KEYWORDS:
                if text == k:
                    return True
            elif text.startswith(k):
                return True
        return False

    def matches(row):
        if float(row[2]) < min_score:
            return False
        text = re.sub(r"\s+", "", str(row[1]))
        if not match_text(text):
            return False
        for k in keywords:
            if (k in _EXACT_ONLY_KEYWORDS and text == k
                    and float(row[2]) < _EXACT_MIN_SCORE):
                return False
        return True

    rows, _ = engine(np.asarray(shot))
    for r in prepare(rows):
        if matches(r):
            return r
    if boundary_group:
        hit = _assemble_by_boundary(np.asarray(shot), rows, keywords,
                                    match_text, min_score, max_text_len,
                                    merge_height_ratio, merge_center_ratio)
        if hit:
            log.info("带内边界拼接命中: %s (%.2f)", hit[1], float(hit[2]))
            return hit
    if anchor_recover:
        hit = button.recover_by_anchor(shot, engine, rows, keywords,
                                       match_text, min_score=min_score)
        if hit:
            log.info("锚点边界重识别命中: %s (%.2f)", hit[1], float(hit[2]))
            return hit
    if not sliding:
        return None
    result = sliding_ocr.scan(shot, engine, matches,
                              {"grids": [2, 3, 4], "overlap": 0.8,
                               "max_seconds": 20, "min_crop_width": 800},
                              preprocess=prepare)
    for r in result:
        if matches(r):
            return r
    return None


def read_progress(shot) -> float:
    """OCR 读进度百分比（0-100）；读不到返回 -1。

    两阶段：先找带 % 的文本；找不到再认 HoYoPlay 7.x 的圆形进度徽章——
    「圆环内纯数字（无 % 号）+ 附近『下载中/校验中』+ 倒计时」，
    以状态文本为锚，取其周围（任意方向，不假定在左侧）的纯数字行。
    """
    import numpy as np
    rows, _ = default_engine()(np.asarray(shot))
    best = -1.0
    for _, text, score in rows or []:
        m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", text)
        if m and float(score) >= 0.6:
            best = max(best, float(m.group(1)))
    if best >= 0:
        return best
    anchor = None
    for box, text, score in rows or []:
        t = re.sub(r"\s+", "", str(text))
        if any(a in t for a in ("下载中", "校验中", "更新中")) and float(score) >= 0.6:
            anchor = box
            break
    if anchor is None:
        return -1.0
    ax = sum(p[0] for p in anchor) / 4
    ay = sum(p[1] for p in anchor) / 4
    for box, text, score in rows or []:
        t = re.sub(r"\s+", "", str(text))
        if not re.fullmatch(r"\d{1,3}", t) or float(score) < 0.6:
            continue
        v = int(t)
        if v > 100:
            continue
        bx = sum(p[0] for p in box) / 4
        by = sum(p[1] for p in box) / 4
        if abs(by - ay) < 160 and abs(bx - ax) < 400:   # 锚点周围任意方向的纯数字
            best = max(best, float(v))
    return best


def read_version(shot) -> str:
    """OCR 读界面上的版本号。两段式（VERSION 7.0）到四段式都接受——
    实测米哈游启动器只显示 x.y，旧的 x.y.z 强制要求会导致校验恒失败。"""
    import numpy as np
    rows, _ = default_engine()(np.asarray(shot))
    for _, text, score in rows or []:
        m = re.search(r"\b(\d+\.\d+(?:\.\d+){0,2})\b", text)
        if m and float(score) > 0.5:
            return m.group(1)
    return ""
