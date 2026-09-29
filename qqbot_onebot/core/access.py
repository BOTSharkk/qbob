"""黑白名单: 群聊按 bot 选白/黑名单模式(默认白), 私聊恒黑名单.

条目按 (bot_appid, chat_type, list_type, openid) 存, 不同 bot 下互相独立。
"""

from __future__ import annotations

import time

from ..db import Database


class AccessControl:
    def __init__(self, db: Database):
        self.db = db
        # (bot_appid, chat_type, list_type) -> set[openid]
        self._cache: dict[tuple[str, str, str], set[str]] = {}

    async def load(self) -> None:
        self._cache.clear()
        rows = await self.db.fetchall(
            "SELECT bot_appid, chat_type, list_type, openid FROM access_list"
        )
        for row in rows:
            key = (row["bot_appid"], row["chat_type"], row["list_type"])
            self._cache.setdefault(key, set()).add(row["openid"])

    def _set(self, bot_appid: str, chat_type: str, list_type: str) -> set[str]:
        return self._cache.setdefault((bot_appid, chat_type, list_type), set())

    def allowed(
        self, bot_appid: str, list_mode: str, chat_type: str, openid: str
    ) -> bool:
        # 群聊未知模式按白名单处理(fail-closed)
        if chat_type == "private" or list_mode == "black":
            return openid not in self._set(bot_appid, chat_type, "black")
        return openid in self._set(bot_appid, chat_type, "white")

    async def add(
        self,
        bot_appid: str,
        chat_type: str,
        list_type: str,
        openid: str,
        virtual_id: int = 0,
        note: str = "",
        added_by: str = "",
    ) -> bool:
        try:
            await self.db.execute(
                "INSERT INTO access_list (bot_appid, chat_type, list_type, openid,"
                " virtual_id, note, added_by, added_at) VALUES (?,?,?,?,?,?,?,?)",
                (bot_appid, chat_type, list_type, openid, virtual_id, note,
                 added_by, int(time.time())),
            )
        except Exception:
            return False  # 已存在
        self._set(bot_appid, chat_type, list_type).add(openid)
        return True

    async def remove(
        self, bot_appid: str, chat_type: str, list_type: str, openid: str
    ) -> bool:
        cur = await self.db.execute(
            "DELETE FROM access_list WHERE bot_appid=? AND chat_type=?"
            " AND list_type=? AND openid=?",
            (bot_appid, chat_type, list_type, openid),
        )
        self._set(bot_appid, chat_type, list_type).discard(openid)
        return cur.rowcount > 0

    @staticmethod
    def _entry_filters(bot_appid: str | None, q: str) -> tuple[list[str], list]:
        wheres, args = [], []
        if bot_appid:
            wheres.append("bot_appid=?")
            args.append(bot_appid)
        if q:
            like = f"%{q}%"
            wheres.append("(openid LIKE ? OR CAST(virtual_id AS TEXT) LIKE ?"
                          " OR note LIKE ? OR added_by LIKE ?)")
            args += [like, like, like, like]
        return wheres, args

    async def entries(self, bot_appid: str | None = None, limit: int = 200,
                      offset: int = 0, q: str = "") -> list[dict]:
        wheres, args = self._entry_filters(bot_appid, q)
        sql = "SELECT * FROM access_list"
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY added_at DESC, id DESC LIMIT ? OFFSET ?"
        rows = await self.db.fetchall(sql, args + [limit, offset])
        return [dict(r) for r in rows]

    async def count(self, bot_appid: str | None = None, q: str = "") -> int:
        wheres, args = self._entry_filters(bot_appid, q)
        sql = "SELECT COUNT(*) AS n FROM access_list"
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        row = await self.db.fetchone(sql, args)
        return int(row["n"]) if row else 0
