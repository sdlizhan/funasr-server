"""ASR 客户端溯源取证模块 (Sniffer & Forensics)

当客户端（尤其是本机 127.0.0.1）连接到 ASR 服务时：
1. 亚毫秒级通过 Windows iphlpapi.dll (GetExtendedTcpTable) 查询源端口对应的 PID。
2. 通过 psutil 抓取进程名、完整可执行文件路径、命令行参数、父进程、工作目录、运行用户。
3. 若为代理/隧道（如 sshd, frp, tunnel），联动排查其活跃外部连接。
4. 记录完整的 WebSocket 握手 HTTP Headers。
5. 自动捕获音频为 WAV 文件并记录识别文本。
6. 持久化记录到 caught_clients.log 和 caught_clients.jsonl。
"""

import ctypes
from ctypes import wintypes
import json
import logging
import os
import socket
import struct
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import psutil
except ImportError:
    psutil = None

LOG = logging.getLogger("asr.sniffer")

# Windows API 定义
AF_INET = 2
AF_INET6 = 23
TCP_TABLE_OWNER_PID_ALL = 5


class MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


class MIB_TCP6ROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("ucLocalAddr", ctypes.c_ubyte * 16),
        ("dwLocalScopeId", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("ucRemoteAddr", ctypes.c_ubyte * 16),
        ("dwRemoteScopeId", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwState", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


def find_pid_for_tcp_port(port: int, is_ipv6: bool = False) -> Optional[int]:
    """通过 Windows 原生 iphlpapi.GetExtendedTcpTable 极速查找端口所有者 PID (耗时 < 1ms)"""
    if port <= 0:
        return None
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi.dll")
    except Exception as e:
        LOG.debug("加载 iphlpapi.dll 失败: %s", e)
        return None

    family = AF_INET6 if is_ipv6 else AF_INET
    size = wintypes.DWORD(0)
    # 获取所需缓冲区大小
    iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), True, family, TCP_TABLE_OWNER_PID_ALL, 0)
    if size.value <= 0:
        return None

    buf = ctypes.create_string_buffer(size.value)
    ret = iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), True, family, TCP_TABLE_OWNER_PID_ALL, 0)
    if ret != 0:
        return None

    num_entries = struct.unpack_from("I", buf.raw, 0)[0]
    row_struct = MIB_TCP6ROW_OWNER_PID if is_ipv6 else MIB_TCPROW_OWNER_PID
    row_size = ctypes.sizeof(row_struct)
    offset = 4

    for _ in range(num_entries):
        row = row_struct.from_buffer_copy(buf.raw[offset : offset + row_size])
        local_port = socket.ntohs(row.dwLocalPort & 0xFFFF)
        if local_port == port:
            return int(row.dwOwningPid)
        offset += row_size

    # 如果 IPv4 没找到且没指定 IPv6，尝试 IPv6
    if not is_ipv6:
        return find_pid_for_tcp_port(port, is_ipv6=True)
    return None


def get_process_forensics(pid: int) -> Dict[str, Any]:
    """根据 PID 获取进程的详细取证信息"""
    if not pid or not psutil:
        return {"pid": pid}

    info: Dict[str, Any] = {"pid": pid}
    try:
        p = psutil.Process(pid)
        info["name"] = p.name()
        try:
            info["exe"] = p.exe()
        except Exception:
            info["exe"] = "N/A"
        try:
            info["cmdline"] = " ".join(p.cmdline())
        except Exception:
            info["cmdline"] = "N/A"
        try:
            info["cwd"] = p.cwd()
        except Exception:
            info["cwd"] = "N/A"
        try:
            info["username"] = p.username()
        except Exception:
            info["username"] = "N/A"
        try:
            info["create_time"] = datetime.fromtimestamp(p.create_time()).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

        # 父进程追溯
        try:
            parent = p.parent()
            if parent:
                info["parent"] = {
                    "pid": parent.pid,
                    "name": parent.name(),
                    "exe": parent.exe() if hasattr(parent, "exe") else "N/A",
                    "cmdline": " ".join(parent.cmdline()) if hasattr(parent, "cmdline") else "N/A",
                }
        except Exception:
            pass

        # 检查是否为代理/隧道服务（如 sshd / frpc / node 等），获取其关联网络连接
        proc_name_lower = str(info.get("name", "")).lower()
        if any(kw in proc_name_lower for kw in ["ssh", "frp", "tunnel", "proxy", "nginx", "node", "java"]):
            try:
                conns = []
                for c in p.connections(kind="tcp"):
                    if c.status == "ESTABLISHED" and c.raddr:
                        conns.append(f"{c.raddr.ip}:{c.raddr.port}")
                if conns:
                    info["active_remote_connections"] = conns
            except Exception:
                pass

    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        info["error"] = str(e)

    return info


