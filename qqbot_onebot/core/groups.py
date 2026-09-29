"""bot 分组(管理台分区). 列表存 config.json, bot 所属存 bots.grp; 默认分组总会存在."""

from __future__ import annotations


NAME_MAX = 32


def normalize_name(name: str) -> str:
    """校验分组名; 禁 / 是因为删除接口按路径传名."""
    name = str(name or "").strip()
    if not name or len(name) > NAME_MAX:
        raise ValueError(f"分组名需 1~{NAME_MAX} 个字")
    if "/" in name or any(ord(c) < 32 for c in name):
        raise ValueError("分组名不能含 / 或控制字符")
    return name


def ensure(config, name: str = "") -> str:
    """确保分组存在并返回分组名; 空名 = 默认分组."""
    groups = list(getattr(config, "bot_groups", None) or [])
    default = str(getattr(config, "default_bot_group", "") or "默认").strip()
    name = normalize_name(str(name or "").strip() or default)
    changed = False
    for wanted in (default, name):
        if wanted not in groups:
            groups.append(wanted)
            changed = True
    if changed:
        config.bot_groups = groups
        config.save()
    return name


async def adopt_existing(db, config) -> None:
    """把库里在用但列表缺失的分组收进来(老数据迁移)."""
    rows = await db.fetchall("SELECT DISTINCT grp FROM bots WHERE grp!=''")
    ensure(config)
    missing = [r["grp"] for r in rows if r["grp"] not in config.bot_groups]
    if missing:
        config.bot_groups = list(config.bot_groups) + missing
        config.save()
