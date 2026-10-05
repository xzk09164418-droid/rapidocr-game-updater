"""Full-image OCR followed by overlapping, row-major multiscale crops."""
import logging
import math
import time

import numpy as np
from PIL import Image


class ScanRows(list):
    exhausted = False


def regions(width, height, grid, overlap):
    if not 0 <= overlap < 1:
        raise ValueError('OCR overlap 必须在 [0, 1) 内')
    def positions(length, size):
        stride = max(1, math.floor(size * (1 - overlap) + 1e-9))
        offsets = list(range(0, length-size+1, stride))
        if offsets[-1] != length-size:
            offsets.append(length-size)
        return offsets
    w, h = math.ceil(width/grid), math.ceil(height/grid)
    for top in positions(height, h):
        for left in positions(width, w):
            yield left, top, left+w, top+h


def scan(shot, engine, matches, settings, preprocess=None):
    """Full-image OCR followed by overlapping, row-major multiscale crops.

    preprocess：可选的识别行预处理（如 ocr.merge_rows 拆框合并），
    对整图结果与每个滑窗 crop 的结果都生效，且先于 matches 判断。"""
    start = time.monotonic()
    budget = settings.get('max_seconds', 15)
    rows, _ = engine(np.asarray(shot))
    if preprocess:
        rows = preprocess(list(rows or []))
    collected = ScanRows((box, text, float(score)) for box, text, score in rows or [])
    if any(matches(r) for r in collected):
        return collected
    for grid in settings.get('grids', [2, 3, 4, 5, 6]):
        if grid not in (2, 3, 4, 5, 6):
            raise ValueError('OCR grids 仅支持 2 至 6')
        logging.info('滑窗 OCR 开始 %sx%s 层，重叠 %.0f%%', grid, grid, settings.get('overlap', .8)*100)
        for left, top, right, bottom in regions(*shot.size, grid, settings.get('overlap', .8)):
            # Budget is checked between inference calls; one inference may overrun.
            if budget is not None and time.monotonic() - start >= budget:
                logging.info('滑窗 OCR 达到本轮时间预算')
                return collected
            crop = shot.crop((left, top, right, bottom))
            scale = min(2., max(1., settings.get('min_crop_width', 800) / crop.width))
            resized = crop.resize((round(crop.width*scale), round(crop.height*scale)), Image.Resampling.LANCZOS)
            sx, sy = resized.width/crop.width, resized.height/crop.height
            detected, _ = engine(np.asarray(resized))
            mapped = [([[float(x)/sx+left, float(y)/sy+top] for x, y in box], text, float(score))
                      for box, text, score in detected or []]
            if preprocess:
                mapped = preprocess(mapped)
            collected.extend(mapped)
            if any(matches(r) for r in mapped):
                logging.info('滑窗 OCR 匹配成功：%sx%s 区域 (%s,%s,%s,%s)', grid, grid, left, top, right, bottom)
                return collected
    collected.exhausted = True
    logging.info('滑窗 OCR 已扫描全部层与最后一个区域，未匹配')
    return collected
