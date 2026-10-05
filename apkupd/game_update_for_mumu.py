#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手游官网 APK 下载器；路径及通知统一使用 game-update.yaml。"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import time
from urllib.parse import urljoin
from pathlib import Path

import requests

# ---------------------------------------------------------------- 基础配置

BASE_DIR = Path(__file__).resolve().parent

PC_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
ANDROID_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36")

TIMEOUT = 30


# ---------------------------------------------------------------- 工具函数

def http_get(url, ua=PC_UA, **kw):
    kw.setdefault("timeout", TIMEOUT)
    kw.setdefault("headers", {})
    kw["headers"].setdefault("User-Agent", ua)
    r = requests.get(url, **kw)
    r.raise_for_status()
    return r


def http_head(url, ua=PC_UA, **kw):
    kw.setdefault("timeout", TIMEOUT)
    kw.setdefault("headers", {})
    kw["headers"].setdefault("User-Agent", ua)
    r = requests.head(url, allow_redirects=True, **kw)
    return r


def extract_version(text):
    """从 URL/文件名里抠出版本号，抠不到就返回「未知版本」（判重靠指纹）。"""
    base = text.rsplit("/", 1)[-1]
    m = re.findall(r"(\d+\.\d+(?:\.\d+){0,2})", base)
    return m[0] if m else "未知版本"


# ---------------------------------------------------------------- 各游戏探测
# 每个 provider 返回 dict:
#   version     版本号（用于展示/对比）
#   fingerprint 唯一指纹（版本变化它一定变化，用于判重）
#   url         APK 最终直链
#   size/md5    可选，用于下载后校验

def probe_arknights():
    """明日方舟：官方永久直链，两次 302 到真实 CDN 包（中间层不支持 HEAD，用 GET 链式跟随）。"""
    url = "https://ak.hypergryph.com/downloads/android_lastest"
    for _ in range(5):  # 逐跳跟随，拿到最终 APK 地址
        r = requests.get(url, headers={"User-Agent": PC_UA},
                         allow_redirects=False, timeout=TIMEOUT, stream=True)
        r.close()
        loc = r.headers.get("Location")
        if r.status_code in (301, 302, 303, 307, 308) and loc:
            url = urljoin(url, loc)
            continue
        break
    assert url.endswith(".apk"), f"方舟直链异常: {url}"
    size = 0
    try:
        rh = http_head(url)
        size = int(rh.headers.get("Content-Length") or 0)
    except Exception:
        pass
    # 版本号在路径里：.../Android/77.0.0_xxxx/arknights-hg-2771.apk
    m = re.search(r"/Android/([\d.]+)_", url)
    ver = m.group(1) if m else extract_version(url)
    return {"version": ver, "fingerprint": url, "url": url, "size": size}


