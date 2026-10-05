# qbob (QQ 官方机器人 -> OneBot v11) 的 Zeabur 部署镜像
#
# 两个关键设计(都是踩过的坑):
#   1. 必须装 build-essential。依赖里的 pilk 是 C 扩展, PyPI 上只发布了
#      win_amd64 的 wheel, Linux 只能下载 sdist 就地编译; slim 镜像没有编译器,
#      pip install 会直接失败(exit code 1)。
#   2. 不再执行 `pip install .`(打包本项目)。那一步走 setuptools 打包,
#      容易因元数据/构建隔离出问题; qbob 可以直接从源码运行
#      (`python -m qqbot_onebot`), 版本号是源码里的普通字面量, 不需要安装。
#
# 期望的仓库布局(把本目录文件放进 qbob 仓库):
#   Dockerfile                    <- 本文件, 放仓库根
#   config.example.json           <- qbob 自带
#   qqbot_onebot/                 <- qbob 自带源码
#   scripts/zeabur-entrypoint.py  <- 本目录的入口脚本

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg         : 语音转码、图片/视频压缩(qbob 用 subprocess 调用)
# build-essential: 编译 pilk 的 C 扩展(Linux 没有预编译 wheel)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 只装运行依赖, 不打包本项目。版本下限与 qbob 的 pyproject.toml 一致。
# 如果卡在下载依赖(超时/解析失败), 取消下面这行注释换国内镜像。
# ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir \
        "fastapi>=0.115" \
        "uvicorn>=0.30" \
        "websockets>=12" \
        "aiohttp>=3.10" \
        "aiosqlite>=0.20" \
        "pynacl>=1.5" \
        "pilk>=0.2" \
        "cryptography>=43" \
        "qrcode>=8" \
        "python-multipart>=0.0.9" \
        "pillow>=10"

# 复制源码。`python -m qqbot_onebot` 在 /app 下运行可直接导入该包,
# 不需要安装进 site-packages。
COPY . .

# ============================================================================
# 关键补丁: 修掉"登录 POST 一律 403 / 前端显示权限不足"的问题
#
# 背景: qbob 的 web/app.py 有 CSRF 中间件 admin_security_headers, 它调用
# is_cross_site_write() 比较"浏览器 Origin 的 scheme"和"应用自认的 scheme"。
#   - qbob 默认 forwarded_allow_ips="127.0.0.1", 只信任本机回环来的 X-Forwarded-*
#   - 但 Zeabur 的反代在 Pod 网络里(如 10.42.x.x), 不是 127.0.0.1
#   - => uvicorn 忽略 X-Forwarded-Proto, request.url.scheme 恒为 http
#   - 而浏览器 Origin 是 https://<域名> => https != http => 判定跨站 => 403
#   - 登录路由本身不需要任何角色, 所以这个 403 只可能来自该中间件
#
# 打两个补丁, 任一生效即可恢复登录; 两个都打是为了不依赖平台是否转发该头:
#   补丁1 放宽反代信任范围, 让 scheme 能正确识别为 https(修根因)
#   补丁2 去掉 scheme 比较, 只比 host 与 sec-fetch-site(对反代缺失该头免疫)
#
# 补丁2 的安全性: 跨站请求的 Origin 是攻击者域名, host 对不上照样被拦;
# 且 sec-fetch-site == cross-site 的检查保持不变。同 host 的 http 页面无法被攻击者控制。
#
# 每个补丁后都跟 grep 断言: 上游若改了对应代码, 构建会直接失败(而不是静默失效)。
# 构建日志里会出现 PATCH-OK-1 / PATCH-OK-2 两行, 可据此确认补丁确实应用了。
# ============================================================================
RUN sed -i 's/forwarded_allow_ips="127\.0\.0\.1"/forwarded_allow_ips="*"/' \
        qqbot_onebot/__main__.py \
    && grep -qF 'forwarded_allow_ips="*"' qqbot_onebot/__main__.py \
    && echo "PATCH-OK-1: forwarded_allow_ips 已放宽为 *" \
    && sed -i 's/return parsed\.scheme != scheme or parsed\.netloc\.lower() != host\.lower()/return parsed.netloc.lower() != host.lower()/' \
        qqbot_onebot/web/app.py \
    && grep -qF 'return parsed.netloc.lower() != host.lower()' qqbot_onebot/web/app.py \
    && ! grep -qF 'parsed.scheme != scheme' qqbot_onebot/web/app.py \
    && echo "PATCH-OK-2: is_cross_site_write 已去掉 scheme 比较"

# 数据目录。注意: 这里故意不写 `VOLUME` 指令 ——
# Dockerfile 的 VOLUME 只是 Docker 层面的匿名卷标记, 不会在 Zeabur 上创建持久卷,
# 反而容易让人误以为已经持久化了。持久化必须在 Zeabur 面板给本服务显式添加 Volume,
# 挂载路径填 /app/data。
RUN mkdir -p /app/data

# Zeabur 注入 PORT; 入口脚本把它写进 config.json 并监听 0.0.0.0
CMD ["python", "scripts/zeabur-entrypoint.py"]
