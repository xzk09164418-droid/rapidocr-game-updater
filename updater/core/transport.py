# -*- coding: utf-8 -*-
"""传输层：退避重试 + 断点续传 + MD5 校验门。vendor 无关。"""
import gzip
import hashlib
import json
import os
import time
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
CHUNK = 1 << 20


def http_get(url: str, retries: int = 3, timeout: int = 30) -> bytes:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
        except Exception as e:
            last = e
            time.sleep(2 ** i)
    raise RuntimeError(f"请求失败（{retries} 次重试后）: {url}\n  {last}")


def get_json(url: str) -> dict:
    return json.loads(http_get(url))


def post_json(url: str, payload: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={**UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def md5_file(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(CHUNK), b""):
            h.update(blk)
    return h.hexdigest()


def download(url: str, dest: str, expected_md5: str = "", retries: int = 3,
             progress: bool = True) -> str:
    """Range 断点续传；MD5 校验失败删文件重下（避免坏文件续传死循环）。"""
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    for attempt in range(retries):
        done = os.path.getsize(dest) if os.path.exists(dest) else 0
        try:
            req = urllib.request.Request(url, headers={**UA, "Range": f"bytes={done}-"})
            with urllib.request.urlopen(req, timeout=60) as r:
                total = int(r.headers.get("Content-Length") or 0) + done
                mode = "ab" if r.status == 206 and done else "wb"
                if mode == "wb":
                    done = 0
                with open(dest, mode) as f:
                    while True:
                        blk = r.read(CHUNK)
                        if not blk:
                            break
                        f.write(blk)
                        done += len(blk)
                        if progress:
                            print(f"\r  下载 {done/1e6:.1f}/{total/1e6:.1f} MB",
                                  end="", flush=True)
            if progress:
                print()
            if expected_md5 and md5_file(dest).lower() != expected_md5.lower():
                os.remove(dest)
                raise RuntimeError("MD5 校验失败，已删除损坏文件")
            return dest
        except Exception as e:
            print(f"\n  下载中断（第 {attempt+1} 次）: {e}")
            time.sleep(2 ** attempt)
    raise RuntimeError(f"下载失败: {url}")
