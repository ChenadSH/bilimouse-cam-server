# BiliMouse 摄像头推流服务端
#
# 构建：docker build -t bilimouse-cam-server .
# 运行：docker run -d -p 39110:39110 -v $(pwd)/data:/data bilimouse-cam-server
#
# 基础镜像默认走官方 Docker Hub；国内拉不动时可在 .env 里设 BASE_IMAGE
# 指向加速源（compose 会透传成 build arg），无需改本文件。
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# pillow 的 JPEG 编解码是纯 C，不需要额外的系统库；
# 但 fonts 之类的可选依赖不需要装，保持镜像小。
WORKDIR /srv

COPY requirements.txt ./

# 国内直连 files.pythonhosted.org 拉 wheel/metadata 经常 Read timed out，
# 默认改走清华 PyPI 镜像（wheel 文件也在国内 CDN）。想换源有两种方式：
#   docker build --build-arg PIP_INDEX_URL=https://pypi.org/simple ...
#   或在 .env 里设 PIP_INDEX_URL（compose 会透传成 build arg）
# 同时放宽单次读超时和重试次数，偶发抖动不会让整层构建白跑。
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_TIMEOUT=60
ARG PIP_RETRIES=10
RUN pip install --no-cache-dir --prefer-binary \
        --index-url "${PIP_INDEX_URL}" \
        --timeout "${PIP_TIMEOUT}" \
        --retries "${PIP_RETRIES}" \
        -r requirements.txt

COPY app ./app

# 快照目录（挂卷到这里）
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 39110

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:39110/healthz')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "39110", "--proxy-headers"]
