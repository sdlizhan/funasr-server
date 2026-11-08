r"""
本地 ASR WebSocket 服务（sherpa-onnx + SenseVoiceSmall）

协议对齐标准 FunASR WebSocket 离线识别协议（FunASRServer）：
  1. 客户端发送 JSON 配置: {"mode":"offline","is_speaking":true,...}
  2. 客户端发送二进制 PCM: 16kHz / 16bit / 单声道
  3. 客户端发送 JSON: {"is_speaking":false}
  4. 服务端返回 JSON: {"text":"识别结果","is_final":true}

启动:
  venv\Scripts\python.exe server.py --host 0.0.0.0 --port 10095 --language zh
"""

import argparse
import asyncio
import json
import logging
import math
import os
import re
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, Tuple

import numpy as np
import sherpa_onnx
import websockets


SAMPLE_RATE = 16000
MIN_AUDIO_SEC = 0.2
MIN_RMS_DBFS = -50.0
LOG = logging.getLogger("asr")


def extract_ws_client_info(ws) -> Tuple[str, int, str]:
    """提取 WebSocket 客户端真实 IP 与端口，若有代理头则补充说明"""
    peer = getattr(ws, "remote_address", None)
    if isinstance(peer, tuple) and len(peer) >= 2:
        client_ip = str(peer[0])
        try:
            client_port = int(peer[1])
        except (ValueError, TypeError):
            client_port = 0
        client_repr = f"{client_ip}:{client_port}"
    elif peer:
        client_ip = str(peer)
        client_port = 0
        client_repr = str(peer)
    else:
        client_ip = "unknown"
        client_port = 0
        client_repr = "unknown"

    request = getattr(ws, "request", None)
    headers = getattr(request, "headers", None) if request else getattr(ws, "request_headers", None)
    if headers:
        xff = headers.get("x-forwarded-for")
        real_ip = headers.get("x-real-ip")
        proxies = []
        if xff:
            proxies.append(f"xff={xff}")
        if real_ip and real_ip != client_ip:
            proxies.append(f"real_ip={real_ip}")
        if proxies:
            client_repr = f"{client_repr} ({', '.join(proxies)})"
    return client_ip, client_port, client_repr


def extract_ws_request_id(ws, prefix: str = "asr") -> str:
    """优先提取握手阶段传递的 X-Request-ID 等追踪标识，不存在时生成唯一 ID"""
    request = getattr(ws, "request", None)
    headers = getattr(request, "headers", None) if request else getattr(ws, "request_headers", None)
    if headers:
        for header_name in ("x-request-id", "x-correlation-id", "request-id", "x-trace-id", "client-id", "device-id"):
            val = headers.get(header_name)
            if val and isinstance(val, str) and val.strip():
                clean_id = re.sub(r'[^a-zA-Z0-9_\-\.:]', '', val.strip())[:64]
                if clean_id:
                    return clean_id
    now_str = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return f"{prefix}-conn-{now_str}"


def sanitize_log_text(text: str, max_len: int = 500) -> str:
    """清理并脱敏日志文本，防止敏感鉴权凭据泄露或日志换行注入"""
    if not text:
        return ""
    clean = re.sub(r'[\r\n\t]+', ' ', text).strip()
    clean = re.sub(r'(?i)\b(bearer\s+)[a-zA-Z0-9_\-\.]{8,}', r'\1***', clean)
    clean = re.sub(r'(?i)\b(sk-[a-zA-Z0-9_\-]{10,})', r'***', clean)
    clean = re.sub(r'(?i)\b(password|token|secret)\s*[:=]\s*\S+', r'\1=***', clean)
    if len(clean) > max_len:
        return clean[:max_len] + f"...(共{len(clean)}字)"
    return clean


class Recognizer:
    def __init__(self, model_dir: Path, language: str, threads: int):
        model_path = model_dir / "model.int8.onnx"
        tokens_path = model_dir / "tokens.txt"
        if not model_path.is_file():
            raise FileNotFoundError(f"缺少模型文件: {model_path}")
        if not tokens_path.is_file():
            raise FileNotFoundError(f"缺少 tokens: {tokens_path}")

        t0 = time.time()
        self.model = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model_path),
            tokens=str(tokens_path),
            num_threads=threads,
            sample_rate=SAMPLE_RATE,
            feature_dim=80,
            decoding_method="greedy_search",
            debug=False,
            language=language,
            use_itn=True,
        )
        LOG.info("模型加载完成，耗时 %.2fs | language=%s | threads=%d", time.time() - t0, language, threads)

    def transcribe(self, pcm_bytes: bytes, log_prefix: str = "") -> str:
        prefix = f"{log_prefix} " if log_prefix else ""
        if len(pcm_bytes) < MIN_AUDIO_SEC * SAMPLE_RATE * 2:
            LOG.info("%s音频过短（%.2fs），跳过识别", prefix, len(pcm_bytes) / 2 / SAMPLE_RATE)
            return ""
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples * samples)))
        dbfs = 20 * math.log10(max(rms, 1e-12))
        if dbfs < MIN_RMS_DBFS:
            LOG.info("%s音量过低（%.1f dBFS），跳过识别", prefix, dbfs)
            return ""
        stream = self.model.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        self.model.decode_stream(stream)
        return stream.result.text.strip()


