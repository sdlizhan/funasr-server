"""标准 FunASR WebSocket 客户端测试脚本，用于本地验证 ASR 服务。"""

import argparse
import asyncio
import json
import time
import wave

import websockets


async def run(path: str, host: str, port: int, chunk_ms: int = 60):
    with wave.open(path, "rb") as f:
        assert f.getnchannels() == 1, "需要单声道"
        assert f.getsampwidth() == 2, "需要 16bit"
        assert f.getframerate() == 16000, f"需要 16kHz，当前 {f.getframerate()}"
        pcm = f.readframes(f.getnframes())

    uri = f"ws://{host}:{port}"
    print(f"连接 {uri} | 音频 {len(pcm)/2/16000:.2f}s | {len(pcm)} bytes")

    async with websockets.connect(
        uri,
        subprotocols=["binary"],
        ping_interval=None,
        additional_headers={"Authorization": "Bearer; none"},
        proxy=None,  # 局域网直连，绕过系统代理
    ) as ws:
        await ws.send(
            json.dumps(
                {
                    "mode": "offline",
                    "chunk_size": [5, 10, 5],
                    "chunk_interval": 10,
                    "wav_name": "local-test",
                    "is_speaking": True,
                    "itn": False,
                }
            )
        )
        step = int(16000 * 2 * chunk_ms / 1000)
        t0 = time.time()
        for i in range(0, len(pcm), step):
            await ws.send(pcm[i : i + step])
        await ws.send(json.dumps({"is_speaking": False}))
        resp = await asyncio.wait_for(ws.recv(), timeout=60)
        print(f"耗时 {(time.time()-t0)*1000:.0f} ms")
        print("返回:", resp)
        data = json.loads(resp)
        print("文本:", data.get("text"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=10095)
    ap.add_argument("--chunk-ms", type=int, default=60)
    args = ap.parse_args()
    asyncio.run(run(args.wav, args.host, args.port, args.chunk_ms))


if __name__ == "__main__":
    main()
