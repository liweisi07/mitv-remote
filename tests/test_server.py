#!/usr/bin/env python3
"""小米电视遥控本地验证：mock ADB 服务端 + HTTP 接口。

不用真电视，验证：
  1. 密钥生成（纯 Python rsa）与 ADB 握手（已授权 / 等待点允许两种模式）
  2. 网页 UI 能打开、登录、设置保存
  3. 遥控按键确实把 `input keyevent ...` 发到了 ADB 服务端
  4. 连接状态区分：connected / refused / awaiting_auth / not_configured
  5. WoL 幻数据包格式（FF*6 + MAC*16）与 MAC 校验
  6. 发现扫描逻辑（mock 在 127.0.0.1 网段）
  7. 截图接口：exec-out 取合法 PNG、exec 失效时 base64 兜底、
     失败回可读错误、服务端并发节流（busy 锁）

运行：python3 tests/test_server.py
"""
import base64
import http.client
import http.cookiejar
import importlib.util
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP_DIR = os.path.join(ROOT, "app")

# ---------------------------------------------------------------------------
# Mock ADB 服务端：实现 ADB 握手与 shell 服务的最小子集
# ---------------------------------------------------------------------------

CNXN, AUTH, OPEN, OKAY, WRTE, CLSE = (
    b"CNXN", b"AUTH", b"OPEN", b"OKAY", b"WRTE", b"CLSE")
AUTH_TOKEN, AUTH_SIGNATURE, AUTH_RSAPUBLICKEY = 1, 2, 3

# 1x1 透明 PNG（mock 截图用，合法 PNG 魔数头）
MOCK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _adb_msg(cmd, arg0, arg1, payload=b""):
    checksum = sum(payload) & 0xFFFFFFFF
    magic = struct.unpack("<I", cmd)[0] ^ 0xFFFFFFFF
    return struct.pack("<6I", struct.unpack("<I", cmd)[0], arg0, arg1,
                       len(payload), checksum, magic) + payload


def _read_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("mock adb: connection closed")
        buf += chunk
    return buf


