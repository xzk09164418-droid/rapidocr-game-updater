# -*- coding: utf-8 -*-
"""鹰角（明日方舟/终末地，Windows）。
启动器：api/launcher/get_latest → 签名 zip（主源），HG-Link 社区镜像（备源）。
游戏：batch_proxy get_latest_game → patch(hdiff) 优先，否则 packs 分卷整包。"""
import os
import zipfile

from ..core import transport, staging
from ..core.transport import get_json, post_json, download
from .base import Adapter, UpdateInfo, register

LATEST = ("https://launcher.hypergryph.com/api/launcher/get_latest"
          "?appcode=abYeZZ16BPluCFyT&channel=1")
MIRROR = ("https://raw.githubusercontent.com/ERSTT/HG-Link/main/"
          "links/HypergryphLauncher.json")
BATCH = "https://launcher.hypergryph.com/api/proxy/batch_proxy"
GAMES = {"endfield": "6LL0KJuqHBVz33WK", "arknights": "GzD1CpaWgmSq1wew"}


@register
class Hypergryph(Adapter):
    name = "hypergryph"
    exe_name = "Launcher.exe"

    def check_launcher(self) -> UpdateInfo:
        errors = []
        for source, url in (("鹰角官方", LATEST), ("HG-Link镜像", MIRROR)):
            try:
                d = get_json(url)
                return UpdateInfo(version=str(d["version"]),
                                  url=d["zip_package_url"], md5=d.get("md5", ""),
                                  size=int(d.get("package_size") or 0),
                                  force=(d.get("action") == 2), source=source)
            except Exception as e:
                errors.append(f"[{source}] {e}")
        raise RuntimeError("所有更新源均不可用:\n  " + "\n  ".join(errors))

    def _game_latest(self, game="endfield"):
        rsp = post_json(BATCH, {"proxy_reqs": [{"kind": "get_latest_game",
                        "get_latest_game_req": {"appcode": GAMES[game],
                        "channel": "1", "sub_channel": "1", "version": "",
                        "launcher_appcode": ""}}]})
        return rsp["proxy_rsps"][0]["get_latest_game_rsp"]

    def plan_game(self, install_dir: str = "", game="endfield") -> dict:
        g = self._game_latest(game)
        packs = g["pkg"]["packs"]
        return {"版本": g["client_version"], "分卷数": len(packs),
                "总大小GB": round(sum(int(p["package_size"]) for p in packs) / 1e9, 2),
                "增量包": "有（hdiff）" if g.get("patch") else "无，走全量分卷",
                "整树指纹": g["pkg"].get("game_files_md5")}

    def update_game(self, install_dir: str, game="endfield"):
        g = self._game_latest(game)
        if g.get("patch"):
            # hdiff 增量：需要 hpatchz（可复用库洛/米哈游启动器目录内的 hpatchz.exe）
            raise NotImplementedError("hdiff 增量需 hpatchz，见 README 集成说明")
        stage = install_dir.rstrip(os.sep) + "_staging"
        for i, p in enumerate(g["pkg"]["packs"], 1):
            print(f"分卷 {i}/{len(g['pkg']['packs'])}")
            part = download(p["url"], f"./_cache/hg_{game}.zip.{i:03d}",
                            expected_md5=p["md5"])
        # 分卷 zip：全部到齐后按序拼接解包
        combined = f"./_cache/hg_{game}_combined.zip"
        with open(combined, "wb") as out:
            for i in range(1, len(g["pkg"]["packs"]) + 1):
                with open(f"./_cache/hg_{game}.zip.{i:03d}", "rb") as f:
                    out.write(f.read())
        with zipfile.ZipFile(combined) as z:
            z.extractall(stage)
        staging.apply_staging(stage, install_dir)
        import shutil
        shutil.rmtree(stage, ignore_errors=True)
        print(f"更新到 {g['client_version']} 完成")
