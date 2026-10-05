# -*- coding: utf-8 -*-
"""按钮边界检测与锚点重识别（OCR 文本几何层之外的物理验证层）。

与 ocr.py 的分工：ocr.py 回答「这段文字是不是关键词」，本模块回答
「这段文字周围有没有一个按钮」。纯文字几何无法区分「按钮里的更新」
和「公告长句『游戏更新公告』里的更新」，按钮边界可以。

  - find_boundary：以文字框为锚检测按钮完整边界（上下左右四边），
    供 ocr.py 的带内边界拼接层做碎片归组（边界内 = 同一按钮），
    以及 recover_by_anchor 做边界内重识别。只扫描按钮中段横条，
    不接触圆角区域——矩形/胶囊/任意圆角矩形在垂直中线处的横截面
    完全相同，天然免疫圆角半径不定。
  - recover_by_anchor：OCR 只检出部分文字（碎片是关键词真子串，如
    只检出「新」）时，以碎片为锚检测边界，裁剪边界内部放大重 OCR，
    把内部全部碎片按 x 序拼接后过词典。治整图尺度下的检测漏字；
    书法字错读、淡色字像素信息缺失仍然治不了（实测）。

边缘判定用「均值轮廓 + 持续电平跳变 + 跨列一致性」三重证据：
  - 检测框比字形大一圈（实测 det 框会越出按钮边），边缘搜索范围向
    框内各收 0.35×字高；
  - 对文字跨度整段做列平均得到灰度轮廓再取梯度——单像素噪声在平均
    中抵消，真边缘保留（实测噪声背景的逐列 argmax 会骗过一致性检查，
    2026-09-24 合成干扰行误命中事故）；
  - 边缘两侧各 0.2×字高的区域均值必须有持续电平差（≥15）——噪声和
    孤立线条没有电平跳变，按钮内填充与外部背景有。

毛玻璃注意：半透明填充透出的模糊背景会破坏内部均匀性，但「模糊 +
  提亮/压暗」本身构成均值电平跳变，边界仍可检出；内部重识别走原色
  放大，不做二值化（玻璃透射团块二值化后变伪笔画，rec 模型容忍花
  背景的能力远强于容忍人造噪声——实测「新」花背景原图 1.00，
  Canny 边缘图上掉到 0.59）。
"""
import logging
import re

log = logging.getLogger("button")

_PROFILE_MIN_GRAD = 8.0    # 列平均轮廓的最小梯度（平均已压噪，阈值相应放低）
_COLUMN_MIN_GRAD = 15.0    # 单列一致性检查的最小梯度
_LEVEL_MIN_SHIFT = 15.0    # 边缘两侧 0.2×字高区域的持续电平差下限
_EDGE_COLS = 5             # 一致性检查的采样列数
_EDGE_MIN_COLS = 3         # 至少多少列在边缘位置 ±2px 内检出梯度
_CONTAINER_MIN = 0.9       # 容器高 / 字高 下限（det 框可能高过按钮本体）
_CONTAINER_MAX = 3.5       # 容器高 / 字高 上限（更高的面板/卡片不是按钮）
_SYMMETRY = 0.6            # 上下留白差 ≤ 0.6×字高（按钮文字垂直居中）


def _gray(shot_img):
    """PIL 图像或 np 数组 -> float 灰度图。"""
    import numpy as np
    arr = np.asarray(shot_img)
    if arr.ndim == 2:
        return arr.astype(float)
    import cv2
    return cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY).astype(float)


def _bounds(box):
    xs = [float(p[0]) for p in box]
    ys = [float(p[1]) for p in box]
    return min(xs), max(xs), min(ys), max(ys)


