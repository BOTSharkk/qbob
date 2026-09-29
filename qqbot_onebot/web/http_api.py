"""napcat 风格的 HTTP API: 直接调用 OneBot 动作.

- `POST /{action}`      body 为 params(不指定 bot 时用默认 bot)
- `POST /{bot}/{action}` bot 可以是 appid 或 15 位虚拟号 self_id
- `GET`  同上, params 走 query string
- 鉴权: `Authorization: Bearer <token>` / `?access_token=` / `X-Token`
- 返回体与 OneBot 一致: {status, retcode, data, message}

默认只绑 127.0.0.1; token 为空时自动生成. 同面的 /qqapi/ 透传(web/passthrough.py)
走平台 appId + clientSecret 鉴权, 不用这里的 token.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..core.manager import BotManager
from ..onebot.actions import dispatch_action
from .passthrough import build_router as build_passthrough_router
from .app import install_body_limit

logger = logging.getLogger("qqbot.httpapi")


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"status": "failed", "retcode": 1403, "data": None, "message": "token 无效"},
        status_code=401,
    )


def build_http_api(manager: BotManager, token: str) -> FastAPI:
    app = FastAPI(title="qqbot-onebot http api", docs_url=None, redoc_url=None,
                  openapi_url=None)
    install_body_limit(app)
    # 先于下面的 /{selector}/{action} 注册, 否则 /qqapi/xxx 会被当成 bot+动作
    app.include_router(build_passthrough_router(manager))

    def check_token(request: Request) -> bool:
        if not token:
            return True
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            auth = auth[7:]
        given = (auth or request.headers.get("x-token", "")
                 or request.query_params.get("access_token", ""))
        return secrets.compare_digest(given, token)

    def resolve_bot(selector: str):
        """selector 为空时取唯一 bot, 否则取 default_bot."""
        if not selector:
            if len(manager.bots) == 1:
                return next(iter(manager.bots.values())), ""
            default = manager.get_bot(manager.config.default_bot or "")
            if default is not None:
                return default, ""
            return None, "存在多个 bot 且未设默认 bot, 请在路径里指定 appid 或 self_id"
        bot = manager.get_bot(selector)
        if bot is not None:
            return bot, ""
        if selector.isdigit():
            for candidate in manager.bots.values():
                if candidate.self_id == int(selector):
                    return candidate, ""
        return None, f"未知 bot: {selector}"

    async def handle(request: Request, selector: str, action: str) -> JSONResponse:
        if not check_token(request):
            return _unauthorized()
        bot, error = resolve_bot(selector)
        if bot is None:
            return JSONResponse(
                {"status": "failed", "retcode": 1404, "data": None, "message": error},
                status_code=404,
            )
        params: dict = {}
        if request.method == "POST":
            try:
                body = await request.json()
                if isinstance(body, dict):
                    params = body.get("params") if isinstance(
                        body.get("params"), dict) else body
            except Exception:
                # 表单兜底需 python-multipart, 没装就当空参数, 别抛 500
                try:
                    params = dict(await request.form())
                except Exception:
                    params = {}
        else:
            params = {k: v for k, v in request.query_params.items()
                      if k != "access_token"}
        result = await dispatch_action(bot, action, params)
        return JSONResponse(result)

    @app.api_route("/{action}", methods=["GET", "POST"])
    async def single(request: Request, action: str):
        return await handle(request, "", action)

    @app.api_route("/{selector}/{action}", methods=["GET", "POST"])
    async def with_bot(request: Request, selector: str, action: str):
        return await handle(request, selector, action)

    return app
