"""
FunASR 服务日志与连接/请求追踪单元测试
验证 WebSocket 连接的客户端 IP/端口、连接 ID 记录，
每次识别记录音频时长、耗时、识别结果，协议消息类型识别与敏感凭据脱敏。
"""

import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock

import websockets

import server
from server import (
    extract_ws_client_info,
    extract_ws_request_id,
    sanitize_log_text,
    handler,
)


class TestFunASRLogging(unittest.TestCase):
    def test_extract_ws_client_info_direct(self):
        ws_mock = MagicMock()
        ws_mock.remote_address = ("127.0.0.1", 54321)
        ws_mock.request = None
        ws_mock.request_headers = None

        ip, port, client_str = extract_ws_client_info(ws_mock)
        self.assertEqual(ip, "127.0.0.1")
        self.assertEqual(port, 54321)
        self.assertEqual(client_str, "127.0.0.1:54321")

    def test_extract_ws_client_info_with_proxies(self):
        ws_mock = MagicMock()
        ws_mock.remote_address = ("10.0.0.1", 8080)
        request_mock = MagicMock()
        request_mock.headers = {
            "x-forwarded-for": "203.0.113.195",
            "x-real-ip": "203.0.113.195",
        }
        ws_mock.request = request_mock

        ip, port, client_str = extract_ws_client_info(ws_mock)
        self.assertEqual(ip, "10.0.0.1")
        self.assertIn("xff=203.0.113.195", client_str)
        self.assertIn("real_ip=203.0.113.195", client_str)

    def test_extract_ws_request_id_custom(self):
        ws_mock = MagicMock()
        request_mock = MagicMock()
        request_mock.headers = {"x-request-id": "asr-trace-custom-777"}
        ws_mock.request = request_mock

        req_id = extract_ws_request_id(ws_mock)
        self.assertEqual(req_id, "asr-trace-custom-777")

    def test_extract_ws_request_id_fallback_device(self):
        ws_mock = MagicMock()
        request_mock = MagicMock()
        request_mock.headers = {"device-id": "ESP32-AA:BB:CC"}
        ws_mock.request = request_mock

        req_id = extract_ws_request_id(ws_mock)
        self.assertEqual(req_id, "ESP32-AA:BB:CC")

    def test_extract_ws_request_id_auto_generated(self):
        ws_mock = MagicMock()
        ws_mock.request = None
        ws_mock.request_headers = None

        req_id = extract_ws_request_id(ws_mock, prefix="asr")
        self.assertTrue(req_id.startswith("asr-conn-"))

    def test_sanitize_log_text(self):
        # 验证回车清洗、token脱敏与超长截断
        dirty = "测试文本\r\nBearer 1234567890abcdef\nsk-live-9999999999"
        cleaned = sanitize_log_text(dirty)
        self.assertNotIn("1234567890abcdef", cleaned)
        self.assertNotIn("sk-live-9999999999", cleaned)
        self.assertIn("Bearer ***", cleaned)
        self.assertIn("***", cleaned)

    def test_websocket_protocol_and_logging_flow(self):
        """启动临时 WebSocket 服务，验证连接、收音、识别全流程的日志输出与字段记录"""
        async def run_flow():
            mock_recognizer = MagicMock()
            mock_recognizer.transcribe.return_value = "明天天气怎么样"

            args_mock = MagicMock()
            args_mock.save_wav = False
            args_mock.save_dir = "tmp"

            server_ready = asyncio.Event()
            captured_port = []

            async def start_server():
                async with websockets.serve(
                    lambda ws: handler(ws, mock_recognizer, args_mock),
                    "127.0.0.1",
                    0,
                    subprotocols=["binary"],
                ) as srv:
                    port = srv.sockets[0].getsockname()[1]
                    captured_port.append(port)
                    server_ready.set()
                    await asyncio.Future()  # run forever until cancelled

            server_task = asyncio.create_task(start_server())
            await server_ready.wait()
            port = captured_port[0]

            uri = f"ws://127.0.0.1:{port}"
            # 模拟 1 秒钟 16kHz 16bit 单声道 PCM 音频 (32000 字节)
            fake_pcm = b"\x01\x00" * 16000

            async with websockets.connect(
                uri,
                subprotocols=["binary"],
                additional_headers={"X-Request-ID": "test-flow-req-101"},
            ) as ws:
                # 1. 发送配置 JSON (包含自定义 request_id / wav_name)
                await ws.send(
                    json.dumps(
                        {
                            "mode": "offline",
                            "wav_name": "sample-wav-01",
                            "is_speaking": True,
                        }
                    )
                )

                # 2. 发送二进制 PCM 音频
                await ws.send(fake_pcm)

                # 3. 发送停止说话
                await ws.send(json.dumps({"is_speaking": False}))

                # 4. 接收识别结果
                resp_str = await asyncio.wait_for(ws.recv(), timeout=5.0)
                resp = json.loads(resp_str)
                self.assertEqual(resp.get("text"), "明天天气怎么样")
                self.assertTrue(resp.get("is_final"))
                self.assertIn("req_id", resp)
                self.assertEqual(resp.get("req_id"), "sample-wav-01")

            # 等待服务端感知客户端连接关闭
            await asyncio.sleep(0.05)

            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass

        with self.assertLogs("asr", level="INFO") as log_cm:
            asyncio.run(run_flow())
            logs = "\n".join(log_cm.output)
            # 验证日志关键字段：
            # 连接建立带 conn_id 和 client
            self.assertIn("[ASR]", logs)
            self.assertIn("conn_id=test-flow-req-101", logs)
            self.assertIn("连接建立", logs)
            # 协议消息类型
            self.assertIn("msg_type=start_speaking", logs)
            self.assertIn("msg_type=stop_speaking", logs)
            # 识别完成、音频时长、耗时、RTF 和文本
            self.assertIn("识别完成", logs)
            self.assertIn("音频=1.00s", logs)
            self.assertIn("耗时=", logs)
            self.assertIn("RTF=", logs)
            self.assertIn("明天天气怎么样", logs)
            # 连接关闭
            self.assertIn("连接关闭", logs)


if __name__ == "__main__":
    unittest.main()
