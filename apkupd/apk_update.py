"""Validate and install a downloaded APK on an explicitly selected MuMu device."""
import re
import subprocess
from pathlib import Path
from apkutils2 import APK


def apk_info(path):
    manifest = APK(str(Path(path).resolve(strict=True))).get_manifest()
    raw = str(manifest['@android:versionCode'])
    version = int(raw, 16) if raw.lower().startswith('0x') else int(raw)
    return manifest['@package'], version


def run(adb, *args, timeout=30):
    result = subprocess.run([*adb, *args], capture_output=True, timeout=timeout)
    output = (result.stdout + result.stderr).decode('utf-8', errors='replace')
    if result.returncode:
        raise RuntimeError('ADB 执行失败：' + output[:300])
    return output


def installed_vc(adb, package):
    output = run(adb, 'shell', 'dumpsys', 'package', package)
    match = re.search(r'\bversionCode=(\d+)', output)
    return int(match[1]) if match else None


def install_downloaded(path, expected_package, adb, *, reinstall_same_version=False):
    path = Path(path).resolve(strict=True)
    package, new = apk_info(path)
    if package != expected_package:
        raise ValueError(f'APK 包名不符：期望 {expected_package}，实际 {package}')
    old = installed_vc(adb, package)
    if old is not None and new < old:
        return 'skipped', f'官网 APK 版本 {new} 低于已安装版本 {old}，保留现有版本'
    if old is not None and new == old and not reinstall_same_version:
        return 'up-to-date', f'已安装版本 {old}，下载版本 {new}，无需安装'
    output = run(adb, 'install', '-r', str(path), timeout=600)
    if not re.search(r'^Success\s*$', output, re.M):
        raise RuntimeError('APK 安装失败：' + output[:300])
    if installed_vc(adb, package) != new:
        raise RuntimeError('安装后版本校验不一致')
    return 'updated', f'APK 安装并验证成功：{old} → {new}'