class MockAdbServer:
    """mode: authorized | auth_never | auth_allow"""

    def __init__(self, mode):
        self.mode = mode
        self.commands = []  # 收到的 shell 命令
        self.connections = 0  # 完成握手进入 shell 服务的 TCP 连接数
        self._conn_lock = threading.Lock()
        # mock 电视上的虚拟文件系统：路径 -> 内容（落文件截图用）
        self.files = {}
        self._files_lock = threading.Lock()
        # 截图延迟（秒）：模拟真机 screencap 慢
        self.screencap_delay = 0.0
        # 截图模式：ok（exec-out 返回合法 PNG）| fallback（exec 返回
        # 垃圾，shell base64 兜底）| file_only（exec 与 base64 都失
        # 效，只有落文件再读能拿到图）| fail（几条路都拿不到合法 PNG）
        self.screencap_mode = "ok"
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(30)
            # 1. 握手
            header = _read_exact(conn, 24)
            cmd, _a0, _a1, length, _cs, _magic = struct.unpack("<6I", header)
            payload = _read_exact(conn, length) if length else b""
            if cmd != struct.unpack("<I", CNXN)[0]:
                return
            connected = False
            if self.mode == "authorized":
                conn.sendall(_adb_msg(
                    CNXN, 0x01000001, 4096, b"device::mock\x00"))
                connected = True
            else:
                token = b"0123456789abcdefghij"
                conn.sendall(_adb_msg(AUTH, AUTH_TOKEN, 0, token))
                # 2. 等签名，再发一次 token（触发客户端发公钥）
                header = _read_exact(conn, 24)
                cmd, a0, _a1, length, _cs, _m = struct.unpack("<6I", header)
                payload = _read_exact(conn, length) if length else b""
                assert cmd == struct.unpack("<I", AUTH)[0] and a0 == AUTH_SIGNATURE, \
                    "mock adb: expected AUTH signature, got %r arg0=%s" % (cmd, a0)
                conn.sendall(_adb_msg(AUTH, AUTH_TOKEN, 0, token))
                # 3. 等公钥
                header = _read_exact(conn, 24)
                cmd, a0, _a1, length, _cs, _m = struct.unpack("<6I", header)
                payload = _read_exact(conn, length) if length else b""
                assert cmd == struct.unpack("<I", AUTH)[0] and a0 == AUTH_RSAPUBLICKEY, \
                    "mock adb: expected AUTH pubkey, got %r arg0=%s" % (cmd, a0)
                if self.mode == "auth_allow":
                    conn.sendall(_adb_msg(
                        CNXN, 0x01000001, 4096, b"device::mock\x00"))
                    connected = True
                else:
                    # auth_never：不回应，客户端应以 AdbTimeoutError 超时
                    # （且 auth_callback 已触发 -> awaiting_auth）
                    time.sleep(35)
                    return
            if not connected:
                return
            with self._conn_lock:
                self.connections += 1
            # 4. shell 服务循环
            remote_id = 1
            while True:
                header = _read_exact(conn, 24)
                cmd, a0, _a1, length, _cs, _m = struct.unpack(
                    "<6I", header)
                payload = _read_exact(conn, length) if length else b""
                cmd_bytes = struct.pack("<I", cmd)
                if cmd_bytes == OPEN:
                    dest = payload.rstrip(b"\x00").decode("utf-8", "replace")
                    if dest.startswith("shell:"):
                        command = dest[len("shell:"):]
                        kind = "shell"
                    elif dest.startswith("exec:"):
                        command = dest[len("exec:"):]
                        kind = "exec"
                    else:
                        raise AssertionError(
                            "mock adb: unexpected service %r" % dest)
                    self.commands.append(command)
                    local_id = a0
                    # 服务端回包的 arg0 是服务端自己的 remote_id、arg1 是
                    # 客户端的 local_id（adb-shell 的 args_match 就这么
                    # 认；写反了第二条流起 OKAY 就对不上号、客户端等超时）。
                    conn.sendall(_adb_msg(OKAY, remote_id, local_id))
                    if command.startswith("getprop ro.product.model"):
                        out = b"MiTV-MockModel\n"
                    elif command.startswith("screencap"):
                        # 模拟真机 screencap 慢：回包前先睡一会儿
                        if self.screencap_delay:
                            time.sleep(self.screencap_delay)
                        if kind == "exec":
                            # exec-out screencap -p
                            if self.screencap_mode == "ok":
                                out = MOCK_PNG
                            elif self.screencap_mode == "fallback":
                                out = b"not-a-png"
                            else:  # fail / file_only
                                out = b""
                        elif "| base64" in command:
                            # shell: screencap -p | base64
                            if self.screencap_mode in ("fallback", "ok"):
                                out = base64.b64encode(MOCK_PNG) + b"\n"
                            else:  # fail / file_only
                                out = b""
                        else:
                            # shell: screencap <path> 落文件（fail 模式写不出）
                            path = command[len("screencap"):].strip()
                            if self.screencap_mode != "fail" and path:
                                with self._files_lock:
                                    self.files[path] = MOCK_PNG
                            out = b""
                    elif (command.startswith("test -f ")
                            and command.rstrip().endswith("&& echo OK")):
                        path = command[len("test -f "):].split("&&")[0].strip()
                        with self._files_lock:
                            exists = path in self.files
                        out = b"OK\n" if exists else b""
                    elif command.startswith("base64 "):
                        path = command[len("base64 "):].strip()
                        with self._files_lock:
                            data = self.files.get(path)
                        out = (base64.b64encode(data) + b"\n") if data else b""
                    elif command.startswith("cat ") and kind == "exec":
                        path = command[len("cat "):].strip()
                        with self._files_lock:
                            data = self.files.get(path)
                        out = data if data else b""
                    elif command.startswith("rm -f "):
                        path = command[len("rm -f "):].strip()
                        with self._files_lock:
                            self.files.pop(path, None)
                        out = b""
                    else:
                        out = b""
                    conn.sendall(_adb_msg(WRTE, remote_id, local_id, out))
                    conn.sendall(_adb_msg(CLSE, remote_id, local_id))
                elif cmd_bytes == CLSE:
                    continue
                elif cmd_bytes == WRTE:
                    continue
                elif cmd_bytes == OKAY:
                    # 客户端收到 WRTE 后回的流控确认（ADB 协议规定），
                    # 真机 adbd 会正常收下：忽略。没有这条，同一条
                    # 连接上的第二条命令会因 mock 关连接而断掉。
                    continue
                else:
                    return
        except (ConnectionError, OSError, AssertionError):
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 载入被测 app（先设环境变量，隔离数据目录）
# ---------------------------------------------------------------------------

