#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Server 酱 3 失败推送：仅在本轮存在更新失败时发送一条汇总。

配置读 apkupd/game-update.yaml 的 notifications.serverchan3 节：
  enabled 改为 true 启用；sendkey 直接填 SendKey（sctp...t...），
  留空则读环境变量 SERVERCHAN_SENDKEY（变量名可由 sendkey_env 覆盖）。
发送失败的消息落盘 logs/notify-pending.json，下次运行按
retry_after_seconds 退避补发；每轮最多发 max_per_run 条，
不创建任何后台任务。推送异常只记日志，绝不影响主流程退出码。
"""
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("notify")

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "apkupd" / "game-update.yaml"
PENDING_PATH = BASE / "logs" / "notify-pending.json"


def load_config(path: str = None) -> dict:
    import yaml
    cfg_path = Path(path) if path else CONFIG_PATH
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return (cfg.get("notifications") or {}).get("serverchan3") or {}


def _resolve_sendkey(cfg: dict) -> str:
    key = (cfg.get("sendkey") or "").strip()
    if key:
        return key
    env = cfg.get("sendkey_env") or "SERVERCHAN_SENDKEY"
    return os.environ.get(env, "").strip()


def _send(sendkey: str, title: str, desp: str, tags: str = "") -> None:
    """SC3 推送；网络异常或返回 code != 0 都抛异常，由调用方落盘重试。"""
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    data = {"title": title[:32], "desp": desp}
    if tags:
        data["tags"] = tags
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(data).encode("utf-8"))
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("code") != 0:
        raise RuntimeError(f"Server酱返回错误: {payload}")


def _load_pending() -> list:
    try:
        return json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_pending(items: list) -> None:
    if items:
        PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
        PENDING_PATH.write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    elif PENDING_PATH.exists():
        PENDING_PATH.unlink()


def notify_failures(failures: list) -> None:
    """failures 为失败描述列表（["标签：原因", ...]）。

    非空时追加一条汇总消息并尝试发送；为空时不产生新消息
    （failures_only），但仍会按退避补发上轮落盘的失败消息。
    """
    try:
        cfg = load_config()
    except Exception as exc:
        # YAML parse errors can include the source line containing a secret.
        log.warning("读取通知配置失败，跳过推送（%s）", type(exc).__name__)
        return
    if not cfg.get("enabled"):
        if failures:
            log.info("Server酱未启用（enabled: false），跳过推送")
        return
    sendkey = _resolve_sendkey(cfg)
    if not sendkey:
        log.warning("Server酱已启用但未配置 SendKey（sendkey 或环境变量 %s），跳过推送",
                    cfg.get("sendkey_env") or "SERVERCHAN_SENDKEY")
        return

    tags = cfg.get("tags") or ""
    retry_after = float(cfg.get("retry_after_seconds") or 60)
    max_per_run = int(cfg.get("max_per_run") or 3)

    queue = _load_pending()
    if failures:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        desp = "\n\n".join(f"- {f}" for f in failures)
        queue.append({
            "title": f"游戏更新失败 {len(failures)} 项",
            "desp": f"{now} 本轮更新存在失败：\n\n{desp}",
            "next_after": 0.0,
        })

    sent, remain = 0, []
    now_ts = time.time()
    for msg in queue:
        if sent >= max_per_run or now_ts < msg.get("next_after", 0):
            remain.append(msg)      # 超每轮上限或仍在退避期，留到下轮
            continue
        try:
            _send(sendkey, msg["title"], msg["desp"], tags)
            sent += 1
            log.info("Server酱推送成功: %s", msg["title"])
        except Exception as exc:
            # Network exceptions can contain the full URL, including SendKey.
            log.warning("Server酱推送失败，已落盘待重试（%s）", type(exc).__name__)
            msg["next_after"] = now_ts + retry_after
            remain.append(msg)
    _save_pending(remain)
