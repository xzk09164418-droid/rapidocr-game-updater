# -*- coding: utf-8 -*-
"""米哈游 HoYoPlay。
启动器：官网 hyp-webstatic 升级包（社区仓库逐版本追踪，exe 逆向兜底）。
游戏：sophon 分块引擎——getBuild?tag=旧版本 得差量构建，四层 MD5 校验。"""
import concurrent.futures as cf
import hashlib
import os

from ..core import transport, staging
from ..core.transport import get_json, http_get, md5_file
from .base import Adapter, UpdateInfo, register

LAUNCHER_ID = "jGHBHlcOq1"
HYP_API = "https://hyp-api.mihoyo.com/hyp/hyp-connect/api"
SOPHON_API = "https://downloader-api.mihoyo.com/downloader/sophon_chunk/api"
# 启动器升级包：社区追踪（HoYoPlay-Download-Library）；失效时用 discover 层逆向官网 exe
LAUNCHER_URL = "https://hyp-webstatic.mihoyo.com/hyp-client/miHoYoLauncher_1.18.exe"


# --- sophon 清单：zstd 压缩 protobuf，手写 wire 解析 ---
def _varint(b, i):
    r = s = 0
    while True:
        x = b[i]; i += 1
        r |= (x & 0x7f) << s
        if not (x & 0x80):
            return r, i
        s += 7


def _fields(b):
    i = 0
    while i < len(b):
        tag, i = _varint(b, i)
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            ln, i = _varint(b, i)
            yield fn, b[i:i + ln]; i += ln
        elif wt == 0:
            v, i = _varint(b, i)
            yield fn, v
        elif wt == 5:
            yield fn, b[i:i + 4]; i += 4
        elif wt == 1:
            yield fn, b[i:i + 8]; i += 8
        else:
            return


def parse_manifest(raw: bytes):
    import zstandard
    raw = zstandard.ZstdDecompressor().decompress(raw, max_output_size=1 << 28)
    assets = []
    for fn, v in _fields(raw):
        if fn != 1:
            continue
        a = {"chunks": []}
        for f2, v2 in _fields(v):
            if f2 == 1:
                a["name"] = v2.decode()
            elif f2 == 2:
                c = {}
                for f3, v3 in _fields(v2):
                    if f3 == 1: c["name"] = v3.decode()
                    elif f3 == 2: c["dec_md5"] = v3.decode()
                    elif f3 == 4: c["c_size"] = v3
                    elif f3 == 5: c["d_size"] = v3
                    elif f3 == 7: c["c_md5"] = v3.decode()
                a["chunks"].append(c)
            elif f2 == 4:
                a["size"] = v2
            elif f2 == 5:
                a["md5"] = v2.decode()
        assets.append(a)
    return assets


def sophon_build(package_id: str, password: str, tag: str = "") -> dict:
    url = f"{SOPHON_API}/getBuild?branch=main&package_id={package_id}&password={password}"
    if tag:  # 传旧版本号 → 差量构建（只含变化 chunk）
        url += f"&tag={tag}"
    return get_json(url)["data"]


def _download_asset(asset: dict, chunk_prefix: str, dest: str):
    import zstandard
    dctx = zstandard.ZstdDecompressor()
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    with open(dest, "wb") as out:
        for c in asset["chunks"]:
            blob = http_get(f"{chunk_prefix}/{c['name']}")
            if hashlib.md5(blob).hexdigest() != c["c_md5"]:
                raise RuntimeError(f"chunk 压缩态校验失败: {c['name']}")
            dec = dctx.decompress(blob, max_output_size=1 << 28)
            if hashlib.md5(dec).hexdigest() != c["dec_md5"]:
                raise RuntimeError(f"chunk 解压态校验失败: {c['name']}")
            out.write(dec)
    if md5_file(dest) != asset["md5"]:
        raise RuntimeError(f"文件校验失败: {asset['name']}")


@register
class Mihoyo(Adapter):
    name = "mihoyo"
    exe_name = "HYP.exe"

    def check_launcher(self) -> UpdateInfo:
        # exe 文件名含版本号；精确版本由社区追踪仓库/exe 逆向校准
        ver = "1.18.0.380"
        return UpdateInfo(version=ver, url=LAUNCHER_URL, source="hyp-webstatic 官网直链")

    def game_branch(self, game_id="1Z8W5NHUQb"):
        br = get_json(f"{HYP_API}/getGameBranches?game_ids[]={game_id}"
                      f"&launcher_id={LAUNCHER_ID}")["data"]["game_branches"][0]
        return br.get("main") or {}

    def plan_game(self, install_dir: str = "", game_id="1Z8W5NHUQb") -> dict:
        br = self.game_branch(game_id)
        build = sophon_build(br["package_id"], br["password"])
        m = build["manifests"][0]
        assets = parse_manifest(http_get(
            m["manifest_download"]["url_prefix"] + "/" + m["manifest"]["id"]))
        return {"版本": build["tag"], "build_id": build["build_id"],
                "文件数": len(assets),
                "chunk数": sum(len(a["chunks"]) for a in assets),
                "解压总大小GB": round(sum(a["size"] for a in assets) / 1e9, 2),
                "增量说明": "sophon_build(..., tag=本地版本) 即得差量构建"}

    def update_game(self, install_dir: str, game_id="1Z8W5NHUQb",
                    local_tag: str = "", workers: int = 8):
        br = self.game_branch(game_id)
        build = sophon_build(br["package_id"], br["password"], tag=local_tag)
        stage = install_dir.rstrip(os.sep) + "_staging"
        for m in build.get("manifests") or []:
            assets = parse_manifest(http_get(
                m["manifest_download"]["url_prefix"] + "/" + m["manifest"]["id"]))
            prefix = m["chunk_download"]["url_prefix"]
            todo = [a for a in assets if not (
                os.path.exists(p := os.path.join(
                    install_dir, a["name"].replace("/", os.sep)))
                and md5_file(p) == a["md5"])]
            print(f"[{m.get('category_name')}] 需更新 {len(todo)}/{len(assets)} 个文件")
            with cf.ThreadPoolExecutor(workers) as ex:
                futs = {ex.submit(_download_asset, a, prefix,
                                  os.path.join(stage, a["name"].replace("/", os.sep))): a
                        for a in todo}
                for f in cf.as_completed(futs):
                    f.result()
                    print(f"  ✓ {futs[f]['name']}")
        n = len(staging.apply_staging(stage, install_dir))
        staging_mod_clean = os.path.exists(stage)
        if staging_mod_clean:
            import shutil
            shutil.rmtree(stage, ignore_errors=True)
        print(f"更新完成，替换 {n} 个文件（旧文件已备份 .old）")