def probe_bh3():
    """保留的 APK 接口；常规任务和命令行不调用。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "legacy_apk_sources", BASE_DIR / "mumuapkupdate" / "game_updater.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.probe_bh3()


def probe_bluearchive():
    """蔚蓝档案国服：抓官网首页 → 找到主 JS → 从 JS 里提取 pkg.bluearchive-cn.com 直链。"""
    html = http_get("https://bluearchive-cn.com/").text
    js_list = re.findall(r'src="(https://webcnstatic\.yostar\.net/[^"]+\.js)"', html)
    assert js_list, "蔚蓝档案：官网首页未找到 JS 资源"
    for js_url in js_list:
        js = http_get(js_url).text
        m = re.findall(r"https://pkg\.bluearchive-cn\.com/[^\"'\\\s<>]+?\.apk", js)
        if m:
            return {"version": extract_version(m[0]), "fingerprint": m[0], "url": m[0]}
    raise RuntimeError("蔚蓝档案：JS 中未找到 APK 直链")


def probe_reverse1999():
    """重返未来:1999：官网 version-page 接口返回当期官方 APK 直链（随版本自动更新）。

    注意：d.bluepoch.com 已接入腾讯 EdgeOne WAF，按 TLS 指纹拦截 Python requests
    （返回 567 拦截页），所以这里只负责探测直链，下载必须走 curl（见 download()）。
    """
    js = http_get("https://re.bluepoch.com/assets/js/api.js").text
    # api.js 里写死了当期版本页标识，如 pageVersion: "3.9"，动态抠出来避免硬编码过期
    m = re.search(r'pageVersion:\s*"([\d.]+)"', js)
    assert m, "1999：api.js 中未找到 pageVersion"
    try:
        r = requests.post(
            "https://re.bluepoch.com/activity/official/websites/version-page",
            json={"gameId": 50001, "pageVersion": m.group(1)},
            headers={"User-Agent": PC_UA,
                     "Referer": "https://re.bluepoch.com/",
                     "Content-Type": "application/json; charset=utf-8"},
            timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()["data"]
        url = d.get("androidDownloadUrl") or ""
        assert url.endswith(".apk"), f"1999：version-page 接口未返回 APK 直链: {url}"
        return stable_apk_info(url, d.get("pageVersion") or extract_version(url))
    except Exception as e:
        # 兜底：api.js 里写死的渠道直链（bdsem 投放链，可能滞后，仅作备用）
        m2 = re.findall(r"https://d\.bluepoch\.com/[^\"'\\\s<>]+?\.apk", js)
        assert m2, f"1999：version-page 接口与 api.js 均未找到直链（{e}）"
        return stable_apk_info(m2[0], extract_version(m2[0]))


def stable_apk_info(url, version):
    """A fixed URL can be overwritten: include HTTP content validators."""
    curl = shutil.which('curl')
    if curl:
        result = subprocess.run([curl, '-f', '-sS', '-I', '-L', '--max-time', '30',
                                 '-A', ANDROID_UA, url], capture_output=True, timeout=40)
        if result.returncode:
            raise RuntimeError('无法校验固定 APK 链接的远端版本')
        headers = {}
        for line in result.stdout.decode('iso-8859-1').splitlines():
            if line.startswith('HTTP/'):
                headers = {}
            elif ':' in line:
                key, value = line.split(':', 1)
                headers[key.lower()] = value.strip()
    else:
        with http_head(url) as response:
            response.raise_for_status()
            headers = {k.lower(): v for k, v in response.headers.items()}
    identity = [headers.get('etag', ''), headers.get('last-modified', ''), headers.get('content-length', '')]
    if not any(identity[:2]):
        raise RuntimeError('固定 APK 链接缺少 ETag/Last-Modified，不能确认缓存有效性')
    return {'version': version, 'fingerprint': url + '|' + '|'.join(identity),
            'url': url, 'size': int(headers.get('content-length') or 0)}


def probe_pvz2():
    """植物大战僵尸2（拓维官网 pvz2.hrgame.com.cn）：官网安卓下载按钮 → 302 → 官方 CDN 包。"""
    r = requests.get(
        "https://pvz2download.ditwan.cn/download-service/baokai",
        headers={"User-Agent": ANDROID_UA,
                 "Referer": "https://pvz2.hrgame.com.cn/"},
        allow_redirects=False, timeout=TIMEOUT)
    loc = r.headers.get("Location", "")
    assert loc.endswith(".apk"), f"PVZ2 官网下载跳转异常: HTTP {r.status_code} {loc}"
    return {"version": extract_version(loc), "fingerprint": loc, "url": loc}


def _probe_biligame(game_base_id, expect_pkg):
    """B站游戏中心统一接口：返回直链、版本、大小、MD5。"""
    r = http_get(
        f"https://line1-h5-pc-api.biligame.com/game/detail/gameinfo?game_base_id={game_base_id}")
    d = r.json()["data"]
    url = d.get("android_download_link") or ""
    assert url.endswith(".apk"), f"biligame {game_base_id} 无安卓直链"
    ver_in_name = extract_version(url)
    return {"version": ver_in_name, "fingerprint": url, "url": url,
            "size": int(d.get("android_pkg_size") or 0),
            "md5": (d.get("android_sign") or "").lower(),
            "pkg_name": d.get("android_pkg_name", expect_pkg)}


def probe_fgo():
    return _probe_biligame(49, "com.bilibili.fatego")


def probe_hbr():
    return _probe_biligame(110907, "com.bilibili.heaven")


# key -> (显示名, 探测函数, 下载时需带的 Referer)
# 注：B站 CDN（pkg.biligame.com）和拓维 CDN 会校验 Referer，必须带上对应官网域名
GAMES = {
    # "bh3": ("崩坏3", probe_bh3, ""),  # 已改用米哈游 PC 启动器
    "arknights":   ("明日方舟",        probe_arknights,   ""),
    "bluearchive": ("蔚蓝档案",        probe_bluearchive, ""),
    "reverse1999": ("重返未来1999",    probe_reverse1999, "https://re.bluepoch.com/"),
    "pvz2":        ("植物大战僵尸2",   probe_pvz2,        "https://pvz2.hrgame.com.cn/"),
    "fgo":         ("FGO",             probe_fgo,         "https://game.bilibili.com/fgo/"),
    "hbr":         ("炽焰天穹",        probe_hbr,         "https://game.bilibili.com/hbr/"),
}


# ---------------------------------------------------------------- 下载

def sha256_of(path, chunk=1 << 22):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def md5_of(path, chunk=1 << 22):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def download(url, dest: Path, expect_size=0, expect_md5="", referer=""):
    """优先 curl（断点续传+自动重试），其次 aria2c，最后 requests 断点续传。下完校验。

    curl 优先的原因：部分游戏 CDN（如 d.bluepoch.com）接入腾讯 EdgeOne WAF，
    按 TLS 指纹拦截 Python requests / aria2（返回 567 拦截页），curl 可正常通过。
    Windows 10+ 自带 curl.exe，无需额外安装。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    curl = shutil.which("curl")
    if curl:
        # -C - 自动断点续传；-f 让 HTTP 错误（如 567）返回非零退出码而不是写入错误页
        cmd = [curl, "-L", "-f", "-C", "-",
               "--connect-timeout", "30",
               "-A", ANDROID_UA, "-o", str(dest)]
        if referer:
            cmd += ["-e", referer]
        cmd.append(url)
        print(f"[下载] curl 断点续传 -> {dest}")
        # --retry-all-errors 需要 curl >= 7.71，老版本（Win10 早期自带 7.55）不认识，
        # 退出码 2 时去掉该参数重跑
        for extra in (["--retry", "10", "--retry-delay", "5", "--retry-all-errors"],
                      ["--retry", "10", "--retry-delay", "5"]):
            r = subprocess.run(cmd[:1] + extra + cmd[1:])
            if r.returncode != 2 or extra[-1] != "--retry-all-errors":
                break
        if r.returncode != 0:
            raise RuntimeError(f"curl 下载失败，退出码 {r.returncode}")
    elif shutil.which("aria2c"):
        cmd = ["aria2c", "-x", "16", "-s", "16", "-c", "--retry-wait=5",
               "--auto-file-renaming=false",
               "--summary-interval=1", "--show-console-readout=true",
               "--max-tries=10", "--file-allocation=none",
               "-U", ANDROID_UA, "-d", str(dest.parent), "-o", dest.name, url]
        if referer:
            cmd += ["--header", f"Referer: {referer}"]
        print(f"[下载] aria2c -> {dest}")
        r = subprocess.run(cmd)
        if r.returncode != 0:
            raise RuntimeError(f"aria2c 下载失败，退出码 {r.returncode}")
    else:
        print(f"[下载] requests 断点续传 -> {dest}")
        headers = {"User-Agent": ANDROID_UA}
        if referer:
            headers["Referer"] = referer
        for attempt in range(10):
            pos = dest.stat().st_size if dest.exists() else 0
            h = dict(headers)
            if pos:
                h["Range"] = f"bytes={pos}-"
            try:
                with requests.get(url, headers=h, stream=True, timeout=60) as r:
                    if r.status_code == 416 and pos:
                        total = re.fullmatch(r'bytes \*/(\d+)', r.headers.get('Content-Range', ''))
                        if total and int(total[1]) == pos:
                            break
                        raise RuntimeError('服务器拒绝续传，文件长度不匹配')
                    if pos and r.status_code == 200:      # 服务器不支持续传，重来
                        pos = 0
                    r.raise_for_status()
                    if r.status_code == 206:
                        bounds = re.match(r'bytes (\d+)-(\d+)/(\d+)', r.headers.get('Content-Range', ''))
                        if not bounds or int(bounds[1]) != pos:
                            raise RuntimeError('续传 Content-Range 不匹配')
                    mode = "ab" if pos else "wb"
                    received = 0
                    total_bytes = int(bounds[3]) if r.status_code == 206 else int(r.headers.get('Content-Length') or expect_size or 0)
                    started = last_display = time.monotonic()
                    def show_progress():
                        done = pos + received
                        speed = received / max(time.monotonic() - started, .001)
                        percent = f'{done / total_bytes:.1%}' if total_bytes else '总大小未知'
                        eta = f'{max(0, total_bytes-done)/speed:.0f}s' if total_bytes and speed else '--'
                        total_label = f'{total_bytes / 1048576:.1f}' if total_bytes else '?'
                        print(f'[进度] {dest.name}: {percent} | {done/1048576:.1f}/{total_label} MiB | {speed/1048576:.2f} MiB/s | 剩余 {eta}', flush=True)
                    show_progress()
                    with open(dest, mode) as f:
                        for chunk in r.iter_content(1 << 20):
                            f.write(chunk)
                            received += len(chunk)
                            if time.monotonic() - last_display >= 1:
                                show_progress()
                                last_display = time.monotonic()
                    show_progress()
                    length = int(r.headers.get('Content-Length') or 0)
                    if length and received != length:
                        raise RuntimeError('响应正文长度不完整')
                break
            except Exception as e:
                print(f"[下载] 中断（第 {attempt + 1} 次）: {e}，5 秒后续传")
                time.sleep(5)
        else:
            raise RuntimeError("下载重试 10 次仍失败")

    # 校验
    real_size = dest.stat().st_size
    if expect_size and real_size != expect_size:
        dest.replace(dest.with_name(dest.name + '.invalid'))
        raise RuntimeError(f"大小校验失败: 期望 {expect_size} 实际 {real_size}")
    if expect_md5 and re.fullmatch(r'[0-9a-fA-F]{32}', expect_md5):
        real_md5 = md5_of(dest)
        if real_md5.lower() != expect_md5.lower():
            dest.replace(dest.with_name(dest.name + '.invalid'))
            raise RuntimeError(f"MD5 校验失败: 期望 {expect_md5} 实际 {real_md5}")
        print(f"[校验] MD5 OK ({real_md5})")
    print(f"[完成] {dest} ({real_size / 1024 / 1024:.1f} MB)")


