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

# 所有运行数据(管理台密码 / bot 凭据 / 消息 / ID 映射)都在 /app/data, 必须挂持久卷
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# Zeabur 注入 PORT; 入口脚本把它写进 config.json 并监听 0.0.0.0
CMD ["python", "scripts/zeabur-entrypoint.py"]