def save_wav(pcm_bytes: bytes, out_dir: Path, name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.wav"
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(pcm_bytes)
    return path


async def handler(ws, recognizer: Recognizer, args):
    client_ip, client_port, client_str = extract_ws_client_info(ws)
    conn_id = extract_ws_request_id(ws, prefix="asr")

    buffer = bytearray()
    seq = 0
    current_req_id = None
    LOG.info("[ASR] [conn_id=%s] [client=%s] 连接建立", conn_id, client_str)

    try:
        async for message in ws:
            if isinstance(message, (bytes, bytearray)):
                buffer.extend(message)
                continue

            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                LOG.warning("[ASR] [conn_id=%s] [client=%s] 收到非 JSON 文本: %r", conn_id, client_str, message[:200])
                continue

            msg_type = (
                data.get("type")
                or ("start_speaking" if data.get("is_speaking") is True
                    else "stop_speaking" if data.get("is_speaking") is False
                    else f"config(mode={data.get('mode')})" if "mode" in data
                    else "json_message")
            )

            extracted_msg_id = (
                data.get("request_id")
                or data.get("req_id")
                or data.get("session_id")
                or data.get("wav_name")
            )

            if data.get("is_speaking") is True:
                buffer.clear()
                seq += 1
                clean_extracted = re.sub(r'[^a-zA-Z0-9_\-\.:]', '', str(extracted_msg_id).strip())[:64] if extracted_msg_id else ""
                current_req_id = clean_extracted if clean_extracted else f"{conn_id}-req{seq}"
                LOG.info(
                    "[ASR] [conn_id=%s] [req_id=%s] [client=%s] 收到协议消息 [msg_type=%s] | 开始收音",
                    conn_id, current_req_id, client_str, msg_type
                )
                continue

            if data.get("is_speaking") is False:
                pcm = bytes(buffer)
                buffer.clear()
                if not current_req_id:
                    seq += 1
                    clean_extracted = re.sub(r'[^a-zA-Z0-9_\-\.:]', '', str(extracted_msg_id).strip())[:64] if extracted_msg_id else ""
                    current_req_id = clean_extracted if clean_extracted else f"{conn_id}-req{seq}"
                req_id = current_req_id
                current_req_id = None

                audio_sec = len(pcm) / 2 / SAMPLE_RATE
                t0 = time.time()
                LOG.info(
                    "[ASR] [conn_id=%s] [req_id=%s] [client=%s] 收到协议消息 [msg_type=%s] | 结束收音, 开始识别 (音频大小=%d bytes, 预计时长=%.2fs)",
                    conn_id, req_id, client_str, msg_type, len(pcm), audio_sec
                )

                log_prefix = f"[ASR] [conn_id={conn_id}] [req_id={req_id}] [client={client_str}]"
                try:
                    text = await asyncio.to_thread(recognizer.transcribe, pcm, log_prefix)
                except Exception as exc:  # noqa: BLE001
                    elapsed = time.time() - t0
                    LOG.exception("%s 识别失败: %s | 耗时=%.3fs", log_prefix, exc, elapsed)
                    await ws.send(json.dumps({"text": "", "is_final": True, "req_id": req_id}))
                    continue

                elapsed = time.time() - t0
                rtf = elapsed / audio_sec if audio_sec else 0
                safe_text = sanitize_log_text(text)
                LOG.info(
                    "[ASR] [conn_id=%s] [req_id=%s] [client=%s] 识别完成 | 音频=%.2fs | 耗时=%.3fs | RTF=%.3f | 文本='%s'",
                    conn_id, req_id, client_str, audio_sec, elapsed, rtf, safe_text or "(空)",
                )

                if args.save_wav:
                    saved_path = save_wav(pcm, Path(args.save_dir), f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{req_id}")
                    LOG.info("[ASR] [conn_id=%s] [req_id=%s] [client=%s] 已保存音频: %s", conn_id, req_id, client_str, saved_path)
                await ws.send(json.dumps({"text": text, "is_final": True, "req_id": req_id}, ensure_ascii=False))
                continue

            LOG.info("[ASR] [conn_id=%s] [client=%s] 收到协议消息 [msg_type=%s] | 内容=%s", conn_id, client_str, msg_type, data)

    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception:  # noqa: BLE001
        LOG.exception("[ASR] [conn_id=%s] [client=%s] 连接异常", conn_id, client_str)
    finally:
        LOG.info("[ASR] [conn_id=%s] [client=%s] 连接关闭", conn_id, client_str)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=10095)
    parser.add_argument(
        "--model-dir",
        default=str(Path(__file__).parent / "models" / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue"),
    )
    parser.add_argument("--language", default="zh", help="zh/en/yue/ja/ko/auto")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--save-wav", action="store_true")
    parser.add_argument("--save-dir", default=str(Path(__file__).parent / "data" / "asr_debug"))
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    )
    # 忽略 websockets 握手断开等底层日志，防止 K8s 健康检查探测刷屏
    logging.getLogger("websockets.server").setLevel(logging.WARNING)
    logging.getLogger("websockets.protocol").setLevel(logging.WARNING)

    recognizer = Recognizer(Path(args.model_dir), args.language, args.threads)
    async with websockets.serve(
        lambda ws: handler(ws, recognizer, args),
        args.host,
        args.port,
        subprotocols=["binary"],
        ping_interval=None,
        max_size=32 * 1024 * 1024,
    ):
        LOG.info("ASR 服务已启动: ws://%s:%d (language=%s)", args.host, args.port, args.language)
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
