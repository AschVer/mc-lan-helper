#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MC 联机助手 —— Minecraft Java 版异地联机（谁都能当房主）

一套程序两边通用：
  · 房主点「① 我开房间」→ 起 EasyTier + 固定端口转发，界面显示该发给对方的地址
  · 对方点「② 加入房间」→ 起 EasyTier，自动发现房主的 IP，界面显示该填的地址

依赖（放同目录）：
  easytier-core.exe / easytier-cli.exe / wintun.dll / Packet.dll / WinDivert64.sys
  mc-lan-helper.py  （端口转发逻辑，复用同一个文件）

配置：
  config.json   —— 房间配置（中继地址、网络名、密钥），两边必须一致
  mclink.json   —— 本机状态（虚拟 IP），自动生成，不用管

用法：双击 启动.bat
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox
import zlib

# ---------------------------------------------------------------- 常量

BASE = os.path.dirname(os.path.abspath(__file__))
ROOM_CONFIG = os.path.join(BASE, "config.json")
STATE_PATH = os.path.join(BASE, "mclink.json")

DEFAULT_ROOM = {
    "network_name": "change-me",
    "network_secret": "change-me-too",
    "relay": "tcp://your-relay.example.com:11010",
    "vpn_prefix": "10.126.126.",
    "bridge_port": 25565,
}

BG = "#11151c"
CARD = "#1a2130"
FG = "#e6edf7"
DIM = "#8b9bb4"
ACCENT = "#3ba55d"
ACCENT2 = "#5865f2"
WARN = "#e3b341"
ERR = "#e06c75"

CREATE_NO_WINDOW = 0x08000000


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> None:
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    script = os.path.abspath(__file__)
    r = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, f'"{script}"', BASE, 1)
    sys.exit(0 if r > 32 else 1)


# ---------------------------------------------------------------- 配置

def _is_placeholder(v: str) -> bool:
    v = (v or "").strip().lower()
    return (not v) or ("change-me" in v) or ("your-relay" in v) or ("example.com" in v)


