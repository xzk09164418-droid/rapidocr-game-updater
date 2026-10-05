# -*- coding: utf-8 -*-
"""自更新看守进程：运行中的 exe 不能覆盖自己，由独立进程等主程序退出后替换并重启。
与官方实现同原理（米哈游 HYUpdater.exe / 库洛 launcher_updater.exe /
鹰角 Updater.exe / 异环 NTEUpdate.exe）。"""
import os
import subprocess
import sys
import tempfile
import zipfile

WATCHDOG = r'''
import os, sys, time, shutil, subprocess
pid, staging, target, exe = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
deadline = time.time() + 120
while time.time() < deadline:                       # 等待主进程退出
    try:
        os.kill(pid, 0)
    except OSError:
        break
    time.sleep(0.5)
for name in os.listdir(staging):                    # 先备份再覆盖，可回滚
    s, d = os.path.join(staging, name), os.path.join(target, name)
    if os.path.exists(d):
        os.replace(d, d + ".old")
    shutil.move(s, d)
subprocess.Popen([exe], cwd=target)                 # 拉起新版本
'''


def self_update(package: str, install_dir: str, exe_name: str):
    """package 为 zip 或已解压目录；解压到 staging 后启动 watchdog 并退出本进程。"""
    staging = tempfile.mkdtemp(prefix="updater_staging_")
    if zipfile.is_zipfile(package):
        with zipfile.ZipFile(package) as z:
            z.extractall(staging)
            # 常见结构：zip 里套一层版本号目录，剥掉
            entries = os.listdir(staging)
            if len(entries) == 1 and os.path.isdir(os.path.join(staging, entries[0])):
                inner = os.path.join(staging, entries[0])
                for n in os.listdir(inner):
                    os.replace(os.path.join(inner, n), os.path.join(staging, n))
    else:
        staging = package
    watchdog = os.path.join(tempfile.gettempdir(), "updater_watchdog.py")
    with open(watchdog, "w", encoding="utf-8") as f:
        f.write(WATCHDOG)
    subprocess.Popen(
        [sys.executable, watchdog, str(os.getpid()), staging,
         install_dir, os.path.join(install_dir, exe_name)],
        creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        if os.name == "nt" else 0, close_fds=True)
    print("看守进程已启动，主程序退出以完成替换……")
    sys.exit(0)
