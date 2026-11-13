#!/usr/bin/env python3
"""Smoke test: run the app exactly as installed from the .fpk.

Simulates the NAS layout: TRIM_APPDEST points at a dir with app/app.py,
TRIM_PKGVAR is the persistent data dir, cmd/main start|status|stop is
used to control the service. A mock ADB TV answers on 127.0.0.1.
"""
import http.cookiejar
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "tests"))
import test_server as ts  # noqa: E402  (reuse the mock ADB server)

PKG = "/tmp/mitv-pkg"
INST = "/tmp/mitv-inst"
DATA = "/tmp/mitv-data"
PORT = 18091


def sh(*args, env=None):
    full_env = dict(os.environ)
    # cmd/main 用 TRIM_APPDEST 定位 app.py 所在的目录（即 app/ 本身），
    # 用 TRIM_PKGVAR 定位数据目录。import test_server 时设进来的
    # MITVREMOTE_DATA_DIR / MITVREMOTE_PORT 会盖掉它们，必须先清掉，
    # 否则子进程用的还是测试用的临时目录与端口。
    full_env.pop("MITVREMOTE_DATA_DIR", None)
    full_env.pop("MITVREMOTE_PORT", None)
    full_env.update({
        "TRIM_APPDEST": os.path.join(INST, "app"),
        "TRIM_PKGVAR": DATA,
        "TRIM_SERVICE_PORT": str(PORT),
    })
    if env:
        full_env.update(env)
    return subprocess.run(args, env=full_env, capture_output=True, text=True)


def call_api(method, path, body=None, opener=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (PORT, path), data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def main():
    # 1. 还原“已安装”目录结构：INST/app/app.py + INST/cmd/*
    subprocess.run(["rm", "-rf", INST], check=True)
    os.makedirs(os.path.join(INST, "app"), exist_ok=True)
    os.makedirs(os.path.join(INST, "cmd"), exist_ok=True)
    subprocess.run(["cp", "-r", os.path.join(PKG, "app-extracted") + "/.",
                    os.path.join(INST, "app")], check=True)
    subprocess.run(["cp", "-r", os.path.join(PKG, "cmd") + "/.",
                    os.path.join(INST, "cmd")], check=True)
    # 2. mock 电视
    mock = ts.MockAdbServer("authorized").start()
    print("mock adb on 127.0.0.1:%d" % mock.port)
    # 3. cmd/main start
    r = sh("sh", os.path.join(INST, "cmd", "main"), "start")
    print("main start rc=%d out=%s err=%s" % (r.returncode, r.stdout, r.stderr))
    assert r.returncode == 0, "main start failed"
    time.sleep(1)
    r = sh("sh", os.path.join(INST, "cmd", "main"), "status")
    print("main status rc=%d (0=running)" % r.returncode)
    assert r.returncode == 0, "service not running after start"
    # 4. 走 HTTP：登录 -> 配电视 -> 测试连接 -> 按键
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar))
    # GET /login 返回 HTML（不是 JSON），直接用 opener 读状态码
    req = urllib.request.Request("http://127.0.0.1:%d/login" % PORT)
    with opener.open(req, timeout=30) as resp:
        login_status = resp.status
        login_body = resp.read()
    print("GET /login ->", login_status)
    assert login_status == 200 and b"admin" in login_body
    status, data = call_api("POST", "/api/login",
                        {"username": "admin", "password": "admin"},
                        opener)
    print("login ->", status, data)
    assert status == 200 and data.get("ok")
    status, data = call_api("POST", "/api/settings",
                        {"tv_name": "客厅电视", "tv_ip": "127.0.0.1",
                         "tv_port": mock.port, "tv_mac": "AA:BB:CC:DD:EE:FF"},
                        opener)
    print("settings ->", status, data)
    assert data.get("ok")
    status, data = call_api("POST", "/api/test", {}, opener)
    print("test ->", status, data)
    assert data.get("ok") and data.get("model") == "MiTV-MockModel"
    before = len(mock.commands)
    status, data = call_api("POST", "/api/key", {"key": "vol_up"}, opener)
    print("key vol_up ->", status, data,
          "mock got:", mock.commands[before:])
    assert data.get("ok")
    assert mock.commands[before:] == ["input keyevent KEYCODE_VOLUME_UP"]
    # 4b. 截图接口：从已安装布局取一帧 PNG
    req = urllib.request.Request(
        "http://127.0.0.1:%d/api/screenshot" % PORT)
    with opener.open(req, timeout=30) as resp:
        shot_status = resp.status
        shot_ct = resp.headers.get("Content-Type", "")
        shot_body = resp.read()
    print("screenshot ->", shot_status, shot_ct, "len=%d" % len(shot_body))
    assert shot_status == 200 and shot_ct.startswith("image/png")
    assert shot_body[:8] == b"\x89PNG\r\n\x1a\n"
    assert mock.commands[before + 1:] == ["screencap -p"]
    # 5. main stop + status 应为 3（未运行）
    r = sh("sh", os.path.join(INST, "cmd", "main"), "stop")
    print("main stop rc=%d" % r.returncode)
    assert r.returncode == 0
    r = sh("sh", os.path.join(INST, "cmd", "main"), "status")
    print("main status after stop rc=%d (want 3)" % r.returncode)
    assert r.returncode == 3
    mock.stop()
    print("SMOKE OK: installed-layout start/status/stop + login + "
          "settings + test + key all work from the .fpk contents")


if __name__ == "__main__":
    main()
