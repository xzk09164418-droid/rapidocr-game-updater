# -*- coding: utf-8 -*-
"""异环 NTE（完美世界/Hotta）。
只更新启动器：官网 yh.wanmei.com 级联解析 → YH_common_setup.exe → NSIS 静默安装。
游戏更新：委托官方启动器内置完成——更新完拉起 NTELauncher.exe 即可。
（其 Config.ini 明文：Patcher=NTEUpdate.exe，OneAppId=1289，通道参数清晰）"""
import os
import re
import subprocess

from ..core.transport import download
from .base import Adapter, UpdateInfo, register

OFFICIAL = "yh.wanmei.com"
EXE_RE = re.compile(r'https?://[^\s"\'<>\\]*YH[^\s"\'<>\\]*setup[^\s"\'<>\\]*\.exe', re.I)
VER_RE = re.compile(r"setup_([\d.]+)_")


@register
class NTE(Adapter):
    name = "nte"
    exe_name = "NTELauncher.exe"

    def check_launcher(self) -> UpdateInfo:
        """官网域名 → 级联解析拿到安装器直链（该站为传统服务端渲染，L4-lite 一次命中）。"""
        from ..discover.website import resolve
        r = resolve(OFFICIAL)
        urls = [c["url"] for c in r.get("直接文件(通过校验)", [])]
        # Only use validated binary candidates, never an unresolved HTML entry.
        exe = next((u for u in urls                    # 主安装器：排除云游戏客户端
                    if "setup" in u.lower() and "cloud" not in u.lower()), None)
        if not exe:
            raise RuntimeError(f"官网解析未拿到安装器: {r}")
        m = VER_RE.search(exe)
        return UpdateInfo(version=m.group(1) if m else "unknown",
                          url=exe, source="yh.wanmei.com 官网解析")

    def update_launcher(self, install_dir: str = "", local_version: str = ""):
        info = self.check_launcher()
        print(f"[nte] 官网安装器 {info.version}: {info.url}")
        pkg = download(info.url, "./_cache/nte_setup.exe")
        if os.name == "nt":
            # NSIS 静默安装（/S）；官方安装器默认覆盖升级
            subprocess.run([pkg, "/S"], check=False)
            launcher = os.path.join(install_dir, self.exe_name)
            if os.path.exists(launcher):
                subprocess.Popen([launcher], cwd=install_dir)
                print("[nte] 启动器已更新并拉起，游戏更新由官方启动器内置完成")
        else:
            print("[nte] 非 Windows 环境：安装器已下载，请在 Windows 上执行（NSIS 支持 /S 静默）")

    def plan_game(self, install_dir: str = "") -> dict:
        return {"说明": "异环游戏更新委托官方启动器内置（NTEUpdate.exe）",
                "操作": "update_launcher 完成后自动拉起 NTELauncher.exe"}

    def update_game(self, install_dir: str):
        launcher = os.path.join(install_dir, self.exe_name)
        if os.name == "nt" and os.path.exists(launcher):
            subprocess.Popen([launcher], cwd=install_dir)
            print("[nte] 已拉起官方启动器，游戏更新由其内置完成")
        else:
            print("[nte] 未找到启动器，请先 update_launcher")