# ---------------------------------------------------------------- 集成入口

def configured_paths(config):
    settings = config["mumu"]["apk"]
    def resolve(value):
        path = Path(value)
        return path if path.is_absolute() else BASE_DIR / path
    return resolve(settings["download_dir"]).resolve(), resolve(settings["state_file"]).resolve()


def ensure_apk(key, config, check_only=False, force=False):
    """Return this game's validated local APK, including cached downloads."""
    if key == 'bh3':
        raise ValueError('崩坏3 APK 仅保留接口，不执行下载或安装')
    name, probe, referer = GAMES[key]
    directory, state_path = configured_paths(config)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    info = probe()
    if check_only:
        return info  # Inspection never marks an APK as downloaded.
    fingerprint = hashlib.sha256(info["fingerprint"].encode()).hexdigest()[:20]
    dest = directory / key / (key + "_" + fingerprint + ".apk")
    cached = state.get(key, {})
    if not force and dest.is_file() and cached.get("fingerprint") == info["fingerprint"] and cached.get("sha256") == sha256_of(dest):
        return dest
    partial = dest.with_suffix(".apk.part")
    download(info["url"], partial, expect_size=info.get("size", 0),
             expect_md5=info.get("md5", ""), referer=referer)
    from apkutils2 import APK
    try:
        manifest = APK(str(partial)).get_manifest()
        if not manifest.get("@package"):
            raise RuntimeError("下载文件不是有效 APK")
    except Exception:
        partial.replace(partial.with_name(partial.name + '.invalid'))
        raise
    partial.replace(dest)
    state[key] = {"fingerprint": info["fingerprint"], "version": info["version"],
                  "file": str(dest), "sha256": sha256_of(dest)}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(state_path)
    return dest


def main():
    import yaml
    ap = argparse.ArgumentParser(description="手游官网 APK 下载")
    ap.add_argument("--config", type=Path, default=BASE_DIR / "game-update.yaml")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--game", choices=list(GAMES))
    args = ap.parse_args()
    config = yaml.safe_load(args.config.read_text(encoding='utf-8'))
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
    failures = []
    keys = [args.game] if args.game else [g["download_key"] for g in config["mumu"]["games"] if g.get("enabled", True) and g["download_key"] != "bh3"]
    for key in keys:
        try:
            result = ensure_apk(key, config, args.check, args.force)
            print(key, result.get("version") if args.check else str(result))
        except Exception as exc:
            failures.append((GAMES[key][0], str(exc)))
    for item, detail in failures:
        print(f"[失败] {item}: {detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    from run_lock import single_instance
    try:
        with single_instance(BASE_DIR / '.update.lock'):
            code = main()
    except RuntimeError as exc:
        print(str(exc))
        code = 1
    raise SystemExit(code)