TMPDATA = tempfile.mkdtemp(prefix="mitvremote-test-")
os.environ["MITVREMOTE_DATA_DIR"] = TMPDATA
# 挑一个空闲端口给 HTTP 服务
_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
_probe.bind(("127.0.0.1", 0))
HTTP_PORT = _probe.getsockname()[1]
_probe.close()
os.environ["MITVREMOTE_PORT"] = str(HTTP_PORT)

spec = importlib.util.spec_from_file_location(
    "mitvremote_app", os.path.join(APP_DIR, "app.py"))
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print("%s %s %s" % ("PASS" if ok else "FAIL", label, detail))


# ---------------------------------------------------------------------------
# 单元测试：MAC / WoL（不依赖网络）
# ---------------------------------------------------------------------------

def test_mac_wol():
    check("parse_mac 冒号格式",
          app.parse_mac("AA:BB:CC:DD:EE:FF") == bytes.fromhex("AABBCCDDEEFF"))
    check("parse_mac 短横格式",
          app.parse_mac("aa-bb-cc-dd-ee-ff") == bytes.fromhex("AABBCCDDEEFF"))
    check("parse_mac 无分隔格式",
          app.parse_mac("AABBCCDDEEFF") == bytes.fromhex("AABBCCDDEEFF"))
    check("parse_mac 非法拒绝",
          app.parse_mac("ZZ:BB:CC:DD:EE:FF") is None
          and app.parse_mac("AA:BB:CC") is None
          and app.parse_mac("") is None)
    pkt = app.build_magic_packet(bytes.fromhex("AABBCCDDEEFF"))
    check("WoL 幻数据包 102 字节",
          len(pkt) == 102, "len=%d" % len(pkt))
    check("WoL 包头 6 个 FF", pkt[:6] == b"\xff" * 6)
    check("WoL 包体 MAC 重复 16 次",
          pkt[6:] == bytes.fromhex("AABBCCDDEEFF") * 16)
    # send_wol：用假 socket 捕获实际发出的包
    sent = {}

    class FakeSock:
        def __init__(self, *a, **k):
            pass

        def setsockopt(self, *a):
            pass

        def sendto(self, data, addr):
            sent["data"] = data
            sent["addr"] = addr

        def close(self):
            pass

    real_socket = socket.socket
    socket.socket = FakeSock
    try:
        ok, err = app.send_wol("AA:BB:CC:DD:EE:FF")
    finally:
        socket.socket = real_socket
    check("send_wol 成功", ok, err)
    check("send_wol 发往广播 255.255.255.255:9",
          sent.get("addr") == ("255.255.255.255", 9), str(sent.get("addr")))
    check("send_wol 发出的就是幻数据包",
          sent.get("data") == pkt)
    ok, err = app.send_wol("not-a-mac")
    check("send_wol 非法 MAC 拒绝", not ok and "MAC" in err, err)


# ---------------------------------------------------------------------------
# HTTP 测试
# ---------------------------------------------------------------------------

