#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mc-lan-helper —— 给 Minecraft Java 版「对局域网开放」配一个固定端口

背景:
  MC 的「对局域网开放」每次都会随机挑一个端口(比如 51234)，
  关掉世界再开就变了，隔着虚拟局域网(Friends 用的是 EasyTier/ZeroTier 之类)时
  每次都要重新问端口，很麻烦。

它做三件事:
  1. 监听 MC 发出的局域网广播 (UDP 224.0.2.60:4445)，自动读出这次开的端口;
  2. 在本机 0.0.0.0:<固定端口> 做 TCP 转发，把连接转给那个随机端口;
  3. 世界重开、端口变了会自动跟上(广播每隔一两秒重发一次)。

于是朋友只需要记住「你的虚拟局域网 IP:25565」，永远不用改。

它只是字节转发，不碰协议、不需要正版验证，也不改变 MC 的任何行为。

用法:
  python mc-lan-helper.py                  # 固定端口 25565
  python mc-lan-helper.py --listen 25565
  python mc-lan-helper.py --target 51234   # 手动指定 MC 端口(不自动检测)
  python mc-lan-helper.py --no-scan        # 关掉 netstat 兜底扫描

只用 Python 标准库。
"""

from __future__ import annotations

import argparse
import csv
import re
import select
import socket
import struct
import subprocess
import sys
import threading
import time

MC_GROUP = "224.0.2.60"
MC_GROUP_PORT = 4445
AD_RE = re.compile(rb"\[AD\](\d+)\[/AD\]")
MOTD_RE = re.compile(rb"\[MOTD\](.*?)\[/MOTD\]", re.S)

# 本程序常被 pythonw(无控制台) 启动，子进程必须隐藏窗口，否则每调一次就闪一个 CMD
_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0

_lock = threading.Lock()


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------
# 本机地址
# --------------------------------------------------------------------------

def local_ipv4s() -> list[str]:
    """尽力列出本机所有 IPv4(含虚拟网卡)，用于提示朋友该连哪个地址。"""
    ips: list[str] = []

    if sys.platform.startswith("win"):
        try:
            out = subprocess.run(
                ["ipconfig"], capture_output=True, text=True,
                encoding="oem", errors="replace", timeout=10,
                creationflags=_NO_WINDOW,
            ).stdout
            for line in out.splitlines():
                m = re.search(r"IPv4[^:]*:\s*([0-9]{1,3}(?:\.[0-9]{1,3}){3})", line)
                if m:
                    ips.append(m.group(1))
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            out = subprocess.run(
                ["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True,
                errors="replace", timeout=10, creationflags=_NO_WINDOW,
            ).stdout
            ips.extend(re.findall(r"inet ([0-9.]+)/", out))
        except (OSError, subprocess.SubprocessError):
            pass

    # 兜底: 默认路由出口地址
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass

    seen, out = set(), []
    for ip in ips:
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        if ip in seen:
            continue
        seen.add(ip)
        out.append(ip)
    return out


# --------------------------------------------------------------------------
# 检测 MC 的局域网端口
# --------------------------------------------------------------------------

def _java_pids() -> set[str]:
    """Windows: 找出 java/javaw 进程的 PID。"""
    pids: set[str] = set()
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
            encoding="oem", errors="replace", timeout=10,
            creationflags=_NO_WINDOW,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return pids
    for row in csv.reader(out.splitlines()):
        if len(row) >= 2 and row[0].strip().lower() in ("javaw.exe", "java.exe"):
            pids.add(row[1].strip())
    return pids


def scan_mc_port(ignore: set[int] | None = None) -> int | None:
    """
    兜底方案: 广播收不到时，从 netstat 里找 java 进程正在监听的高位端口。
    MC 客户端开局域网时，java 进程通常只监听这一个端口，所以唯一即命中。
    """
    if not sys.platform.startswith("win"):
        return None
    ignore = ignore or set()
    pids = _java_pids()
    if not pids:
        return None
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True,
            encoding="oem", errors="replace", timeout=15,
            creationflags=_NO_WINDOW,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None

    cands: list[int] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if parts[3].upper() != "LISTENING":
            continue
        if parts[4] not in pids:
            continue
        addr = parts[1]
        port_s = addr.rsplit(":", 1)[-1]
        if not port_s.isdigit():
            continue
        port = int(port_s)
        if port < 1024 or port in ignore:
            continue
        # 只要对外监听的(0.0.0.0 / [::])，127.0.0.1 上的多半不是
        host_part = addr.rsplit(":", 1)[0]
        if host_part not in ("0.0.0.0", "[::]", "[::]", "*"):
            continue
        if port not in cands:
            cands.append(port)

    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1:
        log(f"netstat 找到多个候选端口 {cands}，无法确定；请用 --target 手动指定")
    return None


class Detector:
    """负责发现 MC 这次开的端口，并喂给 Bridge。"""

    def __init__(self, on_port, ignore_ports: set[int] | None = None, allow_scan: bool = True):
        self._on_port = on_port
        self._ignore = set(ignore_ports or ())
        self._allow_scan = allow_scan
        self._found: int | None = None
        self._last_scan = 0.0

    # ---- UDP 广播 ----
    def _open_multicast(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        s.bind(("", MC_GROUP_PORT))

        targets = ["0.0.0.0"] + local_ipv4s()
        joined = 0
        for ip in targets:
            try:
                mreq = struct.pack(
                    "4s4s", socket.inet_aton(MC_GROUP), socket.inet_aton(ip)
                )
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                joined += 1
            except OSError:
                continue
        if joined == 0:
            log("加入组播组失败，将只依赖 netstat 扫描")
        s.settimeout(1.0)
        return s

    def _multicast_loop(self) -> None:
        try:
            s = self._open_multicast()
        except OSError as e:
            log(f"无法监听 UDP {MC_GROUP_PORT}: {e}（改用 netstat 扫描）")
            return
        log(f"正在监听 MC 局域网广播 UDP {MC_GROUP}:{MC_GROUP_PORT}")
        while True:
            try:
                data, _ = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            m = AD_RE.search(data)
            if not m:
                continue
            try:
                port = int(m.group(1))
            except ValueError:
                continue
            motd = ""
            mm = MOTD_RE.search(data)
            if mm:
                motd = mm.group(1).decode("utf-8", "replace").strip()
            self._found = port
            self._on_port(port, motd)

    # ---- netstat 兜底 ----
    def _scan_loop(self) -> None:
        while True:
            time.sleep(5)
            if self._found is not None:
                continue
            now = time.time()
            if now - self._last_scan < 5:
                continue
            self._last_scan = now
            port = scan_mc_port(ignore=self._ignore)
            if port:
                self._found = port
                self._on_port(port, "")

    def start(self) -> None:
        threading.Thread(target=self._multicast_loop, daemon=True).start()
        if self._allow_scan:
            threading.Thread(target=self._scan_loop, daemon=True).start()

    def reset(self) -> None:
        """转发目标失效时调用，让扫描重新开始。"""
        self._found = None


# --------------------------------------------------------------------------
# TCP 转发
# --------------------------------------------------------------------------

def _pump(a: socket.socket, b: socket.socket) -> int:
    """双向搬字节，返回总字节数。"""
    total = 0
    pair = [a, b]
    idle = 0
    try:
        while pair:
            r, _, x = select.select(pair, [], pair, 30)
            if x:
                break
            if not r:
                idle += 30
                if idle >= 300:      # 5 分钟完全没数据就回收
                    break
                continue
            idle = 0
            for s in r:
                other = b if s is a else a
                try:
                    chunk = s.recv(65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    try:
                        other.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    if s in pair:
                        pair.remove(s)
                    if not pair:
                        return total
                    continue
                total += len(chunk)
                try:
                    other.sendall(chunk)
                except OSError:
                    return total
    except (OSError, ValueError):
        pass
    finally:
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass
    return total


class Bridge:
    """固定端口 -> MC 随机端口 的 TCP 转发器。"""

    def __init__(self, host: str, port: int):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.srv.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.srv.bind((host, port))
        self.srv.listen(32)
        self.host = host
        self.port = self.srv.getsockname()[1]
        self._target: int | None = None
        self._tlock = threading.Lock()
        self.detector: Detector | None = None

    # ---- 目标 ----
    @property
    def target(self) -> int | None:
        with self._tlock:
            return self._target

    def set_target(self, port: int | None, motd: str = "") -> None:
        with self._tlock:
            changed = port != self._target
            self._target = port
        if not changed:
            return
        if port is None:
            log("转发目标已失效，等待下一局「对局域网开放」…")
            if self.detector:
                self.detector.reset()
        else:
            extra = f"  世界: {motd}" if motd else ""
            log(f"检测到 MC 局域网端口 → 转发 127.0.0.1:{port}{extra}")

    def _drop_target(self) -> None:
        with self._tlock:
            self._target = None
        if self.detector:
            self.detector.reset()

    # ---- 服务 ----
    def start(self) -> None:
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, peer = self.srv.accept()
            except OSError:
                return
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            tgt = self.target
            if tgt is None:
                log(f"拒绝 {peer[0]}：还没检测到 MC 的局域网端口（请先在游戏里「对局域网开放」）")
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            threading.Thread(
                target=self._handle, args=(conn, peer[0], tgt), daemon=True
            ).start()

    def _handle(self, conn: socket.socket, peer_ip: str, tgt: int) -> None:
        try:
            up = socket.create_connection(("127.0.0.1", tgt), timeout=8)
        except OSError as e:
            log(f"{peer_ip} 转发到 127.0.0.1:{tgt} 失败({e})，重新检测端口…")
            try:
                conn.close()
            except OSError:
                pass
            self._drop_target()
            return
        try:
            up.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        log(f"{peer_ip} 已接入 → MC(127.0.0.1:{tgt})")
        n = _pump(conn, up)
        log(f"{peer_ip} 断开，共转发 {n} 字节")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    # 终端里沿用控制台自己的编码(cp936/utf-8 都能正常显示中文)；
    # 被重定向/管道时改用 UTF-8，免得各种工具读到乱码。
    try:
        if sys.stdout.isatty():
            sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
        else:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

    ap = argparse.ArgumentParser(
        description="给 Minecraft「对局域网开放」配一个固定端口"
    )
    ap.add_argument("--listen", type=int, default=25565, help="本机固定监听端口(默认 25565)")
    ap.add_argument("--bind", default="0.0.0.0", help="监听地址(默认 0.0.0.0，即所有网卡)")
    ap.add_argument("--target", type=int, default=None, help="手动指定 MC 局域网端口，跳过自动检测")
    ap.add_argument("--no-scan", action="store_true", help="关掉 netstat 兜底扫描")
    args = ap.parse_args()

    try:
        bridge = Bridge(args.bind, args.listen)
    except OSError as e:
        log(f"监听 {args.bind}:{args.listen} 失败: {e}")
        log("端口可能被占用了，用 --listen 换一个，例如 --listen 25566")
        return 1

    bridge.start()

    print()
    print("=" * 60)
    print("  MC 局域网固定端口助手")
    print("=" * 60)
    print(f"  固定端口: {bridge.port}")
    if args.target:
        bridge.set_target(args.target)
        print(f"  手动目标: 127.0.0.1:{args.target}")
    else:
        det = Detector(
            on_port=lambda p, m: bridge.set_target(p, m),
            ignore_ports={bridge.port},
            allow_scan=not args.no_scan,
        )
        bridge.detector = det
        det.start()
        print("  自动检测: 开好「对局域网开放」即可")
    print()
    print("  把下面某个地址发给朋友(客户端用「直接连接」):")
    ips = local_ipv4s()
    if not ips:
        print("    (没找到本机 IP，用 ipconfig 看一眼)")
    for ip in ips:
        tag = ""
        if ip.startswith("10.") or ip.startswith("100.") or ip.startswith("172."):
            tag = "   <- 虚拟局域网地址，多半是这个"
        print(f"    {ip}:{bridge.port}{tag}")
    print()
    print("  注意: 朋友连之前，这台机器必须先「对局域网开放」。")
    print("  按 Ctrl+C 退出。")
    print("=" * 60)
    print(flush=True)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print()
        log("退出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
