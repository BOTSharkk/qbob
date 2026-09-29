"""每群主动消息计数与配额耗尽后的静默窗口.

平台频控两层: Bot 维度 认证 60/qpm、未认证 30/qpm; 单关系 20/qpm 且每群每天最多 1000 条.
两层超限都回 40034100, 故自己计数: 离 1000 远当分钟级限流短冷却, 接近才认定日配额耗尽.

日界时区无文档: 静默到北京/UTC 00:00 中较早者, 到点重试, 仍被拒再顺延到下一个.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("qqbot.quota")

DAY = 86400
# 每群每天最多接收 1000 条
DAILY_LIMIT = 1000
# 计数达此值 40034100 才按日配额耗尽处理, 否则当分钟级限流; 留 5% 余量(平台可能多算)
NEAR_LIMIT = 950
# 分钟级限流冷却(秒)
RATE_COOLDOWN = 70

BEIJING = timezone(timedelta(hours=8))


def next_boundary(now: float | None = None) -> float:
    """下一个候选零点: 北京/UTC 00:00 中较早者(猜错只多撞一次, 取晚的可能白等 8 小时)."""
    moment = datetime.fromtimestamp(now if now is not None else time.time(),
                                    tz=timezone.utc)
    candidates = []
    for tz in (timezone.utc, BEIJING):
        local = moment.astimezone(tz)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        candidates.append((midnight + timedelta(days=1)).timestamp())
    return min(candidates)


def boundary_label(ts: float) -> str:
    """零点所属时区, 写日志用."""
    utc = datetime.fromtimestamp(ts, tz=timezone.utc)
    return "UTC 00:00" if utc.hour == 0 else "北京 00:00"


class QuotaTracker:
    """按 (chat_type, peer_openid) 记账. 纯内存, 重启归零; 少数的部分靠 40034100 兜底."""

    def __init__(self) -> None:
        self._sent: dict[tuple[str, str], deque[float]] = {}
        self._blocked: dict[tuple[str, str], float] = {}
        self._errors: dict[str, int] = {}
        # 撞日配额次数, 供统计
        self.quota_hits = 0

    # ---------- 计数 ----------

    def note_sent(self, chat_type: str, peer_openid: str) -> None:
        """记一条主动消息(被动回复不占配额, 调用方已过滤)."""
        key = (chat_type, peer_openid)
        now = time.time()
        bucket = self._sent.setdefault(key, deque())
        bucket.append(now)
        self._trim(bucket, now)

    @staticmethod
    def _trim(bucket: deque[float], now: float) -> None:
        limit = now - DAY
        while bucket and bucket[0] < limit:
            bucket.popleft()

    def count(self, chat_type: str, peer_openid: str) -> int:
        """近 24 小时主动消息条数; 滚动窗口比任何固定日界都严格."""
        bucket = self._sent.get((chat_type, peer_openid))
        if not bucket:
            return 0
        self._trim(bucket, time.time())
        return len(bucket)

    def note_error(self, kind: str) -> None:
        self._errors[kind] = self._errors.get(kind, 0) + 1

    # ---------- 静默窗口 ----------

    def note_quota_error(self, chat_type: str, peer_openid: str) -> bool:
        """收到 40034100; 返回 True 表示按日配额耗尽静默到下一个零点."""
        key = (chat_type, peer_openid)
        used = self.count(chat_type, peer_openid)
        now = time.time()
        if used < NEAR_LIMIT and key not in self._blocked:
            # 离上限远, 当分钟级限流
            self._blocked[key] = now + RATE_COOLDOWN
            logger.info("配额: %s 触发频控(近 24h 仅 %d 条), 冷却 %ds",
                        peer_openid[:8], used, RATE_COOLDOWN)
            return False
        until = next_boundary(now)
        self.quota_hits += 1
        self._blocked[key] = until
        logger.warning(
            "配额: %s 日配额耗尽(近 24h %d 条), 静默至 %s —— 到点自动重试, "
            "若仍被拒会顺延到下一个候选零点",
            peer_openid[:8], used, boundary_label(until))
        return True

    def blocked_until(self, chat_type: str, peer_openid: str) -> float:
        until = self._blocked.get((chat_type, peer_openid), 0.0)
        if until and until <= time.time():
            self._blocked.pop((chat_type, peer_openid), None)
            logger.info("配额: %s 静默窗口结束(%s), 恢复下发",
                        peer_openid[:8], boundary_label(until))
            return 0.0
        return until

    def is_blocked(self, chat_type: str, peer_openid: str) -> bool:
        return self.blocked_until(chat_type, peer_openid) > 0

    def clear(self, chat_type: str, peer_openid: str) -> None:
        """发送成功, 提前解除静默."""
        self._blocked.pop((chat_type, peer_openid), None)

    # ---------- 统计 ----------

    def snapshot(self, top: int = 10) -> dict:
        now = time.time()
        rows = []
        for (chat_type, peer), bucket in self._sent.items():
            self._trim(bucket, now)
            if bucket:
                rows.append({"chat_type": chat_type, "peer_openid": peer,
                             "proactive_24h": len(bucket)})
        rows.sort(key=lambda r: r["proactive_24h"], reverse=True)
        blocked = [
            {"chat_type": c, "peer_openid": p, "until": int(u),
             "boundary": boundary_label(u)}
            for (c, p), u in self._blocked.items() if u > now
        ]
        return {
            "daily_limit": DAILY_LIMIT,
            "proactive_24h_total": sum(r["proactive_24h"] for r in rows),
            "peers": rows[:top],
            "blocked": blocked,
            "quota_hits": self.quota_hits,
            "errors": dict(self._errors),
        }