def _find_edge(gray, profile, lo, hi, pad, cols, axis, prefer):
    """在 [lo,hi] 内找一条真边缘：轮廓梯度 + 电平跳变 + 跨列一致。

    按钮边缘是离文字框最近的真边缘，但搜索范围内可能有强度更高的
    背景纹理边缘（实测：按钮上方原画的樱花边缘梯度强于胶囊边缘，
    全局 argmax 会抢跑）。故枚举所有梯度达标的局部极大，按到
    prefer（文字框边）的距离升序逐个验证，首个通过三重证据的胜出。
    axis=0 表示水平边缘（纵向扫描，cols 为采样列）；axis=1 表示垂直
    边缘（横向扫描，cols 为采样行）。返回边缘坐标（float）或 None。
    """
    import numpy as np
    if hi - lo < 3:
        return None
    grad = np.abs(np.diff(profile))
    seg = grad[lo:hi]
    cand = [lo + i for i in range(1, len(seg) - 1)
            if seg[i] >= _PROFILE_MIN_GRAD
            and seg[i] >= seg[i - 1] and seg[i] >= seg[i + 1]]
    cand.sort(key=lambda p: abs(p - prefer))
    colgrad = np.abs(np.diff(gray, axis=axis))
    for pos in cand:
        # 持续电平跳变：边缘两侧各 pad 像素的均值差
        a_lo, a_hi = max(0, pos + 1 - pad), pos + 1
        b_lo, b_hi = pos + 1, min(len(profile), pos + 1 + pad)
        if a_hi - a_lo < 2 or b_hi - b_lo < 2:
            continue
        if abs(float(profile[a_lo:a_hi].mean())
               - float(profile[b_lo:b_hi].mean())) < _LEVEL_MIN_SHIFT:
            continue
        # 跨列一致性：多数采样列在该位置附近也有梯度（防噪声尖峰）。
        # 窗口随 pad 放宽：采样列已只取中段，残留位置偏差在像素级
        win = max(3, pad // 4)
        cnt = 0
        for c in cols:
            if axis == 0:               # 水平边缘：固定列 c，沿 y 取梯度
                segc = colgrad[max(0, pos - win):pos + win + 1, int(c)]
            else:                       # 垂直边缘：固定行 c，沿 x 取梯度
                segc = colgrad[int(c), max(0, pos - win):pos + win + 1]
            if segc.size and float(segc.max()) >= _COLUMN_MIN_GRAD:
                cnt += 1
        if cnt >= _EDGE_MIN_COLS:
            return float(pos + 1)
    return None


def _top_bottom(gray, x0, x1, y0, y1, h):
    """找按钮顶/底边（文字跨度内列平均轮廓）。返回 (top, bot) 或 None。"""
    import numpy as np
    H, _ = gray.shape
    cx0 = int(x0 + 0.05 * (x1 - x0))
    cx1 = int(x1 - 0.05 * (x1 - x0))
    profile = gray[:, cx0:cx1 + 1].mean(axis=1)
    pad = max(3, int(0.2 * h))
    # 一致性采样列只取中段 60%：胶囊/圆角矩形的角部边缘会上翘，
    # 两侧各 20% 的列在顶/底边上不可靠（圆角半径 ≤ 半高，中段必平）
    cols = np.linspace(cx0 + 0.2 * (cx1 - cx0), cx1 - 0.2 * (cx1 - cx0),
                       _EDGE_COLS)
    top = _find_edge(gray, profile, max(0, int(y0 - 1.5 * h)),
                     min(H - 1, int(y0 + 0.35 * h)), pad, cols, axis=0,
                     prefer=y0)
    if top is None:
        return None
    bot = _find_edge(gray, profile, max(0, int(y1 - 0.35 * h)),
                     min(H - 1, int(y1 + 1.5 * h)), pad, cols, axis=0,
                     prefer=y1)
    if bot is None:
        return None
    return top, bot


def find_boundary(shot_img, box):
    """以锚点文字框为中心找按钮完整边界 (l, t, r, b)；找不到返回 None。

    顶/底边同 verify；左/右边在文字垂直中线 ±0.3×字高的行带上做行
    平均轮廓（中线处圆角矩形的侧边是直线，圆角半径不影响）。
    """
    import numpy as np
    x0, x1, y0, y1 = _bounds(box)
    h = max(1.0, y1 - y0)
    gray = _gray(shot_img)
    edges = _top_bottom(gray, x0, x1, y0, y1, h)
    if edges is None:
        return None
    top, bot = edges
    if not (_CONTAINER_MIN * h <= bot - top <= _CONTAINER_MAX * h):
        return None
    H, W = gray.shape
    cy = (y0 + y1) / 2
    ry0 = int(max(0, cy - 0.3 * h))
    ry1 = int(min(H - 1, cy + 0.3 * h))
    profile = gray[ry0:ry1 + 1, :].mean(axis=0)
    pad = max(3, int(0.2 * h))
    rows = np.linspace(ry0, ry1, _EDGE_COLS)
    left = _find_edge(gray, profile, max(0, int(x0 - 3 * h)),
                      min(W - 1, int(x0 + 0.3 * h)), pad, rows, axis=1,
                      prefer=x0)
    if left is None:
        return None
    right = _find_edge(gray, profile, max(0, int(x1 - 0.3 * h)),
                       min(W - 1, int(x1 + 3 * h)), pad, rows, axis=1,
                       prefer=x1)
    if right is None:
        return None
    if right - left < (bot - top):      # 按钮必然宽大于高
        return None
    return (left, top, right, bot)


def _recog_interior(shot, engine, boundary, match_text, min_score):
    """裁剪边界内部 → 放大重 OCR → 内部碎片连续窗口拼接 → 词典判定。

    走原色放大，不做二值化（玻璃/纹理内部二值化会产生伪笔画）。
    match_text 为 ocr.find_text 的纯文本判定闭包（长度/排除词/词表
    规则与主路径一致）。返回的 box 统一为四点框（边界框转换），
    点击中心即按钮中心。
    """
    import numpy as np
    from PIL import Image
    l, t, r, b = (int(round(v)) for v in boundary)
    inset = 2                           # 内缩避开边缘渐变带
    crop = shot.crop((l + inset, t + inset, max(l + inset + 1, r - inset),
                      max(t + inset + 1, b - inset)))
    if crop.width < 8 or crop.height < 8:
        return None
    scale = min(3.0, max(1.0, 800 / crop.width))
    if scale > 1.0:
        crop = crop.resize((round(crop.width * scale),
                            round(crop.height * scale)),
                           Image.Resampling.LANCZOS)
    rows, _ = engine(np.asarray(crop))
    frags = []
    for box2, text, score in rows or []:
        tt = re.sub(r"\s+", "", str(text))
        if tt and float(score) >= min_score:
            xs = [float(p[0]) for p in box2]
            frags.append((min(xs), tt, float(score)))
    frags.sort()
    n = len(frags)
    for a in range(n):
        text = ""
        for b in range(a, n):
            text += frags[b][1]
            if match_text(text):
                log.info("边界内重识别命中: %r（碎片 %s）",
                         text, [f[1] for f in frags[a:b + 1]])
                lf, tf, rf, bf = (float(v) for v in boundary)
                return ([[lf, tf], [rf, tf], [rf, bf], [lf, bf]],
                        text, min(f[2] for f in frags[a:b + 1]))
    return None


def recover_by_anchor(shot, engine, rows, keywords, match_text,
                      min_score=0.6, max_anchors=3):
    """部分命中锚点 → 边缘定边界 → 边界内重识别。返回 (box, text, score)。

    锚点规则：去空白后是某关键词的真子串（「新」⊂「更新」），置信度
    ≥min_score；按置信度降序最多尝试 max_anchors 个。命中返回的 box 为
    按钮边界四点框——点击中心比文字框更准。
    """
    anchors = []
    for box, text, score in rows or []:
        t = re.sub(r"\s+", "", str(text))
        if not t or float(score) < min_score:
            continue
        if any(t != k and t in k for k in keywords):
            anchors.append((box, t, float(score)))
    anchors.sort(key=lambda r: -r[2])
    for box, t, score in anchors[:max_anchors]:
        boundary = find_boundary(shot, box)
        if boundary is None:
            log.info("锚点 %r 周围未检出按钮边界", t)
            continue
        log.info("锚点 %r 检出按钮边界 %s", t,
                 tuple(int(round(v)) for v in boundary))
        hit = _recog_interior(shot, engine, boundary, match_text, min_score)
        if hit:
            return hit
    return None
