# -*- coding: utf-8 -*-
"""适配器基类与注册表。每个厂商实现：check_launcher / update_launcher /
plan_game / update_game（异环游戏更新委托给官方启动器）。"""
from dataclasses import dataclass


@dataclass
class UpdateInfo:
    version: str
    url: str
    md5: str = ""
    size: int = 0
    force: bool = False
    source: str = ""


class Adapter:
    name = ""
    exe_name = ""          # 启动器主程序文件名（watchdog 重启用）

    def check_launcher(self) -> UpdateInfo:
        raise NotImplementedError

    def plan_game(self, install_dir: str = "") -> dict:
        raise NotImplementedError

    def update_launcher(self, install_dir: str, local_version: str = ""):
        """默认流程：check → 下载 → MD5 → watchdog 自更新替换。"""
        from ..core import transport, staging, watchdog
        info = self.check_launcher()
        if local_version and not staging.is_newer(info.version, local_version):
            print(f"[{self.name}] 已是最新 {local_version}")
            return
        print(f"[{self.name}] 新版本 {info.version}（源: {info.source}），开始下载")
        pkg = transport.download(info.url, f"./_cache/{self.name}_launcher.pkg",
                                 info.md5)
        watchdog.self_update(pkg, install_dir, self.exe_name)

    def update_game(self, install_dir: str):
        raise NotImplementedError


REGISTRY = {}


def register(cls):
    REGISTRY[cls.name] = cls
    return cls


def get(name: str) -> Adapter:
    return REGISTRY[name]()