def load_room_config() -> tuple[dict, bool]:
    """config.json：房间配置。返回 (配置, 是否已正确填写)。"""
    cfg = dict(DEFAULT_ROOM)
    if os.path.exists(ROOM_CONFIG):
        try:
            with open(ROOM_CONFIG, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
    else:
        try:
            with open(ROOM_CONFIG, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_ROOM, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    ok = not any(_is_placeholder(str(cfg.get(k, "")))
                 for k in ("network_name", "network_secret", "relay"))
    return cfg, ok


def load_state(vpn_prefix: str) -> str:
    """mclink.json：本机的固定虚拟 IP，首次运行按主机名散列生成。"""
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                ip = json.load(f).get("vpn_ip", "")
            if ip.startswith(vpn_prefix):
                return ip
        except (OSError, ValueError):
            pass

    seed = zlib.crc32((socket.gethostname() + "mclink").encode("utf-8"))
    ip = f"{vpn_prefix}{11 + seed % 200}"
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"vpn_ip": ip}, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return ip


# ---------------------------------------------------------------- EasyTier

class EasyTier:
    def __init__(self, room: dict, vpn_ip: str):
        self.room = room
        self.vpn_ip = vpn_ip
        self.core = os.path.join(BASE, "easytier-core.exe")
        self.cli = os.path.join(BASE, "easytier-cli.exe")
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        args = [
            self.core,
            "--network-name", str(self.room["network_name"]),
            "--network-secret", str(self.room["network_secret"]),
            "--ipv4", self.vpn_ip,
            "--peers", str(self.room["relay"]),
            "--hostname", socket.gethostname(),
            "--enable-udp-broadcast-relay", "true",
        ]
        self.proc = subprocess.Popen(
            args, cwd=BASE, creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=6)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    p.kill()
                except OSError:
                    pass

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def peers(self) -> list[dict]:
        """读 easytier-cli peer 的表格。"""
        try:
            out = subprocess.run(
                [self.cli, "peer"], cwd=BASE, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=10,
                creationflags=CREATE_NO_WINDOW,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return []

        rows = []
        for line in out.splitlines():
            s = line.strip()
            if not s.startswith("|") or set(s) <= {"|", "-", " "}:
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) < 4 or cells[0].lower() == "ipv4":
                continue
            rows.append({
                "ipv4": cells[0],
                "hostname": cells[1],
                "cost": cells[2] if len(cells) > 2 else "",
                "latency": cells[3] if len(cells) > 3 else "",
            })
        return rows

    def remote_peer(self) -> dict | None:
        """找出对面那台真机（跳过中继节点和自己）。"""
        for r in self.peers():
            ip = r["ipv4"].split("/")[0].strip()
            if ip and ip.startswith(self.vpn_ip.rsplit(".", 1)[0] + ".") and ip != self.vpn_ip:
                return r
        return None

    def tunnel_ready(self) -> bool:
        try:
            addrs = subprocess.run(
                ["ipconfig"], capture_output=True, text=True,
                encoding="oem", errors="replace", timeout=10,
                creationflags=CREATE_NO_WINDOW,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return self.vpn_ip in addrs


# ---------------------------------------------------------------- 端口转发

def load_bridge_module():
    """复用 mc-lan-helper.py 里已经验证过的转发实现。"""
    path = os.path.join(BASE, "mc-lan-helper.py")
    spec = importlib.util.spec_from_file_location("mclan_bridge", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 界面

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.room, self.room_ok = load_room_config()
        self.vpn_ip = load_state(str(self.room.get("vpn_prefix", "10.126.126.")))
        self.et = EasyTier(self.room, self.vpn_ip)
        self.bridge_mod = load_bridge_module()
        self.bridge = None
        self.detector = None
        self.mode: str | None = None      # "host" / "guest"
        self.remote: dict | None = None
        self._poll_job = None
        self._busy = False
        self._shown_addr: str | None = None
        self._host_peer_ip: str | None = None
        self._warned_conflict = False

        self.bridge_mod.log = self.log   # 让转发模块的日志进界面

        self._build_ui()
        self.log(f"本机固定虚拟 IP：{self.vpn_ip}")
        if not self.room_ok:
            self._config_warning()
        self._schedule_poll()

    # ---------------- UI 搭建

    def _build_ui(self) -> None:
        r = self.root
        r.title("MC 联机助手")
        r.configure(bg=BG)
        r.geometry("620x690")
        r.minsize(560, 640)

        ui = "Microsoft YaHei UI"
        self.f_big = tkfont.Font(family=ui, size=17, weight="bold")
        self.f_h1 = tkfont.Font(family=ui, size=15, weight="bold")
        self.f_body = tkfont.Font(family=ui, size=10)
        self.f_small = tkfont.Font(family=ui, size=9)
        self.f_addr = tkfont.Font(family="Consolas", size=24, weight="bold")
        self.f_btn = tkfont.Font(family=ui, size=12, weight="bold")

        pad = {"padx": 18}

        top = tk.Frame(r, bg=BG)
        top.pack(fill="x", pady=(16, 2), **pad)
        tk.Label(top, text="MC 联机助手", font=self.f_h1, bg=BG, fg=FG).pack(side="left")
        self.btn_cfg = tk.Button(
            top, text="⚙ 配置", font=self.f_small, bg="#2a3446", fg=DIM,
            activebackground="#35425a", activeforeground=FG, relief="flat",
            bd=0, cursor="hand2", command=self.open_config)
        self.btn_cfg.pack(side="right", ipadx=8, ipady=2)

        self.lbl_sub = tk.Label(
            r, text=f"本机 IP {self.vpn_ip}", font=self.f_small,
            bg=BG, fg=DIM)
        self.lbl_sub.pack(anchor="w", **pad)

        # 状态
        self.lbl_status = tk.Label(
            r, text="● 未连接", font=self.f_body, bg=BG, fg=DIM)
        self.lbl_status.pack(anchor="w", pady=(10, 0), **pad)

        # 按钮
        bf = tk.Frame(r, bg=BG)
        bf.pack(fill="x", pady=12, **pad)
        self.btn_host = tk.Button(
            bf, text="① 我开房间", font=self.f_btn, bg=ACCENT, fg="white",
            activebackground="#2f8a4d", activeforeground="white",
            relief="flat", bd=0, cursor="hand2", command=self.start_host)
        self.btn_host.pack(side="left", expand=True, fill="x", ipady=12, padx=(0, 6))
        self.btn_guest = tk.Button(
            bf, text="② 加入房间", font=self.f_btn, bg=ACCENT2, fg="white",
            activebackground="#4752c4", activeforeground="white",
            relief="flat", bd=0, cursor="hand2", command=self.start_guest)
        self.btn_guest.pack(side="left", expand=True, fill="x", ipady=12, padx=(6, 0))

        # 地址卡片
        card = tk.Frame(r, bg=CARD)
        card.pack(fill="x", **pad)
        self.lbl_addr_hint = tk.Label(
            card, text="地址", font=self.f_small, bg=CARD, fg=DIM)
        self.lbl_addr_hint.pack(anchor="w", padx=14, pady=(12, 0))
        self.lbl_addr = tk.Label(
            card, text="—", font=self.f_addr, bg=CARD, fg="#5ee08a")
        self.lbl_addr.pack(anchor="w", padx=14)
        self.lbl_addr_note = tk.Label(
            card, text="先选上面一个模式", font=self.f_small, bg=CARD, fg=DIM,
            justify="left", wraplength=540)
        self.lbl_addr_note.pack(anchor="w", padx=14, pady=(2, 10))

        self.btn_copy = tk.Button(
            card, text="复制地址", font=self.f_small, bg="#2a3446", fg=FG,
            activebackground="#35425a", activeforeground=FG, relief="flat",
            bd=0, cursor="hand2", command=self.copy_addr, state="disabled")
        self.btn_copy.pack(anchor="w", padx=14, pady=(0, 12), ipadx=10, ipady=3)

        # 日志
        lf = tk.Frame(r, bg=BG)
        lf.pack(fill="both", expand=True, pady=(10, 0), **pad)
        self.txt = tk.Text(
            lf, bg="#0c1017", fg="#a9b7cc", font=self.f_small, relief="flat",
            height=9, wrap="word", state="disabled", padx=8, pady=8)
        self.txt.pack(fill="both", expand=True)

        # 底部
        self.btn_stop = tk.Button(
            r, text="停止 / 断开", font=self.f_body, bg="#2a3446", fg=FG,
            activebackground="#35425a", activeforeground=FG, relief="flat",
            bd=0, cursor="hand2", command=self.stop_all, state="disabled")
        self.btn_stop.pack(fill="x", pady=12, ipady=7, **pad)
        self._polish()

    def _polish(self) -> None:
        """居中 + 深色标题栏（跟界面配色一致）。"""
        r = self.root
        r.update_idletasks()
        w, h = max(r.winfo_width(), 620), max(r.winfo_height(), 690)
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        r.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        try:
            hwnd = ctypes.windll.user32.GetParent(r.winfo_id())
            on = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(on), ctypes.sizeof(on))
        except Exception:
            pass

    # ---------------- 小工具

    def log(self, msg: str) -> None:
        def _do():
            self.txt.configure(state="normal")
            self.txt.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
            self.txt.see("end")
            self.txt.configure(state="disabled")
        try:
            self.root.after(0, _do)
        except Exception:
            pass

    def set_status(self, text: str, color: str = DIM) -> None:
        self.lbl_status.configure(text="● " + text, fg=color)

    def set_addr(self, addr: str | None, hint: str = "", note: str = "") -> None:
        self.lbl_addr.configure(text=addr or "—")
        self.lbl_addr_hint.configure(text=hint)
        self.lbl_addr_note.configure(text=note)
        self.btn_copy.configure(state="normal" if addr else "disabled")

    def copy_addr(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.lbl_addr.cget("text"))
        self.log("地址已复制到剪贴板")

    def open_config(self) -> None:
        try:
            os.startfile(ROOM_CONFIG)  # noqa: S606
        except OSError as e:
            messagebox.showerror("打不开配置", f"{ROOM_CONFIG}\n\n{e}")

    def _config_warning(self) -> None:
        self.set_status("还没配置", ERR)
        self.log(f"缺少配置：{ROOM_CONFIG}")
        self.log("请填好 network_name / network_secret / relay 后重启本程序")
        self.log("relay 填你自己的 EasyTier 中继节点，例如 tcp://1.2.3.4:11010")
        self.root.after(400, lambda: messagebox.showwarning(
            "还没配置",
            "config.json 还没填。\n\n"
            f"文件位置：\n{ROOM_CONFIG}\n\n"
            "需要填三项：\n"
            "  network_name    网络名（两台机器一致）\n"
            "  network_secret  网络密钥（两台机器一致）\n"
            "  relay           你的 EasyTier 中继地址\n\n"
            "填好后重启本程序。详见 README。"))

    def _set_buttons(self, enabled: bool) -> None:
        # 不用 state=disabled（Windows 下会把彩色按钮变灰、文字看不清），
        # 改成换配色，点击由 _busy/mode 拦住。
        if enabled:
            self.btn_host.configure(bg=ACCENT, fg="white")
            self.btn_guest.configure(bg=ACCENT2, fg="white")
            self.btn_stop.configure(state="disabled", fg=DIM)
        else:
            self.btn_host.configure(bg="#243040", fg=DIM)
            self.btn_guest.configure(bg="#243040", fg=DIM)
            self.btn_stop.configure(state="normal", fg=FG)

    # ---------------- 房主

    def start_host(self) -> None:
        if self._busy or self.mode or not self._check_room():
            return
        self._busy = True
        self.mode = "host"
        self._set_buttons(False)
        self.set_status("正在启动联机通道…", WARN)
        self.set_addr(None, "地址", "正在连接中继服务器…")
        threading.Thread(target=self._host_worker, daemon=True).start()

    def _host_worker(self) -> None:
        try:
            self.log("启动 EasyTier…")
            self.et.start()
            if not self._wait_tunnel():
                self._fail("通道没起来：检查中继地址、网络名密钥，或杀软拦截")
                return

            self.log(f"通道已建立，本机虚拟 IP {self.et.vpn_ip}")
            port = self._start_bridge()
            if not port:
                return

            addr = f"{self.et.vpn_ip}:{port}"
            self.root.after(0, lambda: self.set_status("房主模式：等她连进来", ACCENT))
            self.root.after(0, lambda: self.set_addr(
                addr, "把这个发给她",
                "她点「② 加入房间」；游戏里「直接连接」填这行。\n"
                "你的操作：Esc → 对局域网开放 → 端口号那一栏【留空】\n"
                f"→ 创建局域网世界（别填 {port}，那个端口被本程序占着）"))
            self.log(f"让她连接：{addr}")
            self.log(f"提醒：MC 的「端口号」要留空，别填 {port}（已被本程序占用）")
            self.log("现在去游戏里：进世界 → Esc → 对局域网开放")
        finally:
            self._busy = False

    # ---------------- 客人

    def start_guest(self) -> None:
        if self._busy or self.mode or not self._check_room():
            return
        self._busy = True
        self.mode = "guest"
        self._set_buttons(False)
        self.set_status("正在加入…", WARN)
        self.set_addr(None, "地址", "正在等待房主上线…")
        threading.Thread(target=self._guest_worker, daemon=True).start()

    def _guest_worker(self) -> None:
        try:
            self.log("启动 EasyTier…")
            self.et.start()
            if not self._wait_tunnel():
                self._fail("通道没起来：检查中继地址、网络名密钥，或杀软拦截")
                return

            self.log("通道已建立，正在找房主…")
            deadline = time.time() + 60
            while time.time() < deadline:
                peer = self.et.remote_peer()
                if peer:
                    self.remote = peer
                    self._show_guest_addr(peer)
                    return
                time.sleep(1.5)

            self.root.after(0, lambda: self.set_status("房主还没上线", WARN))
            self.root.after(0, lambda: self.set_addr(
                None, "地址", "60 秒内没找到房主。\n让她先点「① 我开房间」，再点这里。"))
        finally:
            self._busy = False

    def _show_guest_addr(self, peer: dict) -> None:
        ip = peer["ipv4"].split("/")[0].strip()
        if not ip or ip == self._shown_addr:
            return
        self._shown_addr = ip
        lat = peer["latency"]
        lat_txt = f"  延迟 {lat} ms" if lat not in ("", "-") else ""
        addr = f"{ip}:{self.bridge_port}"
        self.root.after(0, lambda: self.set_status(f"已找到房主{lat_txt}", ACCENT))
        self.root.after(0, lambda: self.set_addr(
            addr, "填到「直接连接」里",
            f"房主是 {peer['hostname'] or ip}。\n"
            "游戏里：多人游戏 → 直接连接 → 输入这行。"))
        self.log(f"找到房主 {ip}（{peer['hostname']}）{lat_txt}")
        self.log(f"在游戏里连：{addr}")

    # ---------------- 公共流程

    @property
    def bridge_port(self) -> int:
        try:
            return int(self.room.get("bridge_port", 25565))
        except (TypeError, ValueError):
            return 25565

    def _check_room(self) -> bool:
        if self.room_ok:
            return True
        self._config_warning()
        return False

    def _wait_tunnel(self, timeout: float = 30) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if not self.et.alive():
                return False
            if self.et.tunnel_ready():
                return True
            time.sleep(0.8)
        return False

    def _start_bridge(self) -> int | None:
        base = self.bridge_port
        for candidate in (base, base + 1, base + 2):
            try:
                self.bridge = self.bridge_mod.Bridge("0.0.0.0", candidate)
                break
            except OSError:
                self.bridge = None
        if not self.bridge:
            self._fail(f"{base}~{base + 2} 都被占用了，先关掉占用它的程序")
            return None

        port = self.bridge.port
        self.bridge.start()
        self.detector = self.bridge_mod.Detector(
            on_port=self._on_mc_port,
            ignore_ports={port},
            allow_scan=True,
        )
        self.bridge.detector = self.detector
        self.detector.start()
        self.log(f"端口转发已就绪：0.0.0.0:{port} → MC 局域网端口")
        return port

    def _on_mc_port(self, port: int, motd: str) -> None:
        msg = f"识别到 MC 局域网端口 {port}"
        if motd:
            msg += f"（{motd}）"
        self.log(msg)

    def _fail(self, msg: str) -> None:
        self.log("失败：" + msg)
        self.root.after(0, lambda: self.set_status(msg, ERR))
        self.root.after(0, lambda: self._set_buttons(True))
        self.stop_all(keep_log=True)

    # ---------------- 轮询 / 停止

    def _schedule_poll(self) -> None:
        self._poll_job = self.root.after(4000, self._poll)

    def _poll(self) -> None:
        # 查对端要跑 easytier-cli，别放在 UI 线程里，否则界面会一顿一顿的
        if self.mode and self.et.alive():
            threading.Thread(target=self._poll_worker, daemon=True).start()
        self._schedule_poll()

    def _poll_worker(self) -> None:
        mode = self.mode
        try:
            peers = self.et.peers()
        except Exception:
            return
        if mode != self.mode:
            return

        # 两台机器撞了同一个虚拟 IP
        if not self._warned_conflict:
            dup = [p for p in peers
                   if p["cost"].lower() != "local"
                   and p["ipv4"].split("/")[0].strip() == self.et.vpn_ip]
            if dup:
                self._warned_conflict = True
                self.log("检测到 IP 冲突：对方和本机是同一个虚拟 IP")
                self.log("解决：删掉 mclink.json 后重启，本机会重新分配一个")

        peer = None
        prefix = self.et.vpn_ip.rsplit(".", 1)[0] + "."
        for p in peers:
            ip = p["ipv4"].split("/")[0].strip()
            if ip and ip.startswith(prefix) and ip != self.et.vpn_ip:
                peer = p
                break

        if not peer:
            return
        if mode == "guest":
            self._show_guest_addr(peer)
        elif mode == "host":
            self._update_host_peer(peer)

    def _update_host_peer(self, peer: dict) -> None:
        ip = peer["ipv4"].split("/")[0].strip()
        if not ip or ip == self._host_peer_ip:
            return
        self._host_peer_ip = ip
        lat = peer["latency"]
        lat_txt = f"  延迟 {lat} ms" if lat not in ("", "-") else ""
        who = peer["hostname"] or ip
        self.log(f"对方上线：{who}（{ip}）{lat_txt}")
        self.root.after(0, lambda: self.set_status(f"房主模式：{who} 已进来{lat_txt}", ACCENT))
        self.root.after(0, lambda: self.lbl_sub.configure(
            text=f"本机 IP {self.et.vpn_ip}   已连接：{ip}"))

    def stop_all(self, keep_log: bool = False) -> None:
        self.mode = None
        self.remote = None
        self._shown_addr = None
        self._host_peer_ip = None
        try:
            self.et.stop()
        except Exception:
            pass
        if self.bridge:
            try:
                self.bridge.srv.close()
            except OSError:
                pass
            self.bridge = None
        self.detector = None
        self.set_addr(None, "地址", "先选上面一个模式")
        self.set_status("已断开", DIM)
        self.lbl_sub.configure(text=f"本机 IP {self.vpn_ip}")
        self._set_buttons(True)
        if not keep_log:
            self.log("已断开连接")


# ---------------------------------------------------------------- 自检（不开界面）

def selftest() -> int:
    room, ok = load_room_config()
    print(f"[config] ok={ok} relay={room.get('relay')} network={room.get('network_name')}")
    if not ok:
        print("[skip] config.json 还没填，跳过联机自检")
        return 0

    vpn_ip = load_state(str(room.get("vpn_prefix", "10.126.126.")))
    print(f"[cfg] vpn_ip = {vpn_ip}")
    et = EasyTier(room, vpn_ip)
    et.start()
    t0 = time.time()
    while time.time() - t0 < 30:
        if et.tunnel_ready():
            print(f"[ok] tunnel up after {time.time() - t0:.1f}s")
            break
        time.sleep(0.8)
    else:
        print("[fail] tunnel 没起来")
        et.stop()
        return 1

    print("[peers]")
    for r in et.peers():
        print("  ", r)

    m = load_bridge_module()
    print(f"[bridge] Bridge={hasattr(m, 'Bridge')} Detector={hasattr(m, 'Detector')}")

    et.stop()
    print("[ok] selftest done")
    return 0


def smoke(role: str) -> int:
    """无人工点击的端到端自检：跑真实的按钮流程。"""
    root = tk.Tk()
    app = App(root)

    def go():
        (app.start_host if role == "host" else app.start_guest)()

    def report():
        print("MODE  :", app.mode)
        print("ADDR  :", app.lbl_addr.cget("text"))
        print("STATUS:", app.lbl_status.cget("text"))
        print(" NOTE :", app.lbl_addr_note.cget("text").replace("\n", " / "))
        print(" LOG   :")
        print(app.txt.get("1.0", "end").strip())
        app.stop_all()
        root.destroy()

    root.after(800, go)
    root.after(15000, report)
    root.mainloop()
    return 0


def main() -> int:
    if not is_admin():
        relaunch_as_admin()
        return 0

    if "--selftest" in sys.argv:
        return selftest()

    if "--smoke" in sys.argv:
        i = sys.argv.index("--smoke")
        role = sys.argv[i + 1] if len(sys.argv) > i + 1 else "host"
        return smoke(role)

    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")

    root = tk.Tk()
    ico = os.path.join(BASE, "mclink.ico")
    if os.path.exists(ico):
        try:
            root.iconbitmap(ico)
        except Exception:
            pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