def extract_full_http_headers(ws) -> Dict[str, str]:
    """提取 WebSocket 握手的完整请求头与请求路径"""
    headers_dict = {}
    request = getattr(ws, "request", None)
    headers = getattr(request, "headers", None) if request else getattr(ws, "request_headers", None)
    if headers:
        for k, v in headers.items():
            headers_dict[str(k).lower()] = str(v)
    path = getattr(ws, "path", None) or (getattr(request, "path", None) if request else None)
    if path:
        headers_dict["_path"] = str(path)
    return headers_dict


def log_and_record_caught_client(
    conn_id: str,
    client_ip: str,
    client_port: int,
    proc_info: Optional[Dict[str, Any]],
    headers: Dict[str, str],
    first_msg: Optional[Dict[str, Any]] = None,
    recognized_text: Optional[str] = None,
    audio_path: Optional[str] = None,
    base_dir: Optional[Path] = None,
):
    """输出醒目抓捕日志并持久化保存取证结果"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    if base_dir is None:
        base_dir = Path(__file__).resolve().parent

    record = {
        "timestamp": now_str,
        "conn_id": conn_id,
        "client_ip": client_ip,
        "client_port": client_port,
        "process": proc_info or {},
        "headers": headers,
        "first_message": first_msg,
        "recognized_text": recognized_text,
        "audio_path": audio_path,
    }

    # 1. 终端大横幅日志
    banner_lines = [
        "",
        "╔══════════════════════════════════════════════════════════════════════════╗",
        f"║ 🎯 【抓捕到 ASR 客户端请求】 时间: {now_str}",
        "╠══════════════════════════════════════════════════════════════════════════╣",
        f"║  连接标识: [conn_id={conn_id}]",
        f"║  客户端地址: {client_ip}:{client_port}",
    ]

    if proc_info and "pid" in proc_info:
        banner_lines.append(f"║  进程 PID:   {proc_info.get('pid')} ({proc_info.get('name', 'unknown')})")
        banner_lines.append(f"║  程序路径:   {proc_info.get('exe', 'N/A')}")
        banner_lines.append(f"║  完整命令行: {proc_info.get('cmdline', 'N/A')}")
        banner_lines.append(f"║  工作目录:   {proc_info.get('cwd', 'N/A')}")
        banner_lines.append(f"║  运行用户:   {proc_info.get('username', 'N/A')}")
        if "parent" in proc_info:
            parent = proc_info["parent"]
            banner_lines.append(f"║  父进程:     PID {parent.get('pid')} ({parent.get('name')})")
            banner_lines.append(f"║  父进程命令: {parent.get('cmdline')}")
        if "active_remote_connections" in proc_info:
            banner_lines.append(f"║  代理外部连线: {proc_info['active_remote_connections']}")
    else:
        banner_lines.append("║  进程信息:   (远程/非本机进程或无法探测)")

    if headers:
        ua = headers.get("user-agent", "(无)")
        host = headers.get("host", "(无)")
        auth = headers.get("authorization", "(无)")
        dev_id = headers.get("device-id", "(无)")
        banner_lines.append(f"║  HTTP User-Agent: {ua}")
        banner_lines.append(f"║  HTTP Host:       {host}")
        if dev_id != "(无)":
            banner_lines.append(f"║  Device ID:       {dev_id}")
        if auth != "(无)":
            banner_lines.append(f"║  Authorization:   {auth[:40]}...")

    if first_msg:
        banner_lines.append(f"║  协议初次请求: {json.dumps(first_msg, ensure_ascii=False)}")

    if recognized_text is not None:
        banner_lines.append(f"║  识别文本:     「{recognized_text}」")
    if audio_path:
        banner_lines.append(f"║  保存录音:     {audio_path}")

    banner_lines.append("╚══════════════════════════════════════════════════════════════════════════╝")
    banner_text = "\n".join(banner_lines)

    # 打印到控制台
    print(banner_text, flush=True)

    # 2. 写入 JSONL 和 Markdown 日志文件
    log_dir = base_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = log_dir / "caught_clients.jsonl"
    md_path = log_dir / "caught_clients.log"

    try:
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        LOG.warning("写入 caught_clients.jsonl 失败: %s", e)

    try:
        with open(md_path, "a", encoding="utf-8") as f:
            f.write(banner_text + "\n\n")
    except Exception as e:
        LOG.warning("写入 caught_clients.log 失败: %s", e)
