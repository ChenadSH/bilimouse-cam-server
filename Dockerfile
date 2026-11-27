# BiliMouse 摄像头推流服务端
#
# 构建：docker build -t bilimouse-cam-server .
# 运行：docker run -d -p 8080:8080 -v $(pwd)/data:/data bilimouse-cam-server
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# pillow 的 JPEG 编解码是纯 C，不需要额外的系统库；
# 但 fonts 之类的可选依赖不需要装，保持镜像小。
WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 快照目录（挂卷到这里）
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
