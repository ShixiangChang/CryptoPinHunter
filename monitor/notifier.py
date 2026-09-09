# -*- coding: utf-8 -*-
"""告警推送：飞书 / 钉钉 webhook + 控制台兜底。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import urllib.parse

import aiohttp

from . import config


class Notifier:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._last_send = 0.0

    async def send(self, title: str, lines: list[str]) -> None:
        if config.DRY_RUN:
            print(f"\n[DRY-RUN 告警] [{title}]\n" + "\n".join(lines) + "\n")
            return

        built = self._build(title, lines)
        if built is None:
            # webhook 没配置：不丢消息，打印到控制台并提醒
            print(f"\n[notifier] 警告: {config.NOTIFY_CHANNEL} webhook 未配置，告警仅打印到控制台\n"
                  f"[{title}]\n" + "\n".join(lines) + "\n")
            return

        url, payload = built
        async with self._lock:
            wait = 2.0 - (time.monotonic() - self._last_send)
            if wait > 0:
                await asyncio.sleep(wait)  # 简单限速，避免触发群机器人限频
            try:
                async with aiohttp.ClientSession(trust_env=True, proxy=config.PROXY or None) as session:
                    async with session.post(
                        url, json=payload, timeout=aiohttp.ClientTimeout(total=config.TIMEOUT)
                    ) as resp:
                        body = await resp.text()
                        if resp.status != 200 or self._bad_response(body):
                            print(f"[notifier] 推送失败 HTTP {resp.status}: {body[:200]}")
                self._last_send = time.monotonic()
            except Exception as exc:
                print(f"[notifier] 推送异常: {exc}")

    def _build(self, title: str, lines: list[str]) -> tuple[str, dict] | None:
        if config.NOTIFY_CHANNEL == "dingtalk":
            if not config.DINGTALK_WEBHOOK:
                return None
            url = config.DINGTALK_WEBHOOK
            if config.DINGTALK_SECRET:
                ts = str(round(time.time() * 1000))
                sign_str = f"{ts}\n{config.DINGTALK_SECRET}"
                digest = hmac.new(
                    config.DINGTALK_SECRET.encode("utf-8"), sign_str.encode("utf-8"), hashlib.sha256
                ).digest()
                sign = urllib.parse.quote_plus(base64.b64encode(digest))
                url = f"{url}&timestamp={ts}&sign={sign}"
            text = "### " + title + "\n" + "\n".join(f"> {ln}" for ln in lines)
            return url, {
                "msgtype": "markdown",
                "markdown": {"title": title, "text": text},
            }

        # 默认飞书
        if not config.FEISHU_WEBHOOK:
            return None
        return config.FEISHU_WEBHOOK, {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": title,
                        "content": [[{"tag": "text", "text": ln}] for ln in lines],
                    }
                }
            },
        }

    @staticmethod
    def _bad_response(body: str) -> bool:
        try:
            data = json.loads(body)
        except Exception:
            return True
        if not isinstance(data, dict):
            return True
        for key in ("code", "StatusCode", "errcode"):
            if key in data and data[key] not in (0, "0"):
                return True
        return False
