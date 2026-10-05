# -*- coding: utf-8 -*-
"""图像模板匹配通道（cv2，参考 March7thAssistant 的 matchTemplate + 相似度阈值）。"""


def match_template(shot, template_path: str, threshold: float = 0.85):
    """在截图(PIL)中找模板图，返回 ((x1,y1,x2,y2), score)；找不到返回 None。"""
    try:
        import cv2
        import numpy as np
    except ImportError:
        raise RuntimeError("模板匹配需 pip install opencv-python")
    img = cv2.cvtColor(np.asarray(shot), cv2.COLOR_RGB2BGR)
    tpl = cv2.imread(template_path)
    if tpl is None:
        raise RuntimeError(f"模板图不存在: {template_path}")
    res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    if score < threshold:
        return None
    h, w = tpl.shape[:2]
    return (loc[0], loc[1], loc[0] + w, loc[1] + h), float(score)
