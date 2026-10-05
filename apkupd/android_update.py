"""MuMu APK download, installation and version verification only.

APK 队列处理完毕后收尾：若实例是本次脚本自行启动的（原来没在跑），
调用 MuMu CLI shutdown 关闭实例；用户本来就开着的实例一律不动。
行为由 game-update.yaml 的 mumu.shutdown_after_update 控制，默认开启。
"""
import json
import subprocess
import time
from pathlib import Path
from game_update_for_mumu import ensure_apk
from apk_update import install_downloaded


def cli(args):
    result = subprocess.run([str(a) for a in args], capture_output=True, timeout=15)
    if result.returncode:
        raise RuntimeError('命令执行失败：' + result.stderr.decode('utf-8', errors='replace')[:300])
    return result.stdout


def manager(settings, *args):
    data = json.loads(cli([settings['manager_executable'], *args]))
    if data.get('errcode', data.get('error_code', 0)):
        raise RuntimeError(data.get('errmsg', 'MuMu CLI 返回错误'))
    return data


def shutdown_if_started_by_us(settings, index, started_by_us, report):
    """APK 队列跑完后收尾：仅关闭本次脚本自行启动的实例，关闭失败不掩盖已完成的安装结果。"""
    if not started_by_us or not settings.get('shutdown_after_update', True):
        return
    try:
        manager(settings, 'control', '-v', index, 'shutdown')
        report(settings['name'], 'closed', 'APK 队列处理完毕，已关闭本次启动的 MuMu 实例')
    except Exception as exc:
        report(settings['name'], 'attention', f'关闭 MuMu 实例失败：{exc}')


def run_android(settings, report, dry_run, root, config):
    if not settings.get('enabled', False):
        return
    games = [g for g in settings.get('games', [])
             if g.get('enabled', True) and g['download_key'] != 'bh3']
    if dry_run:
        missing = [settings[key] for key in ('manager_executable', 'adb_path')
                   if not Path(settings[key]).is_file()]
        if missing:
            report(settings['name'], 'failed', 'MuMu 工具不存在：' + ', '.join(missing))
            return
        report(settings['name'], 'dry-run', f'官网 APK 下载 → 校验并安装 → 验证版本，队列 {len(games)} 个游戏')
        return
    index = str(settings.get('vm_index', 0))
    started_by_us = False
    try:
        info = manager(settings, 'info', '-v', index)
        if not info.get('is_android_started'):
            manager(settings, 'control', '-v', index, 'launch')
            started_by_us = True
        deadline = time.monotonic() + settings.get('startup_wait_seconds', 75)
        while not info.get('is_android_started'):
            if time.monotonic() >= deadline:
                raise RuntimeError('安卓实例启动超时')
            time.sleep(2)
            info = manager(settings, 'info', '-v', index)
        serial = f"{info['adb_host_ip']}:{info['adb_port']}"
        adb = [settings['adb_path']]
        cli([*adb, 'connect', serial])
        adb += ['-s', serial]
        # is_android_started 变 true 只代表实例起来了，adb 守护还要几秒才就绪；
        # 单次判定会遇到 device offline 假失败，改为带期限的轮询。
        deadline = time.monotonic() + settings.get('adb_wait_seconds', 90)
        while True:
            try:
                if cli([*adb, 'get-state']).strip() == b'device':
                    break
            except RuntimeError:
                pass  # 瞬时 offline / 未就绪，继续等
            if time.monotonic() >= deadline:
                raise RuntimeError('ADB 设备尚未连接（等待 adb 就绪超时）')
            time.sleep(2)
        active = manager(settings, 'control', '-v', index, 'app', 'info', '-i').get('active')
        if active in {g['package'] for g in games}:
            # 用户正在玩队列中的游戏，连「脚本自启的实例」也不关，避免中断使用。
            report(settings['name'], 'skipped', '当前有队列中的游戏在前台，跳过以避免中断使用')
            return
    except Exception as exc:
        # 启动超时等异常：若实例是脚本拉起的，同样收尾关闭，避免半启动实例空转。
        shutdown_if_started_by_us(settings, index, started_by_us, report)
        report(settings['name'], 'attention', str(exc))
        return
    for game in games:
        try:
            apk = ensure_apk(game['download_key'], config)
            # A large download can take minutes; the user may open the game meanwhile.
            active = manager(settings, 'control', '-v', index, 'app', 'info', '-i').get('active')
            if active == game['package']:
                report(game['name'] + ' APK', 'skipped', '下载期间游戏被打开，保留 APK 缓存并跳过安装')
                continue
            status, detail = install_downloaded(apk, game['package'], adb)
            report(game['name'] + ' APK', status, detail)
        except Exception as exc:
            report(game['name'] + ' APK', 'failed', str(exc))
    # 队列跑完（含个别失败/跳过）后统一收尾：仅关闭脚本本次自启的实例。
    # 收尾前最后确认一次前台：用户在玩（或刚打开）队列中的游戏就不关。
    try:
        active = manager(settings, 'control', '-v', index, 'app', 'info', '-i').get('active')
    except Exception:
        active = None
    if active in {g['package'] for g in games}:
        report(settings['name'], 'skipped', '收尾检测到游戏在前台，保持 MuMu 实例运行')
    else:
        shutdown_if_started_by_us(settings, index, started_by_us, report)
