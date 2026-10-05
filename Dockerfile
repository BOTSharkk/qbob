# qbob (QQ 官方机器人 -> OneBot v11) 的 Zeabur 部署镜像
#
# 为什么用 Dockerfile 而不是 Zeabur 的 Python 构建器:
#   1. qbob 要求 Python >= 3.12, 固定基础镜像最稳;
#   2. 需要在启动时按平台 $PORT 改写 config.json(见 scripts/zeabur-entrypoint.py),
#      所以要把该脚本一起打进镜像。
#
# 期望的仓库布局(把本目录文件放进 qbob 仓库):
#   Dockerfile                 <- 本文件, 放仓库根
#   pyproject.toml             <- qbob 自带
#   uv.lock                    <- qbob 自带
#   config.example.json        <- qbob 自带, 放仓库根
#   qqbot_onebot/              <- qbob 自带源码
#   scripts/zeabur-entrypoint.py  <- 本目录的入口脚本

FROM python:3.12-slim

# qbob 发非 silk 语音需要 ffmpeg; 缺少只影响语音转码, 不影响文本
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制依赖描述与包源码, 让依赖层可被缓存
COPY pyproject.toml uv.lock ./
COPY qqbot_onebot ./qqbot_onebot

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

COPY config.example.json ./
COPY scripts ./scripts

# 所有运行数据(管理台密码/bot 凭据/消息/ID 映射)都在 /app/data, 必须挂持久卷
RUN mkdir -p /app/data
VOLUME ["/app/data"]

ENV PYTHONUNBUFFERED=1

# Zeabur 注入 PORT; 入口脚本写进 config.json 并监听 0.0.0.0
CMD ["python", "scripts/zeabur-entrypoint.py"]