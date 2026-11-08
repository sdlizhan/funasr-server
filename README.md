# FunASR-Server (SenseVoice + sherpa-onnx)

极轻量、高性能的本地 ASR WebSocket 服务端，兼容标准 **FunASRServer** 离线语音识别协议（适用于各类 ESP32 语音助手、智能硬件及自建语音交互客户端）。

底层基于阿里达摩院开源的 **SenseVoiceSmall (int8)** 与 **sherpa-onnx** 推理引擎，纯 CPU 运行，零显存占用，毫秒级响应。

---

## 特性

* **极轻量**：int8 量化模型仅约 228MB，镜像开箱即用，内存占用仅 300~500MB，不抢占任何 GPU 显存。
* **协议兼容**：完美对齐标准 `FunASRServer` WebSocket 离线协议，开箱即用支持各类开源语音助理与硬件终端。
* **多语言支持**：支持中文 (`zh`)、英文 (`en`)、日语 (`ja`)、韩语 (`ko`)、粤语 (`yue`) 或自动识别 (`auto`)。
* **智能噪声过滤**：内置静音与极短音频（<0.2s, <-50 dBFS）自动丢弃机制，防止环境底噪导致大模型频繁唤醒。
* **链路追踪与脱敏**：支持客户端代理真实 IP 提取、`x-request-id` 会话透传跟踪以及 Token/密码日志脱敏。
* **多架构支持**：Docker 镜像原生支持 `linux/amd64` 和 `linux/arm64`（适配 x86 电脑/服务器、树莓派、香橙派等开发板）。

---

## 快速使用 Docker 运行

### 1. 直接拉取镜像并启动
```bash
docker run -d \
  --name funasr-server \
  -p 10095:10095 \
  --restart unless-stopped \
  ghcr.io/<your-username>/funasr-server:latest
```

### 2. 挂载数据与日志目录（可选）
如果希望开启 `--save-wav` 保留识别音频用于排查调试：
```bash
docker run -d \
  --name funasr-server \
  -p 10095:10095 \
  -v $(pwd)/data:/app/data \
  --restart unless-stopped \
  ghcr.io/<your-username>/funasr-server:latest \
  --host 0.0.0.0 --port 10095 --language zh --save-wav
```

---

## 协议与交互规范

* **协议类型**：WebSocket
* **服务地址**：`ws://<ip>:10095`
* **交互流程**：
  1. 客户端发送 JSON 开始收音：
     ```json
     {"mode": "offline", "is_speaking": true}
     ```
  2. 客户端连续推送二进制 PCM 数据（规格：**16kHz / 16bit / 单声道**）。
  3. 客户端发送 JSON 结束收音：
     ```json
     {"is_speaking": false}
     ```
  4. 服务端返回识别结果 JSON：
     ```json
     {"text": "识别出来的文本", "is_final": true, "req_id": "xxx"}
     ```

---

## 本地开发与测试

### 本地直接运行
```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 运行服务端
python server.py --host 0.0.0.0 --port 10095 --language zh
```

### 运行测试客户端
```bash
python test_client.py
```

---

## 启动参数说明

| 参数 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `--host` | `0.0.0.0` | 监听地址 |
| `--port` | `10095` | 监听端口 |
| `--language` | `zh` | 识别语言：`zh`, `en`, `yue`, `ja`, `ko`, `auto` |
| `--threads` | `4` | 推理 CPU 线程数 |
| `--save-wav` | `false` | 是否保存接收到的识别音频（调试用） |
| `--save-dir` | `/app/data/asr_debug` | 保存音频的目标目录 |
| `--model-dir` | `/app/models/...` | SenseVoice 模型目录 |
