# -*- coding: utf-8 -*-
"""库洛·鸣潮（国服）。
启动器：launcher-config index.json → default.resource(launcher.zip) + cdnList 权重 + gray 灰度。
游戏：indexFile.json 逐文件 MD5 比对，只下差异；krpdiff 差分用包内 hpatchz.exe 应用。"""
import os

from ..core import transport, staging
from ..core.transport import get_json, download, md5_file
from .base import Adapter, UpdateInfo, register

INDEX = ("https://prod-cn-alicdn-gamestarter.kurogame.com/launcher/launcher/"
         "10003_Y8xXrXk65DqFHEDgApn3cpK5lfczpFx5/G152/index.json")
GAME_INDEX = ("https://prod-cn-alicdn-gamestarter.kurogame.com/launcher/game/"
              "G152/10003_Y8xXrXk65DqFHEDgApn3cpK5lfczpFx5/index.json")


@register
class Kuro(Adapter):
    name = "kuro"
    exe_name = "launcher.exe"

    def check_launcher(self) -> UpdateInfo:
        cfg = get_json(INDEX)["default"]
        res = cfg["resource"]
        cdn = cfg["cdnList"][0]["url"]
        # 灰度检查：resourcesGray.graySwitch=1 时可向 grayUrl 询问是否命中灰度
        return UpdateInfo(version=res["version"], url=cdn + res["path"],
                          md5=res["md5"], size=int(res["size"]), source="launcher-config")

    def _game_manifest(self):
        idx = get_json(GAME_INDEX)["default"]
        cdn = idx["cdnList"][0]["url"]
        files = get_json(cdn + idx["config"]["indexFile"])["resource"]
        return idx["config"]["version"], cdn, files

    def plan_game(self, install_dir: str = "") -> dict:
        ver, _, files = self._game_manifest()
        changed = missing = 0
        dl = 0
        for f in files:
            p = os.path.join(install_dir, f["dest"].replace("/", os.sep)) if install_dir else ""
            if not install_dir or not os.path.exists(p):
                missing += 1; dl += f["size"]
            elif md5_file(p) != f["md5"]:
                changed += 1; dl += f["size"]
        return {"版本": ver, "文件总数": len(files), "需新增": missing,
                "需更新": changed, "下载量GB": round(dl / 1e9, 2),
                "备选": "大版本差分包 krpdiff，用启动器目录 hpatchz.exe 应用"}

    def update_game(self, install_dir: str):
        ver, cdn, files = self._game_manifest()
        stage = install_dir.rstrip(os.sep) + "_staging"
        n = 0
        for f in files:
            p = os.path.join(install_dir, f["dest"].replace("/", os.sep))
            if os.path.exists(p) and md5_file(p) == f["md5"]:
                continue  # 断点续更：完好文件跳过
            download(cdn + f["dest"].replace("\\", "/"),
                     os.path.join(stage, f["dest"].replace("/", os.sep)),
                     expected_md5=f["md5"])
            n += 1
        staging.apply_staging(stage, install_dir)
        import shutil
        shutil.rmtree(stage, ignore_errors=True)
        print(f"鸣潮更新到 {ver}，共更新 {n} 个文件")
