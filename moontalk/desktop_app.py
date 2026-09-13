#!/usr/bin/env python3
"""Moontalk Windows desktop launcher.

Starts the Flask backend in a daemon thread and wraps it in a native pywebview
window, matching the desktop startup model used by Susei.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.request

import webview

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 8010
HOST = "127.0.0.1"
URL = f"http://{HOST}:{PORT}/"

sys.path.insert(0, ROOT)
os.environ["MOONTALK_DESKTOP_MODE"] = "1"

import app as flask_app

_flask_thread: threading.Thread | None = None
_server_ready = threading.Event()


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((HOST, port))
            return False
        except OSError:
            return True


def start_flask():
    global _flask_thread
    if _flask_thread and _flask_thread.is_alive():
        return

    def run_server():
        try:
            flask_app.app.run(host=HOST, port=PORT, debug=False, threaded=True)
        except Exception as exc:
            print(f"Moontalk Flask 启动失败: {exc}", flush=True)

    _flask_thread = threading.Thread(target=run_server, daemon=True)
    _flask_thread.start()

    for _ in range(30):
        try:
            with urllib.request.urlopen(URL, timeout=1):
                _server_ready.set()
                print(f"Moontalk 后端已就绪: {URL}", flush=True)
                return
        except Exception:
            time.sleep(0.5)

    _server_ready.set()
    print("Moontalk 后端启动等待超时，继续创建窗口。", flush=True)


def create_main_window():
    return webview.create_window(
        "Moontalk",
        URL,
        width=1180,
        height=800,
        x=160,
        y=60,
        min_size=(860, 560),
        resizable=True,
        frameless=False,
        transparent=False,
        text_select=True,
        confirm_close=False,
        focus=True,
    )


def main():
    if port_in_use(PORT):
        print(
            f"Moontalk 已在运行: {URL}\n"
            "如需重启，请先关闭已有 Moontalk 窗口/进程。",
            flush=True,
        )
        return

    print("=" * 60, flush=True)
    print("  Moontalk - Windows 桌面应用", flush=True)
    print("  Flask 后端 + pywebview 原生窗口", flush=True)
    print("=" * 60, flush=True)

    start_flask()
    print("创建 Moontalk 主窗口...", flush=True)
    create_main_window()
    webview.start(
        debug=False,
        http_server=False,
        private_mode=False,
        storage_path=os.path.join(ROOT, ".desktop_cache"),
    )


if __name__ == "__main__":
    main()
