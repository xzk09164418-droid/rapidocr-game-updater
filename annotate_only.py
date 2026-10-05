# -*- coding: utf-8 -*-
"""离线标注补跑：对 test_genshin_preupdate.py 已完成的运行目录重新生成标注。

用法：python annotate_only.py <run_dir>
与主程序探针完全解耦，只读 events.jsonl + frames_raw + ocr_raw，
输出 frames_annotated / ocr_annotated / summary.md。单帧出错跳过并打印。
"""
import json
import sys
import time
import traceback
from pathlib import Path

RUN_DIR = Path(sys.argv[1]).resolve()
FONT_PATH = r"C:\Windows\Fonts\msyh.ttc"

RED = (255, 56, 56)
GREEN = (0, 200, 70)
BLUE = (70, 130, 255)
YELLOW = (255, 200, 0)


def _font(size):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except Exception:
        return ImageFont.load_default()


def _box_xy(box):
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    return min(xs), min(ys), max(xs), max(ys)


def _same_row(a, b):
    return a and b and a[1] == b[1] and abs(a[2] - b[2]) < 1e-6


def annotate(events):
    from PIL import Image, ImageDraw

    frames = [e for e in events if e["kind"] == "frame"]
    infers = [e for e in events if e["kind"] == "ocr_infer"]
    finds = [e for e in events if e["kind"] == "find_text"]
    moves = [e for e in events if e["kind"] == "mouse_move"]
    clicks = [e for e in events if e["kind"] == "click"]

    decisions = []
    for f in finds:
        rows, img, rect = [], None, None
        for inf in infers:
            if f["seq_start"] < inf["seq"] <= f["seq_end"]:
                rows.extend(inf["rows"])
                if inf.get("img") and img is None:
                    img, rect = inf["img"], inf.get("rect")
        kept = f.get("kept")
        rejected = [r for r in rows if not _same_row(r, kept)]
        decisions.append({"ts": f["ts"] - f.get("dur", 0), "ts_end": f["ts"],
                          "img": img, "rect": rect, "kept": kept,
                          "rejected": rejected, "keywords": f["keywords"],
                          "sliding": f["sliding"], "dur": f.get("dur", 0)})
        if img and (RUN_DIR / img).exists():
            try:
                im = Image.open(RUN_DIR / img).convert("RGB")
                dr = ImageDraw.Draw(im, "RGBA")
                font = _font(max(18, im.width // 60))
                for box, text, score in rejected:
                    x1, y1, x2, y2 = _box_xy(box)
                    dr.rectangle([x1, y1, x2, y2], outline=GREEN, width=2)
                if kept:
                    x1, y1, x2, y2 = _box_xy(kept[0])
                    dr.rectangle([x1 - 2, y1 - 2, x2 + 2, y2 + 2], outline=RED, width=4)
                    dr.rectangle([x1, y1 - font.size - 8,
                                  x1 + font.size * 12, y1], fill=(255, 56, 56, 180))
                    dr.text((x1 + 4, y1 - font.size - 6),
                            f"采用: {kept[1]} {kept[2]:.2f}", font=font,
                            fill=(255, 255, 255))
                head = (f"keywords={f['keywords']}  sliding={f['sliding']}  "
                        f"识别{len(rows)}行 过滤{len(rejected)}行  "
                        f"{'采用「' + kept[1] + '」' if kept else '未命中'}")
                dr.rectangle([0, 0, im.width, font.size + 14], fill=(0, 0, 0, 160))
                dr.text((8, 6), head, font=font, fill=YELLOW)
                im.save(RUN_DIR / f"ocr_annotated/d{f['seq']:05d}.png")
            except Exception:
                traceback.print_exc()

    font = _font(20)
    small = _font(15)
    n_ok = 0
    for i, fr in enumerate(frames):
        try:
            t, rect = fr["ts"], fr["rect"]
            path = RUN_DIR / fr["img"]
            if not path.exists():
                continue
            im = Image.open(path).convert("RGB")
            W, H = im.size
            dr = ImageDraw.Draw(im, "RGBA")
            ox, oy = rect[0], rect[1]
            # 帧可能按 FRAME_MAXW 缩放保存：屏幕/截图坐标需乘比例落到帧像素
            sx = W / max(1, rect[2] - rect[0])
            sy = H / max(1, rect[3] - rect[1])

            dec = None
            for d in decisions:
                if d["ts_end"] <= t + 0.5:
                    dec = d
            if dec and t - dec["ts_end"] <= 6.0 and dec.get("rect"):
                dx = dec["rect"][0] - ox
                dy = dec["rect"][1] - oy
                for box, text, score in dec["rejected"]:
                    x1, y1, x2, y2 = _box_xy(box)
                    dr.rectangle([(x1 - dx) * sx, (y1 - dy) * sy,
                                  (x2 - dx) * sx, (y2 - dy) * sy],
                                 outline=GREEN, width=2)
                if dec["kept"]:
                    x1, y1, x2, y2 = _box_xy(dec["kept"][0])
                    dr.rectangle([(x1 - 2 - dx) * sx, (y1 - 2 - dy) * sy,
                                  (x2 + 2 - dx) * sx, (y2 + 2 - dy) * sy],
                                 outline=RED, width=4)
                    dr.text(((x1 - dx) * sx + 4, max(0, (y1 - dy) * sy - 24)),
                            f"{dec['kept'][1]} {dec['kept'][2]:.2f}",
                            font=small, fill=RED)

            for mv in moves:
                dur = mv.get("dur", 0.5)
                if mv["ts"] - 0.6 <= t <= mv["ts"] + dur + 0.6:
                    pts = [((a - ox) * sx, (b - oy) * sy) for a, b in mv["pts"]]
                    if len(pts) >= 2:
                        dr.line(pts, fill=GREEN, width=3)
                        dr.ellipse([pts[-1][0] - 5, pts[-1][1] - 5,
                                    pts[-1][0] + 5, pts[-1][1] + 5],
                                   outline=GREEN, width=2)

            # 点击落点（蓝）：点击前后各 1s 停顿会拉长时间差，窗口放宽到 ±3s
            for ck in clicks:
                if abs(ck.get("ts_click", ck["ts"]) - t) <= 3.0:
                    cx, cy = (ck["x"] - ox) * sx, (ck["y"] - oy) * sy
                    r = max(9, round(16 * sx))
                    dr.ellipse([cx - r, cy - r, cx + r, cy + r], outline=BLUE, width=4)
                    dr.line([cx - r - 6, cy, cx + r + 6, cy], fill=BLUE, width=2)
                    dr.line([cx, cy - r - 6, cx, cy + r + 6], fill=BLUE, width=2)
                    dr.text((cx + r + 4, cy - 12), f"CLICK #{ck.get('n', '?')}",
                            font=small, fill=BLUE)

            age = f"  OCR框龄{t - dec['ts_end']:.1f}s" if dec and t - dec["ts_end"] <= 6.0 else ""
            dr.rectangle([0, 0, W, 30], fill=(0, 0, 0, 150))
            dr.text((8, 4),
                    f"f{fr['idx']:05d} " +
                    time.strftime("%H:%M:%S", time.localtime(t)) +
                    f".{int((t % 1) * 10)}{age}  红=采用文字 绿=被过滤文字/鼠标轨迹 蓝=点击",
                    font=small, fill=(255, 255, 255))
            if W > 1440:
                im = im.resize((1440, round(H * 1440 / W)))
            im.save(RUN_DIR / "frames_annotated" / path.name, quality=88)
            n_ok += 1
            if n_ok % 100 == 0:
                print(f"frames_annotated {n_ok}/{len(frames)}", flush=True)
        except Exception:
            print(f"[跳过] 帧 {fr.get('idx')} 标注失败:", flush=True)
            traceback.print_exc()
    return decisions


def summarize(events, decisions):
    frames = [e for e in events if e["kind"] == "frame"]
    clicks = [e for e in events if e["kind"] == "click"]
    finds = [e for e in events if e["kind"] == "find_text"]
    scans = [e for e in events if e["kind"] == "sliding_scan"]
    infers = [e for e in events if e["kind"] == "ocr_infer"]
    outcome = next((e["text"] for e in events if e["kind"] == "outcome"), "未知")
    lines = [
        f"# 原神预更新可视化测试 {RUN_DIR.name}", "",
        f"- 结局：{outcome}",
        f"- 连续帧：{len(frames)} 张（0.5s/帧，约 {len(frames) * 0.5 / 60:.1f} 分钟）",
        f"- OCR 推理：{len(infers)} 次；find_text 决策：{len(finds)} 次；"
        f"滑窗扫描：{len(scans)} 次（耗尽 {sum(1 for s in scans if s.get('exhausted'))} 次）",
        f"- 点击：{len(clicks)} 次", "",
        "## 点击明细", "", "| # | 坐标 | 差异像素比 |", "|---|---|---|",
    ]
    for c in clicks:
        lines.append(f"| {c.get('n')} | ({c['x']},{c['y']}) | {c.get('diff_ratio')} |")
    lines += ["", "## find_text 命中明细", "", "| 时间 | 关键词 | 命中 | 耗时s |",
              "|---|---|---|---|"]
    for d in decisions:
        kept = f"{d['kept'][1]}({d['kept'][2]:.2f})" if d["kept"] else "—"
        lines.append(f"| {time.strftime('%H:%M:%S', time.localtime(d['ts_end']))} "
                     f"| {'/'.join(d['keywords'])} | {kept} | {d['dur']} |")
    (RUN_DIR / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    t0 = time.time()
    events = [json.loads(l) for l in
              (RUN_DIR / "events.jsonl").read_text(encoding="utf-8").splitlines()
              if l.strip()]
    print(f"事件 {len(events)} 条，开始标注…", flush=True)
    (RUN_DIR / "frames_annotated").mkdir(exist_ok=True)
    (RUN_DIR / "ocr_annotated").mkdir(exist_ok=True)
    decisions = annotate(events)
    summarize(events, decisions)
    print(f"标注完成，耗时 {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
