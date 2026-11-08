FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

WORKDIR /app

# 安装音频处理必要系统库与下载工具
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    wget \
    bzip2 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 运行依赖
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 下载并解压 SenseVoiceSmall int8 模型 (约 228MB)
RUN mkdir -p /app/models && \
    wget -q https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2 && \
    tar xjf sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2 -C /app/models && \
    mv /app/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17 /app/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue && \
    rm -rf sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2 /app/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue/test_wavs

# 拷贝代码
COPY server.py /app/server.py

# 暴露 WebSocket 端口
EXPOSE 10095

ENTRYPOINT ["python", "server.py"]
CMD ["--host", "0.0.0.0", "--port", "10095", "--language", "zh"]