class Http:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (HTTP_PORT, path), data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {})
        try:
            with self.opener.open(request, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def get(self, path):
        return self.req("GET", path)

    def post(self, path, body=None):
        return self.req("POST", path, body if body is not None else {})

    def json(self, method, path, body=None):
        status, raw = self.req(method, path, body)
        try:
            return status, json.loads(raw.decode("utf-8"))
        except ValueError:
            return status, {"_raw": raw[:200]}


def test_http(mock_auth):
    h = Http()
    # 1. 未登录：页面 302 到登录页，接口 401
    status, _ = h.get("/")
    check("未登录 / 跳登录页", status in (200, 302), "status=%s" % status)
    status, data = h.json("GET", "/api/status")
    check("未登录接口回 401", status == 401, "status=%s" % status)
    # 2. 登录页能打开
    status, raw = h.get("/login")
    check("登录页 200", status == 200 and b"admin" in raw,
          "status=%s" % status)
    # 3. 错密码 401
    status, data = h.json("POST", "/api/login",
                          {"username": "admin", "password": "wrong"})
    check("错密码 401", status == 401, str(data))
    # 4. 默认 admin/admin 登录成功
    status, data = h.json("POST", "/api/login",
                          {"username": "admin", "password": "admin"})
    check("默认 admin/admin 登录成功",
          status == 200 and data.get("ok"), str(data))
    # 5. 首页是遥控器
    status, raw = h.get("/")
    check("登录后首页 200 且含遥控器",
          status == 200 and "小米电视遥控".encode("utf-8") in raw
          and b"data-key=\"vol_up\"" in raw, "status=%s" % status)
    # 6. 设置页能打开
    status, raw = h.get("/settings")
    check("设置页 200 且含 ADB 指引",
          status == 200 and "ADB 调试".encode("utf-8") in raw,
          "status=%s" % status)
    # 7. 静态资源
    status, raw = h.get("/static/style.css")
    check("静态 CSS 200", status == 200 and len(raw) > 1000,
          "status=%s len=%d" % (status, len(raw)))
    status, raw = h.get("/manifest.json")
    check("PWA manifest 200", status == 200
          and b"mitvremote" not in raw and "电视遥控".encode("utf-8") in raw,
          "status=%s" % status)
    # 8. 未配置时状态是 not_configured
    status, data = h.json("GET", "/api/status")
    check("未配电视时状态 not_configured",
          data.get("state") == "not_configured", str(data))
    # 9. 按键未配置时报错（不崩）
    status, data = h.json("POST", "/api/key", {"key": "vol_up"})
    check("未配电视按按键回错",
          not data.get("ok") and data.get("state") == "not_configured",
          str(data))
    # 10. 未知按键 400
    status, data = h.json("POST", "/api/key", {"key": "nope"})
    check("未知按键 400", status == 400, str(data))
    # 11. 设置校验：非法 MAC 拒绝
    status, data = h.json("POST", "/api/settings",
                          {"tv_name": "客厅电视", "tv_ip": "127.0.0.1",
                           "tv_port": mock_auth.port, "tv_mac": "xx"})
    check("非法 MAC 保存被拒", status == 400 and "MAC" in data.get("error", ""),
          str(data))
    # 12. 设置校验：非法 IP 拒绝
    status, data = h.json("POST", "/api/settings",
                          {"tv_name": "客厅电视", "tv_ip": "999.1.1.1",
                           "tv_port": mock_auth.port, "tv_mac": ""})
    check("非法 IP 保存被拒", status == 400, str(data))
    # 13. 保存正确设置
    status, data = h.json("POST", "/api/settings",
                          {"tv_name": "客厅电视", "tv_ip": "127.0.0.1",
                           "tv_port": mock_auth.port,
                           "tv_mac": "AA:BB:CC:DD:EE:FF"})
    check("保存设置成功", status == 200 and data.get("ok"), str(data))
    cfg = json.load(open(os.path.join(TMPDATA, "config.json"),
                         encoding="utf-8"))
    check("config.json 已持久化",
          cfg.get("tv_ip") == "127.0.0.1"
          and cfg.get("tv_mac") == "AA:BB:CC:DD:EE:FF"
          and cfg.get("tv_port") == mock_auth.port, str(cfg))
    # 14. 测试连接：mock 已授权模式应成功并读出型号
    status, data = h.json("POST", "/api/test")
    check("测试连接成功（mock 已授权）",
          status == 200 and data.get("ok")
          and data.get("state") == "connected"
          and data.get("model") == "MiTV-MockModel",
          str(data))
    # 15. 发按键：mock 应收到 input keyevent
    before = len(mock_auth.commands)
    status, data = h.json("POST", "/api/key", {"key": "vol_up"})
    check("音量+按键成功",
          status == 200 and data.get("ok"), str(data))
    check("mock 收到 input keyevent KEYCODE_VOLUME_UP",
          mock_auth.commands[before:] == ["input keyevent KEYCODE_VOLUME_UP"],
          str(mock_auth.commands[before:]))
    for key, code in (("power", "KEYCODE_POWER"), ("ok", "KEYCODE_DPAD_CENTER"),
                      ("back", "KEYCODE_BACK"), ("home", "KEYCODE_HOME"),
                      ("menu", "KEYCODE_MENU"), ("input", "KEYCODE_TV_INPUT"),
                      ("mute", "KEYCODE_MUTE"), ("ch_down", "KEYCODE_CHANNEL_DOWN"),
                      ("up", "KEYCODE_DPAD_UP"), ("left", "KEYCODE_DPAD_LEFT")):
        status, data = h.json("POST", "/api/key", {"key": key})
        if not data.get("ok"):
            check("按键 %s 成功" % key, False, str(data))
            break
    else:
        expect = ["input keyevent %s" % c for _, c in
                  (("power", "KEYCODE_POWER"), ("ok", "KEYCODE_DPAD_CENTER"),
                   ("back", "KEYCODE_BACK"), ("home", "KEYCODE_HOME"),
                   ("menu", "KEYCODE_MENU"), ("input", "KEYCODE_TV_INPUT"),
                   ("mute", "KEYCODE_MUTE"), ("ch_down", "KEYCODE_CHANNEL_DOWN"),
                   ("up", "KEYCODE_DPAD_UP"), ("left", "KEYCODE_DPAD_LEFT"))]
        check("全部按键都发到 mock",
              mock_auth.commands[before + 1:] == expect,
              str(mock_auth.commands[before + 1:]))
    # 16. 状态接口反映已连接
    status, data = h.json("GET", "/api/status")
    check("状态接口已连接",
          data.get("state") == "connected"
          and data.get("tv_ip") == "127.0.0.1", str(data))
    # 17. 改密码后旧密码失效
    status, data = h.json("POST", "/api/change-password",
                          {"current_password": "admin",
                           "new_username": "", "new_password": "newpass1"})
    check("改密码成功", status == 200 and data.get("ok"), str(data))
    h2 = Http()
    status, data = h2.json("POST", "/api/login",
                           {"username": "admin", "password": "admin"})
    check("改密码后旧密码登录失败", status == 401, str(data))
    status, data = h2.json("POST", "/api/login",
                           {"username": "admin", "password": "newpass1"})
    check("改密码后新密码登录成功", status == 200 and data.get("ok"),
          str(data))
    # 恢复默认密码，供后续测试复用同一会话
    h.json("POST", "/api/change-password",
           {"current_password": "newpass1", "new_username": "",
            "new_password": "admin"})
    return h


def test_screenshot(h, mock_auth):
    def parse_json(raw):
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {"_unparsable": raw[:200]}

    # 1. 截图接口须登录
    h_anon = Http()
    status, raw = h_anon.req("GET", "/api/screenshot")
    check("未登录截图接口回 401", status == 401, "status=%s" % status)
    # 2. 首页含电视画面区
    status, raw = h.get("/")
    check("首页含电视画面区",
          status == 200 and b'id="shot-img"' in raw
          and b'id="btn-shot-toggle"' in raw
          and b'id="shot-wrap"' in raw
          and "电视画面".encode("utf-8") in raw, "status=%s" % status)
    # 3. exec-out 通道返回合法 PNG
    before = len(mock_auth.commands)
    status, raw = h.req("GET", "/api/screenshot")
    check("截图接口 200 返回 PNG",
          status == 200 and raw[:8] == b"\x89PNG\r\n\x1a\n",
          "status=%s len=%d" % (status, len(raw)))
    check("截图内容就是 mock 的 PNG", raw == MOCK_PNG,
          "len=%d" % len(raw))
    check("截图走了 exec-out（screencap -p）",
          mock_auth.commands[before:] == ["screencap -p"],
          str(mock_auth.commands[before:]))
    # 4. exec 失效时 base64 兜底仍返回合法 PNG
    mock_auth.screencap_mode = "fallback"
    before = len(mock_auth.commands)
    status, raw = h.req("GET", "/api/screenshot")
    check("exec 失效时 base64 兜底仍返回合法 PNG",
          status == 200 and raw == MOCK_PNG,
          "status=%s len=%d" % (status, len(raw)))
    check("兜底走了 shell screencap -p | base64",
          mock_auth.commands[before:] == ["screencap -p",
                                          "screencap -p | base64"],
          str(mock_auth.commands[before:]))
    mock_auth.screencap_mode = "ok"
    # 5. 三条路都失败：回 JSON 可读错误，写明三种方法各卡在哪，不带堆栈
    mock_auth.screencap_mode = "fail"
    status, raw = h.req("GET", "/api/screenshot")
    data = parse_json(raw)
    check("截图失败回 JSON 可读错误（写明三种方法）",
          status == 503 and not data.get("ok") and data.get("error")
          and "截图失败" in data.get("error", "")
          and "exec-out" in data.get("error", "")
          and "base64" in data.get("error", "")
          and "落文件" in data.get("error", "")
          and "Traceback" not in raw.decode("utf-8", "replace"),
          "status=%s %s" % (status, str(data)[:200]))
    mock_auth.screencap_mode = "ok"
    # 6. 节流：服务端正忙时直接报 busy（429），不排队
    app._screenshot_lock.acquire()
    try:
        status, raw = h.req("GET", "/api/screenshot")
        data = parse_json(raw)
    finally:
        app._screenshot_lock.release()
    check("截图进行中再请求报 busy（429）",
          status == 429 and data.get("state") == "busy",
          "status=%s %s" % (status, str(data)[:200]))
    # 7. 未配置电视
    h.json("POST", "/api/settings",
           {"tv_name": "客厅电视", "tv_ip": "", "tv_port": mock_auth.port,
            "tv_mac": ""})
    status, raw = h.req("GET", "/api/screenshot")
    data = parse_json(raw)
    check("未配电视截图回 not_configured",
          not data.get("ok") and data.get("state") == "not_configured",
          str(data))
    # 8. 连接被拒
    h.json("POST", "/api/settings",
           {"tv_name": "客厅电视", "tv_ip": "127.0.0.1", "tv_port": 59999,
            "tv_mac": ""})
    status, raw = h.req("GET", "/api/screenshot")
    data = parse_json(raw)
    check("连不上电视截图回 refused",
          not data.get("ok") and data.get("state") == "refused", str(data))
    # 9. 恢复设置后截图正常
    h.json("POST", "/api/settings",
           {"tv_name": "客厅电视", "tv_ip": "127.0.0.1",
            "tv_port": mock_auth.port, "tv_mac": "AA:BB:CC:DD:EE:FF"})
    status, raw = h.req("GET", "/api/screenshot")
    check("恢复后截图正常", status == 200 and raw == MOCK_PNG,
          "status=%s" % status)


def test_states(h, mock_auth, mock_never):
    # refused：指一个没开的端口
    status, data = h.json("POST", "/api/settings",
                          {"tv_name": "客厅电视", "tv_ip": "127.0.0.1",
                           "tv_port": 59999, "tv_mac": ""})
    check("切到关闭端口保存成功", data.get("ok"), str(data))
    status, data = h.json("POST", "/api/test")
    check("连关闭端口报 refused",
          not data.get("ok") and data.get("state") == "refused",
          str(data))
    check("refused 文案提到 ADB 没开",
          "ADB 没开" in data.get("error", ""), data.get("error", ""))
    check("refused 文案提到遥控器开机可能复位 ADB",
          "用遥控器开机后" in data.get("error", ""), data.get("error", ""))
    # awaiting_auth：mock 从不回应公钥，客户端应在 auth 超时后报等待点允许
    status, data = h.json("POST", "/api/settings",
                          {"tv_name": "客厅电视", "tv_ip": "127.0.0.1",
                           "tv_port": mock_never.port, "tv_mac": ""})
    check("切到 auth_never mock 保存成功", data.get("ok"), str(data))
    status, data = h.json("POST", "/api/test")
    check("等待授权时报 awaiting_auth",
          not data.get("ok") and data.get("state") == "awaiting_auth",
          str(data))
    check("awaiting_auth 文案提到点允许",
          "允许" in data.get("error", ""), data.get("error", ""))
    # 恢复到已授权 mock
    h.json("POST", "/api/settings",
           {"tv_name": "客厅电视", "tv_ip": "127.0.0.1",
            "tv_port": mock_auth.port, "tv_mac": "AA:BB:CC:DD:EE:FF"})


def test_discover(h, mock_auth):
    # 让发现扫描 127.0.0.0/24：本机伪装成 127.0.0.2，mock 在 127.0.0.1 上
    #（扫描会跳过本机自身，所以本机不能伪装成 mock 所在的 127.0.0.1）
    real_local_ip = app._local_ip
    app._local_ip = lambda: "127.0.0.2"
    try:
        status, data = h.json("POST", "/api/discover")
    finally:
        app._local_ip = real_local_ip
    check("发现扫描成功",
          status == 200 and data.get("ok"), str(data)[:300])
    found = [d for d in data.get("devices", [])
             if d.get("ip") == "127.0.0.1"]
    check("发现扫描找到 mock 电视",
          len(found) == 1 and found[0].get("state") == "connected"
          and found[0].get("model") == "MiTV-MockModel",
          str(data.get("devices"))[:300])
    check("发现扫描网段正确",
          data.get("subnet") == "127.0.0.0/24", str(data.get("subnet")))


def test_power_on(h):
    # 沙箱禁 UDP 广播（EPERM），这里用桩替换 send_wol：只验证开机流程
    # （调 WoL → 等待 → ADB 连接）与 MAC 透传；真实广播发送已在单元
    # 测试里用假 socket 捕获验证过。
    calls = {}
    real_send_wol = app.send_wol

    def fake_send_wol(mac):
        calls["mac"] = mac
        return True, ""

    app.send_wol = fake_send_wol
    try:
        status, data = h.json("POST", "/api/power-on")
    finally:
        app.send_wol = real_send_wol
    check("开机接口调用了 WoL（MAC 透传）",
          calls.get("mac") == "AA:BB:CC:DD:EE:FF", str(calls))
    check("开机接口成功",
          status == 200 and data.get("ok")
          and data.get("state") in ("connected", "waking"),
          str(data))


def test_connection_reuse(h, mock_auth):
    """1.0.3：按键与截图各用一条持久连接，互不干扰。"""
    # 先把设置指回已授权 mock，并丢掉全部连接，从干净状态开始数
    h.json("POST", "/api/settings",
           {"tv_name": "客厅电视", "tv_ip": "127.0.0.1",
            "tv_port": mock_auth.port, "tv_mac": "AA:BB:CC:DD:EE:FF"})
    app._drop_all_devices()
    before = mock_auth.connections
    t0 = time.time()
    for key in ("vol_up", "vol_down", "up", "down", "ok"):
        ok, state, detail = app.send_key(key)
        check("复用：按键 %s 成功" % key, ok, str(detail))
    dt = time.time() - t0
    check("复用：5 次按键只建 1 条连接",
          mock_auth.connections - before == 1,
          "connections=%d->%d" % (before, mock_auth.connections))
    check("复用：5 次按键 2 秒内完成",
          dt < 2.0, "%.2fs" % dt)
    # 截图在途时按键不受阻：mock 截图拖 3 秒，后台取截图，主线程按键
    mock_auth.screencap_delay = 3.0
    before_cmds = len(mock_auth.commands)
    shot_result = {}

    def _shot():
        shot_result["ret"] = app.take_screenshot()

    th = threading.Thread(target=_shot, daemon=True)
    th.start()
    # 等截图命令确实发到 mock（在途），再按，避免按键抢在截图前面
    deadline = time.time() + 10
    while ("screencap -p" not in mock_auth.commands[before_cmds:]
           and time.time() < deadline):
        time.sleep(0.05)
    check("隔离：截图请求已在途",
          "screencap -p" in mock_auth.commands[before_cmds:],
          str(mock_auth.commands[before_cmds:]))
    t0 = time.time()
    ok, state, detail = app.send_key("vol_up")
    key_dt = time.time() - t0
    check("隔离：截图在途时按键成功", ok, str(detail))
    check("隔离：截图在途时按键 2 秒内返回（截图要 3 秒）",
          key_dt < 2.0, "%.2fs" % key_dt)
    th.join(timeout=30)
    png, state, detail = shot_result.get("ret", (None, "?", "?"))
    check("隔离：慢截图最终仍返回 PNG",
          png == MOCK_PNG, "state=%s %s" % (state, detail))
    mock_auth.screencap_delay = 0.0
    # 截图失败不丢按键连接：fail 模式截图一次，再按键不应新建连接
    mock_auth.screencap_mode = "fail"
    png, state, detail = app.take_screenshot()
    check("隔离：截图失败回错",
          png is None and state == "error", "state=%s %s" % (state, detail))
    before = mock_auth.connections
    ok, state, detail = app.send_key("vol_up")
    check("隔离：截图失败后按键成功", ok, str(detail))
    check("隔离：截图失败后按键不新建连接",
          mock_auth.connections - before == 0,
          "connections=%d->%d" % (before, mock_auth.connections))
    # 落文件兜底：exec 与 base64 都失效时，落文件再读能拿到图
    mock_auth.screencap_mode = "file_only"
    before_cmds = len(mock_auth.commands)
    png, state, detail = app.take_screenshot()
    cmds = mock_auth.commands[before_cmds:]
    check("兜底：落文件方式拿到合法 PNG",
          png == MOCK_PNG, "state=%s %s" % (state, detail))
    check("兜底：走了落文件命令",
          any(c.startswith("screencap /sdcard/") for c in cmds)
          and any(c.startswith("test -f /sdcard/") for c in cmds),
          str(cmds))
    check("兜底：临时文件已清理",
          not mock_auth.files, str(mock_auth.files))
    mock_auth.screencap_mode = "ok"


def main():
    print("== 单元：MAC / WoL ==")
    test_mac_wol()
    print("== 启动 mock ADB + HTTP 服务 ==")
    mock_auth = MockAdbServer("authorized").start()
    mock_never = MockAdbServer("auth_never").start()
    print("mock authorized port=%d, auth_never port=%d"
          % (mock_auth.port, mock_never.port))
    t0 = time.time()
    app.load_config()
    app.load_sessions()
    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), app.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("http on 127.0.0.1:%d (data dir %s)" % (HTTP_PORT, TMPDATA))
    print("== HTTP 测试 ==")
    h = test_http(mock_auth)
    print("== 截图测试 ==")
    test_screenshot(h, mock_auth)
    print("== 状态区分测试 ==")
    test_states(h, mock_auth, mock_never)
    print("== 发现扫描测试 ==")
    test_discover(h, mock_auth)
    print("== 开机测试 ==")
    test_power_on(h)
    print("== 连接复用与隔离测试 ==")
    test_connection_reuse(h, mock_auth)
    print("== 密钥文件 ==")
    check("ADB 密钥已持久化到数据目录",
          os.path.isfile(os.path.join(TMPDATA, "adbkey"))
          and os.path.isfile(os.path.join(TMPDATA, "adbkey.pub")))
    key_size = os.path.getsize(os.path.join(TMPDATA, "adbkey"))
    check("ADB 私钥为 PKCS#8 PEM",
          open(os.path.join(TMPDATA, "adbkey")).read().startswith(
              "-----BEGIN PRIVATE KEY-----"),
          "size=%d" % key_size)
    server.shutdown()
    mock_auth.stop()
    mock_never.stop()
    print("== 汇总：%d/%d 通过，用时 %.1fs =="
          % (sum(RESULTS), len(RESULTS), time.time() - t0))
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
