"""Start the local dashboard and open it in the default browser."""

import os
import socket
import threading
import time
import urllib.error
import urllib.request
import webbrowser

import uvicorn


def available_port(preferred: int = 4001) -> int:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]


def open_browser_when_ready(address: str) -> None:
    health_url = address + "/health"
    for _ in range(50):
        try:
            with urllib.request.urlopen(health_url, timeout=1) as response:
                if response.status == 200:
                    break
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    else:
        print(f"服務未就緒，請稍後手動開啟 {address}", flush=True)
        return

    try:
        if os.name == "nt":
            os.startfile(address)
        elif not webbrowser.open(address):
            raise OSError("No browser accepted the URL")
        print(f"已開啟瀏覽器：{address}", flush=True)
    except OSError as exc:
        print(f"無法自動開啟瀏覽器（{exc}），請手動開啟 {address}", flush=True)


if __name__ == "__main__":
    port = available_port()
    address = f"http://127.0.0.1:{port}"
    print(f"蝦幣工作台：{address}", flush=True)
    threading.Thread(target=open_browser_when_ready, args=(address,), daemon=True).start()
    uvicorn.run("main:app", host="127.0.0.1", port=port)
