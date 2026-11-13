#!/usr/bin/env python3
"""小米电视遥控 mitvremote：飞牛 FnOS 原生应用。

用 ADB 控制同一局域网内的小米电视：手机浏览器打开就是遥控器，
大按钮发按键（电源、音量、换台、方向盘、确认、返回、主页、菜单、
信号源、静音），关机状态下可发局域网唤醒（WoL）开机。遥控页顶部
还有“电视画面”截图预览：ADB 截图每 2 秒自动刷新一张，点画面
手动刷新，可暂停（实时流畅视频做不到，ADB 截图本身就慢）。

  * 不依赖 NAS 上装 adb：纯 Python 的 adb-shell（0.4.4）+ rsa
    （4.9.1）+ pyasn1（0.6.5）全部 vendored 在 app/vendor/ 里，
    直接连电视的 5555 端口发 `input keyevent`。
  * 首次连接电视会弹“允许 USB 调试？”的授权框：应用启动时在数据
    目录生成一对 RSA 密钥（adbkey / adbkey.pub，持久保存），连接
    时把公钥发给电视，用户在电视上点“允许”（可勾“始终允许”）后
    以后不再弹。连接状态明确区分：已连接 / 连接被拒（ADB 没开）
    / 等待电视上点允许 / 超时。
  * 局域网唤醒：设置页填电视 MAC 地址，点“开机”先发 WoL 幻数据
    包（MAC 重复 16 次，UDP 广播 255.255.255.255:9），再尝试 ADB
    连接。WoL 只在电视待机不断电、且支持网络唤醒时有效。
  * 发现电视：扫描本机所在 /24 网段的 5555 端口，列出响应的 ADB
    设备（能连上的还会读出型号），一键填入 IP。
  * 账号密码：默认 admin/admin，首次运行即写入加盐哈希；设置页
    可改账号密码，登录后用 HttpOnly 会话 cookie 保持 30 天，未登
    录打不开任何页面与接口。忘密码：删 config.json 的 auth 段，
    重启恢复默认。
  * PWA：manifest + service worker，手机浏览器“添加到主屏幕”后
    可全屏打开，像原生遥控器一样。

只用 Python 标准库 + vendored 纯 Python 库。设置页与遥控页只应
在局域网内访问，不要把端口映射到公网。
"""

import base64
import concurrent.futures
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import struct
import sys
import threading
import time
import urllib.parse
import warnings

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

warnings.filterwarnings("ignore", category=DeprecationWarning)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
VENDOR_DIR = os.path.join(APP_DIR, "vendor")
STATIC_DIR = os.path.join(APP_DIR, "static")
sys.path.insert(0, VENDOR_DIR)
sys.path.insert(0, APP_DIR)

VERSION = "1.0.3"
APPNAME = "mitvremote"
DEFAULT_PORT = 18091
DEFAULT_ADB_PORT = 5555
SESSION_COOKIE = "mitvremote_session"
SESSION_TTL = 30 * 24 * 3600  # 会话有效期 30 天
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin"
MAX_JSON_BODY = 64 * 1024

# 按键名 -> Android keyevent（input keyevent 用）
KEY_EVENTS = {
    "power": "KEYCODE_POWER",
    "vol_up": "KEYCODE_VOLUME_UP",
    "vol_down": "KEYCODE_VOLUME_DOWN",
    "mute": "KEYCODE_MUTE",
    "ch_up": "KEYCODE_CHANNEL_UP",
    "ch_down": "KEYCODE_CHANNEL_DOWN",
    "up": "KEYCODE_DPAD_UP",
    "down": "KEYCODE_DPAD_DOWN",
    "left": "KEYCODE_DPAD_LEFT",
    "right": "KEYCODE_DPAD_RIGHT",
    "ok": "KEYCODE_DPAD_CENTER",
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "menu": "KEYCODE_MENU",
    "input": "KEYCODE_TV_INPUT",
}

# 连接状态 -> 页面上显示的一句话
STATE_TEXT = {
    "not_configured": "未配置电视：请先在设置页填电视 IP",
    "connected": "已连接",
    "refused": ("连接被拒：电视 ADB 没开，或电视关机，或 IP 不对。"
                "用遥控器开机后 ADB 开关可能被复位，请在电视上重新开启"
                " ADB 调试（设置 → 账号与安全 → ADB 调试 → 允许）"),
    "awaiting_auth": "请在电视上点“允许 USB 调试”（可勾“始终允许”），再点一次",
    "timeout": "连接超时：电视没有响应，请检查电视是否开机、是否在同一网段",
    "error": "连接出错",
    "unknown": "未测试连接",
}

# 小米固件提示：部分小米电视（实测系统版本 1.3.130）用遥控器开机
# （哪怕只是待机唤醒）后 ADB 开关会被复位成拒绝，不是应用的问题；
# 重新在电视上开一次 ADB 调试即可。见 README“电视重启后 ADB 被复位”。
ADB_RESET_HINT = ("用遥控器开机后 ADB 开关可能被复位，请在电视上重新开启"
                  " ADB 调试（设置 → 账号与安全 → ADB 调试 → 允许）")


# --------------------------------------------------------------------------
# Paths / environment
# --------------------------------------------------------------------------

def resolve_data_dir():
    for key in ("MITVREMOTE_DATA_DIR", "TRIM_PKGVAR"):
        value = os.environ.get(key)
        if value:
            return value
    return os.path.join(APP_DIR, "data")


DATA_DIR = resolve_data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
SESSIONS_PATH = os.path.join(DATA_DIR, "sessions.json")
ADB_KEY_PATH = os.path.join(DATA_DIR, "adbkey")


def resolve_port():
    for key in ("MITVREMOTE_PORT", "wizard_port", "TRIM_SERVICE_PORT"):
        value = os.environ.get(key)
        if value:
            try:
                port = int(value)
            except (TypeError, ValueError):
                continue
            if 1 <= port <= 65535:
                return port
    return DEFAULT_PORT


# --------------------------------------------------------------------------
# Config (atomic JSON file in the data dir)
# --------------------------------------------------------------------------

_config_lock = threading.Lock()
_config = {"tv_name": "小米电视", "tv_ip": "", "tv_port": DEFAULT_ADB_PORT,
           "tv_mac": ""}


def _atomic_write(path, data_bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data_bytes)
    os.replace(tmp, path)


def load_config():
    global _config
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            with _config_lock:
                if isinstance(data.get("tv_name"), str):
                    _config["tv_name"] = data["tv_name"].strip() or "小米电视"
                if isinstance(data.get("tv_ip"), str):
                    _config["tv_ip"] = data["tv_ip"].strip()
                try:
                    port = int(data.get("tv_port"))
                    if 1 <= port <= 65535:
                        _config["tv_port"] = port
                except (TypeError, ValueError):
                    pass
                if isinstance(data.get("tv_mac"), str):
                    _config["tv_mac"] = data["tv_mac"].strip()
                if _valid_auth(data.get("auth")):
                    _config["auth"] = data["auth"]
    except (OSError, ValueError):
        pass
    if not _config.get("tv_ip"):
        # First run: seed the TV IP from the install wizard.
        wizard_ip = (os.environ.get("wizard_tv_ip") or "").strip()
        if wizard_ip and _valid_host(wizard_ip):
            with _config_lock:
                _config["tv_ip"] = wizard_ip
            save_config()
    _ensure_auth()


def save_config():
    with _config_lock:
        payload = json.dumps(_config, ensure_ascii=False, indent=2)
    _atomic_write(CONFIG_PATH, payload.encode("utf-8"))


def get_config_copy():
    with _config_lock:
        return dict(_config)


# --------------------------------------------------------------------------
# 账号密码（加盐哈希存 config.json；会话 token 存 sessions.json）
# --------------------------------------------------------------------------

_sessions_lock = threading.Lock()
_sessions = {}  # token -> {"username": str, "expires": int}


def _hash_password(password, salt):
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def _new_auth(username, password):
    salt = secrets.token_hex(16)
    return {"username": username, "salt": salt,
            "password_hash": _hash_password(password, salt)}


def _valid_auth(auth):
    return (isinstance(auth, dict)
            and isinstance(auth.get("username"), str)
            and bool(auth["username"])
            and isinstance(auth.get("salt"), str)
            and bool(auth["salt"])
            and isinstance(auth.get("password_hash"), str)
            and bool(auth["password_hash"]))


def get_auth():
    """当前账号配置；未配置时返回默认 admin/admin（内存态）。"""
    with _config_lock:
        auth = _config.get("auth")
    if _valid_auth(auth):
        return auth
    return _new_auth(DEFAULT_USERNAME, DEFAULT_PASSWORD)


def _ensure_auth():
    """保证 _config 里有一份有效的账号配置，不足则建默认并存盘。"""
    with _config_lock:
        dirty = not _valid_auth(_config.get("auth"))
        if dirty:
            _config["auth"] = _new_auth(DEFAULT_USERNAME, DEFAULT_PASSWORD)
    if dirty:
        save_config()


def verify_login(username, password):
    """比对账号与密码（哈希比对，防时序攻击）。"""
    if not isinstance(username, str) or not isinstance(password, str):
        return False
    auth = get_auth()
    if username != auth["username"]:
        return False
    expect = _hash_password(password, auth["salt"])
    return hmac.compare_digest(expect, auth["password_hash"])


def set_auth(username, password):
    """改账号密码：只动 config 的 auth 段，电视设置不受影响。"""
    with _config_lock:
        _config["auth"] = _new_auth(username, password)
    save_config()


def load_sessions():
    global _sessions
    try:
        with open(SESSIONS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            now = int(time.time())
            with _sessions_lock:
                _sessions = {t: v for t, v in data.items()
                             if isinstance(v, dict)
                             and isinstance(v.get("expires"), int)
                             and v["expires"] > now
                             and isinstance(v.get("username"), str)}
    except (OSError, ValueError):
        pass


def save_sessions():
    with _sessions_lock:
        payload = json.dumps(_sessions, ensure_ascii=False, indent=2)
    _atomic_write(SESSIONS_PATH, payload.encode("utf-8"))


def create_session(username):
    token = secrets.token_hex(32)
    with _sessions_lock:
        _sessions[token] = {"username": username,
                            "expires": int(time.time()) + SESSION_TTL}
    save_sessions()
    return token


def validate_session(token):
    """有效返回账号名；无效或过期返回 None（过期顺手清掉）。"""
    if not token:
        return None
    expired = False
    with _sessions_lock:
        sess = _sessions.get(token)
        if not sess:
            return None
        if sess.get("expires", 0) <= int(time.time()):
            del _sessions[token]
            expired = True
        else:
            return sess.get("username")
    if expired:
        save_sessions()
    return None


def destroy_session(token):
    if not token:
        return
    with _sessions_lock:
        gone = _sessions.pop(token, None) is not None
    if gone:
        save_sessions()


def destroy_other_sessions(keep_token):
    """改密码后只保留当前会话，其它设备/浏览器要重新登录。"""
    with _sessions_lock:
        others = [t for t in _sessions if t != keep_token]
        for t in others:
            del _sessions[t]
    if others:
        save_sessions()


# --------------------------------------------------------------------------
# ADB 密钥：纯 Python 生成 RSA 密钥对（vendored rsa 库），持久化保存
#
# adb-shell 的 PythonRSASigner 要的是 PKCS#8 PEM 私钥 + Android 二进制
# 格式的公钥文件（.pub）。adb-shell 自带的 keygen 依赖 cryptography
# （非纯 Python，NAS 上不一定有），这里用 rsa 库自己生成并组装两种
# 格式，逻辑照搬 adb-shell/auth/keygen.py 的 encode_pubkey。
# --------------------------------------------------------------------------

_ADB_KEY_BITS = 2048
_ANDROID_PUBKEY_MODULUS_SIZE = 2048 // 8  # 256 bytes


def _der_len(length):
    if length < 0x80:
        return bytes([length])
    buf = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(buf)]) + buf


def _der_tlv(tag, content):
    return bytes([tag]) + _der_len(len(content)) + content


def _der_int(value):
    raw = value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return _der_tlv(0x02, raw)


def _pkcs8_pem(priv_key):
    """rsa.PrivateKey -> PKCS#8 PEM 文本（adb-shell 认的格式）。"""
    pkcs1_der = priv_key.save_pkcs1(format="DER")
    rsa_oid = bytes([0x06, 0x09, 0x2A, 0x86, 0x48, 0x86, 0xF7, 0x0D,
                     0x01, 0x01, 0x01])  # 1.2.840.113549.1.1.1
    alg_id = _der_tlv(0x30, rsa_oid + _der_tlv(0x05, b""))
    key_info = _der_tlv(
        0x30,
        _der_int(0) + alg_id + _der_tlv(0x04, pkcs1_der))
    b64 = base64.encodebytes(key_info).decode("ascii")
    lines = ["-----BEGIN PRIVATE KEY-----"]
    lines.extend(line for line in b64.splitlines() if line)
    lines.append("-----END PRIVATE KEY-----")
    return "\n".join(lines) + "\n"


def _android_pubkey(pub_key):
    """rsa.PublicKey -> Android 二进制公钥文件的 base64 文本。"""
    n, e = pub_key.n, pub_key.e
    size = _ANDROID_PUBKEY_MODULUS_SIZE
    n0 = n % (1 << 32)
    n0inv = (1 << 32) - pow(n0, -1, 1 << 32)
    modulus = n.to_bytes(size, "little")
    rr = pow(1 << (size * 8), 2, n).to_bytes(size, "little")
    blob = struct.pack("<LL", size // 4, n0inv) + modulus + rr
    blob += struct.pack("<L", e)
    user = " mitvremote@%s" % (socket.gethostname() or "nas")
    return base64.b64encode(blob).decode("ascii") + user


_signer_lock = threading.Lock()
_signer = None


def get_signer():
    """加载或生成 ADB 密钥对，返回 PythonRSASigner（单例）。"""
    global _signer
    with _signer_lock:
        if _signer is not None:
            return _signer
        from adb_shell.auth.sign_pythonrsa import PythonRSASigner
        priv_path = ADB_KEY_PATH
        pub_path = ADB_KEY_PATH + ".pub"
        if os.path.isfile(priv_path) and os.path.isfile(pub_path):
            try:
                _signer = PythonRSASigner.FromRSAKeyPath(priv_path)
                return _signer
            except Exception:  # noqa: BLE001 - 密钥损坏就重新生成
                pass
        import rsa
        pub_key, priv_key = rsa.newkeys(_ADB_KEY_BITS)
        pem = _pkcs8_pem(priv_key)
        pub_text = _android_pubkey(pub_key)
        os.makedirs(DATA_DIR, exist_ok=True)
        _atomic_write(priv_path, pem.encode("utf-8"))
        try:
            os.chmod(priv_path, 0o600)
        except OSError:
            pass
        _atomic_write(pub_path, pub_text.encode("utf-8"))
        _signer = PythonRSASigner(pub=pub_text, priv=pem)
        return _signer


# --------------------------------------------------------------------------
# ADB 连接层：状态明确区分，供页面显示
# --------------------------------------------------------------------------

_status_lock = threading.Lock()
_status = {"state": "unknown", "detail": "", "model": "",
           "checked_at": 0, "latency_ms": None}


def _set_status(state, detail="", model=None, latency_ms=None):
    with _status_lock:
        _status.update({"state": state, "detail": detail,
                        "checked_at": int(time.time())})
        # 型号与耗时只在明确传值时覆盖：按键成功不应把已读到的型号清掉。
        if model is not None:
            _status["model"] = model
        if latency_ms is not None:
            _status["latency_ms"] = latency_ms


def get_status_copy():
    with _status_lock:
        return dict(_status)


def _adb_connect(host, port, auth_timeout_s=10.0, transport_timeout_s=5.0):
    """一次 ADB 连接尝试。

    返回 (device 或 None, state, detail)。state 取值：
      connected / refused / awaiting_auth / timeout / error。
    awaiting_auth 的判定：adb-shell 在发完公钥后等 CNXN 超时，且
    auth_callback 已被调用（说明公钥已发给电视，电视正在等用户点
    “允许”）。其它超时一律算 timeout。
    """
    from adb_shell.adb_device import AdbDeviceTcp
    from adb_shell import exceptions as adb_exc
    sent_pubkey = {"flag": False}

    def _auth_callback(_device):
        sent_pubkey["flag"] = True

    try:
        signer = get_signer()
    except Exception as exc:  # noqa: BLE001 - 密钥生成失败
        return None, "error", "ADB 密钥生成失败：%s" % exc
    device = AdbDeviceTcp(host, port,
                          default_transport_timeout_s=transport_timeout_s)
    try:
        device.connect(rsa_keys=[signer], auth_timeout_s=auth_timeout_s,
                       auth_callback=_auth_callback)
    except ConnectionRefusedError:
        return None, "refused", (
            "连不上 %s:%s：电视 ADB 没开，或电视关机，或 IP 不对。"
            "%s" % (host, port, ADB_RESET_HINT))
    except (socket.timeout, TimeoutError):
        return None, "timeout", "连接 %s:%s 超时" % (host, port)
    except adb_exc.AdbTimeoutError:
        if sent_pubkey["flag"]:
            return None, "awaiting_auth", (
                "电视上已弹出授权框，请在电视上点“允许 USB 调试”"
                "（可勾“始终允许”），然后再点一次")
        return None, "timeout", "电视没有响应（握手超时）"
    except adb_exc.TcpTimeoutException:
        # TcpTransport 的读超时不经过 AdbTimeoutError 包装：同样按
        # 是否已发公钥区分“等点允许”与“超时”。
        if sent_pubkey["flag"]:
            return None, "awaiting_auth", (
                "电视上已弹出授权框，请在电视上点“允许 USB 调试”"
                "（可勾“始终允许”），然后再点一次")
        return None, "timeout", "电视没有响应（握手超时）"
    except adb_exc.DeviceAuthError as exc:
        return None, "error", "电视拒绝了本机的 ADB 密钥：%s" % exc
    except OSError as exc:
        # socket.gaierror / unreachable 等
        msg = str(exc)
        if "Name or service not known" in msg or "Temporary failure" in msg:
            return None, "error", "电视地址解析失败：%s" % host
        return None, "error", "连不上 %s:%s（%s）" % (host, port, exc)
    except Exception as exc:  # noqa: BLE001 - 兜底
        return None, "error", "连接失败：%s" % exc
    return device, "connected", ""


# --------------------------------------------------------------------------
# ADB 连接层：按键与截图各用一条持久连接
#
# 为什么分两条：真机上单次 ADB 命令本身就慢（实测按键约 2 秒），截图
# （screencap）更要 1–3 秒甚至超时。之前两路共用一条连接，截图一慢
# 就拖住按键；截图一失败还会把共用连接丢掉，下一键又要重新握手——
# 用户看到的就是“按一下键 2 秒才有反应、画面还加载不出来”。现在各
# 用各的连接、各自加锁串行：截图再慢、再失败也挡不住按键，截图失败
# 只丢截图连接，按键连接不受影响。按键通道复用一条连接（多次按键只
# 建一次连接，断了重连一次），目标是单次按键压到 0.5 秒以内。
# --------------------------------------------------------------------------

def _log(message):
    """服务端日志（stderr，飞牛上可在应用日志里看到，方便排障）。"""
    sys.stderr.write("mitvremote: %s\n" % message)
    try:
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - 日志写失败无所谓
        pass


class _AdbSlot:
    """一条持久 ADB 连接：设备 + 目标 + 串行锁。

    get/drop 假定调用方已持有 self.lock（整条命令的操作都在锁内完
    成，避免两个线程同时在同一条连接上发命令）。
    """

    def __init__(self, name):
        self.name = name
        self.lock = threading.Lock()
        self.device = None
        self.target = (None, None)

    def get(self, host, port, auth_timeout_s=10.0):
        """拿到可用设备；没有或目标变了就重连一次。"""
        if (self.device is not None and self.target == (host, port)
                and getattr(self.device, "available", False)):
            return self.device, "connected", ""
        device, state, detail = _adb_connect(host, port, auth_timeout_s)
        if device is not None:
            self.device = device
            self.target = (host, port)
        else:
            self.device = None
            self.target = (None, None)
        return device, state, detail

    def drop(self):
        """丢掉连接（下次再建）。"""
        if self.device is not None:
            try:
                self.device.close()
            except Exception:  # noqa: BLE001 - 关闭失败无所谓
                pass
        self.device = None
        self.target = (None, None)


_cmd_slot = _AdbSlot("按键")    # 按键 / 读型号 / 测试连接
_shot_slot = _AdbSlot("截图")   # 截图（与按键完全隔离）


def _drop_all_devices():
    """丢掉全部连接（如改了电视 IP 后）。锁按固定顺序拿，避免死锁。"""
    with _cmd_slot.lock:
        _cmd_slot.drop()
    with _shot_slot.lock:
        _shot_slot.drop()


def _run_shell_once(host, port, command, auth_timeout_s=10.0):
    """按键通道跑一条 shell 命令。返回 (ok, state, detail, output)。"""
    from adb_shell import exceptions as adb_exc
    with _cmd_slot.lock:
        device, state, detail = _cmd_slot.get(host, port, auth_timeout_s)
        if device is None:
            return False, state, detail, ""
        try:
            out = device.shell(command, transport_timeout_s=5.0,
                               read_timeout_s=5.0)
            return True, "connected", "", out or ""
        except (adb_exc.AdbConnectionError, adb_exc.AdbTimeoutError,
                OSError):
            # 连接断了：丢掉旧连接，重连一次再跑
            _cmd_slot.drop()
            device, state, detail = _cmd_slot.get(host, port, auth_timeout_s)
            if device is None:
                return False, state, detail, ""
            try:
                out = device.shell(command, transport_timeout_s=5.0,
                                   read_timeout_s=5.0)
                return True, "connected", "", out or ""
            except Exception as exc:  # noqa: BLE001 - 重试也失败
                _cmd_slot.drop()
                return False, "error", "发命令失败：%s" % exc, ""
        except Exception as exc:  # noqa: BLE001 - 兜底
            _cmd_slot.drop()
            return False, "error", "发命令失败：%s" % exc, ""


def send_key(key_name):
    """发一个遥控按键。返回 (ok, state, detail)。"""
    code = KEY_EVENTS.get(key_name)
    if not code:
        return False, "error", "未知的按键：%s" % key_name
    cfg = get_config_copy()
    host = (cfg.get("tv_ip") or "").strip()
    port = cfg.get("tv_port") or DEFAULT_ADB_PORT
    if not host:
        return False, "not_configured", STATE_TEXT["not_configured"]
    ok, state, detail, _out = _run_shell_once(
        host, port, "input keyevent %s" % code)
    if ok:
        _set_status("connected")
    elif state in STATE_TEXT:
        _set_status(state, detail)
    return ok, state, detail or STATE_TEXT.get(state, "")


def test_connection():
    """测试连接：全新连接 + 读型号，返回 dict（含耗时毫秒）。"""
    cfg = get_config_copy()
    host = (cfg.get("tv_ip") or "").strip()
    port = cfg.get("tv_port") or DEFAULT_ADB_PORT
    if not host:
        _set_status("not_configured", STATE_TEXT["not_configured"])
        return {"ok": False, "state": "not_configured",
                "error": STATE_TEXT["not_configured"], "ms": 0, "model": ""}
    started = time.time()
    with _cmd_slot.lock:
        _cmd_slot.drop()
        device, state, detail = _cmd_slot.get(host, port, auth_timeout_s=10.0)
        ms = int((time.time() - started) * 1000)
        if device is None:
            _set_status(state, detail, latency_ms=ms)
            return {"ok": False, "state": state,
                    "error": detail or STATE_TEXT.get(state, ""),
                    "ms": ms, "model": ""}
        model = ""
        try:
            out = device.shell("getprop ro.product.model",
                               transport_timeout_s=5.0, read_timeout_s=5.0)
            model = (out or "").strip()
        except Exception:  # noqa: BLE001 - 型号读不到不影响“已连接”
            model = ""
        _set_status("connected", model=model, latency_ms=ms)
        return {"ok": True, "state": "connected", "error": "", "ms": ms,
                "model": model}


# --------------------------------------------------------------------------
# 电视画面截图：ADB screencap，每 2 秒自动刷新一张（前端节流）
#
# 真机上单次 ADB 命令本身就慢（实测按键约 2 秒），截图传输更大、更
# 容易超：截图的超时要放得比按键宽得多。一次超时只说明这一种方法不
# 行，兜底链会换下一种方法继续试，不会一次超时就直接报错。三种方法
# 依次兜底：exec-out 直接拿原始字节 → shell + base64（文本通道）→
# 落文件再读（绕开 stdout，兼容性最强）。失败时界面上给一句具体的
# 原因（三种方法各卡在哪），详细原因同时写进服务端日志方便排障。
# --------------------------------------------------------------------------

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# 同一时间只跑一张截图：上一张没回来时新请求直接报 busy，不排队堆积。
_screenshot_lock = threading.Lock()

# 真机 screencap 可能要 1–3 秒甚至更久：单次读超时放宽到 10 秒，
# 单种方法总超时放宽到 25 秒（按键仍是 5 秒，截图不走按键通道）。
_SHOT_TRANSPORT_TIMEOUT_S = 10.0
_SHOT_READ_TIMEOUT_S = 25.0
# 落文件截图的候选路径（/sdcard 有的机型不可写时换 /data/local/tmp）
_SHOT_FILE_PATHS = ("/sdcard/mitvremote_shot.png",
                    "/data/local/tmp/mitvremote_shot.png")


class _ShotConnectionError(Exception):
    """截图中途连接断了：调用方应重连一次再试。"""


def _validate_png(raw):
    return raw if raw and raw.startswith(PNG_MAGIC) else None


def _shot_exec_out(device):
    """方法 1：exec-out 直接拿原始字节。返回 (png, 失败原因)。"""
    from adb_shell import exceptions as adb_exc
    try:
        raw = device.exec_out("screencap -p",
                              transport_timeout_s=_SHOT_TRANSPORT_TIMEOUT_S,
                              read_timeout_s=_SHOT_READ_TIMEOUT_S,
                              decode=False)
    except (adb_exc.AdbTimeoutError, adb_exc.TcpTimeoutException):
        # 超时只说明这台电视慢，换下一种方法继续试，不直接报错。
        return None, "超时（%s 秒无响应）" % _SHOT_READ_TIMEOUT_S
    except (adb_exc.AdbConnectionError, OSError) as exc:
        raise _ShotConnectionError("exec-out 连接异常：%s" % exc)
    except Exception as exc:  # noqa: BLE001 - 兜底
        return None, "出错：%s" % exc
    if _validate_png(raw) is not None:
        return raw, ""
    return None, "未返回有效图片"


def _shot_base64(device):
    """方法 2：shell + base64（文本通道，不损坏二进制）。"""
    from adb_shell import exceptions as adb_exc
    try:
        out = device.shell("screencap -p | base64",
                           transport_timeout_s=_SHOT_TRANSPORT_TIMEOUT_S,
                           read_timeout_s=_SHOT_READ_TIMEOUT_S)
    except (adb_exc.AdbTimeoutError, adb_exc.TcpTimeoutException):
        return None, "超时（%s 秒无响应）" % _SHOT_READ_TIMEOUT_S
    except (adb_exc.AdbConnectionError, OSError) as exc:
        raise _ShotConnectionError("base64 连接异常：%s" % exc)
    except Exception as exc:  # noqa: BLE001 - 兜底
        return None, "出错：%s" % exc
    if not out:
        return None, "未返回数据"
    try:
        raw = base64.b64decode("".join(out.split()))
    except Exception:  # noqa: BLE001 - 不是合法 base64
        return None, "解码失败"
    if _validate_png(raw) is not None:
        return raw, ""
    return None, "未返回有效图片"


def _shot_file(device):
    """方法 3：落文件再读（绕开 stdout，兼容性最强）。"""
    from adb_shell import exceptions as adb_exc
    for path in _SHOT_FILE_PATHS:
        try:
            device.shell("screencap %s" % path,
                         transport_timeout_s=_SHOT_TRANSPORT_TIMEOUT_S,
                         read_timeout_s=_SHOT_READ_TIMEOUT_S)
        except (adb_exc.AdbTimeoutError, adb_exc.TcpTimeoutException):
            # 写出超时：文件可能已生成，继续用 test -f 确认一次
            pass
        except (adb_exc.AdbConnectionError, OSError) as exc:
            raise _ShotConnectionError("落文件连接异常：%s" % exc)
        except Exception as exc:  # noqa: BLE001 - 兜底
            return None, "写出出错：%s" % exc
        try:
            out = device.shell("test -f %s && echo OK" % path,
                               transport_timeout_s=5.0, read_timeout_s=5.0)
        except (adb_exc.AdbTimeoutError, adb_exc.TcpTimeoutException):
            continue  # 该路径确认超时，换下一个路径
        except (adb_exc.AdbConnectionError, OSError) as exc:
            raise _ShotConnectionError("落文件连接异常：%s" % exc)
        except Exception as exc:  # noqa: BLE001 - 兜底
            return None, "检查出错：%s" % exc
        if "OK" not in (out or ""):
            continue  # 该路径写不出，换下一个路径
        raw = None
        try:
            raw = device.exec_out("cat %s" % path,
                                  transport_timeout_s=_SHOT_TRANSPORT_TIMEOUT_S,
                                  read_timeout_s=_SHOT_READ_TIMEOUT_S,
                                  decode=False)
        except (adb_exc.AdbTimeoutError, adb_exc.TcpTimeoutException):
            raw = None  # cat 超时，改走 base64 读
        except (adb_exc.AdbConnectionError, OSError) as exc:
            raise _ShotConnectionError("落文件连接异常：%s" % exc)
        except Exception:  # noqa: BLE001 - exec-out cat 失败就走 base64
            raw = None
        if _validate_png(raw) is None:
            try:
                out = device.shell("base64 %s" % path,
                                   transport_timeout_s=_SHOT_TRANSPORT_TIMEOUT_S,
                                   read_timeout_s=_SHOT_READ_TIMEOUT_S)
                raw = base64.b64decode("".join((out or "").split()))
            except (adb_exc.AdbTimeoutError, adb_exc.TcpTimeoutException):
                raw = None
            except (adb_exc.AdbConnectionError, OSError) as exc:
                raise _ShotConnectionError("落文件连接异常：%s" % exc)
            except Exception:  # noqa: BLE001 - 不是合法 base64
                raw = None
        try:
            device.shell("rm -f %s" % path,
                         transport_timeout_s=5.0, read_timeout_s=5.0)
        except Exception:  # noqa: BLE001 - 清理失败无所谓
            pass
        if _validate_png(raw) is not None:
            return raw, ""
        return None, "读取未返回有效图片"
    return None, "写出失败（%s 均不可写）" % "、".join(_SHOT_FILE_PATHS)


def _screenshot_chain(device):
    """依次试三种截图方法。返回 (png, 成功方法名, [各方法失败原因])。"""
    failures = []
    for name, method in (("exec-out", _shot_exec_out),
                         ("base64", _shot_base64),
                         ("落文件", _shot_file)):
        png, reason = method(device)
        if png is not None:
            return png, name, failures
        failures.append("%s（%s）" % (name, reason))
    return None, "", failures


def take_screenshot():
    """取一帧电视画面。返回 (png 或 None, state, detail)。"""
    cfg = get_config_copy()
    host = (cfg.get("tv_ip") or "").strip()
    port = cfg.get("tv_port") or DEFAULT_ADB_PORT
    if not host:
        return None, "not_configured", STATE_TEXT["not_configured"]
    if not _screenshot_lock.acquire(blocking=False):
        return None, "busy", "上一张截图还没取完，稍后再试"
    try:
        # 截图走自己的持久连接，与按键完全隔离：截图再慢、再失败也
        # 挡不住按键；截图失败只丢截图连接，按键连接不受影响。
        with _shot_slot.lock:
            for _attempt in range(2):
                device, state, detail = _shot_slot.get(
                    host, port, auth_timeout_s=10.0)
                if device is None:
                    return None, state, detail
                try:
                    png, _method, failures = _screenshot_chain(device)
                except _ShotConnectionError as exc:
                    _shot_slot.drop()
                    if _attempt == 0:
                        continue  # 重连一次再试
                    _log("screenshot %s:%s connection failed: %s"
                         % (host, port, exc))
                    return None, "error", "截图失败：连接已断开（%s），请重试" % exc
                if png is not None:
                    _set_status("connected")
                    return png, "connected", ""
                _shot_slot.drop()
                detail = "截图失败：" + "；".join(failures)
                _log("screenshot %s:%s failed: %s" % (host, port, detail))
                return None, "error", detail
            return None, "error", "截图失败：连接已断开，请重试"
    finally:
        _screenshot_lock.release()


# --------------------------------------------------------------------------
# 局域网唤醒（WoL）
# --------------------------------------------------------------------------

_MAC_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
_MAC_PLAIN_RE = re.compile(r"^[0-9A-Fa-f]{12}$")


def parse_mac(mac):
    """MAC 文本 -> 6 字节；非法返回 None。冒号/短横/无分隔均可。"""
    if not isinstance(mac, str):
        return None
    text = mac.strip()
    if _MAC_RE.match(text):
        parts = re.split("[:-]", text)
        return bytes(int(p, 16) for p in parts)
    if _MAC_PLAIN_RE.match(text):
        return bytes.fromhex(text)
    return None


def build_magic_packet(mac_bytes):
    """幻数据包：6 个 0xFF + MAC 重复 16 次，共 102 字节。"""
    return b"\xff" * 6 + mac_bytes * 16


def send_wol(mac):
    """发 WoL 幻数据包。返回 (ok, error)。"""
    mac_bytes = parse_mac(mac)
    if mac_bytes is None:
        return False, "MAC 地址格式不对，应为 AA:BB:CC:DD:EE:FF"
    packet = build_magic_packet(mac_bytes)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, ("255.255.255.255", 9))
        return True, ""
    except OSError as exc:
        return False, "发送唤醒包失败：%s" % exc
    finally:
        sock.close()


def power_on():
    """开机：先发 WoL，再等 2 秒尝试 ADB 连接。"""
    cfg = get_config_copy()
    host = (cfg.get("tv_ip") or "").strip()
    mac = (cfg.get("tv_mac") or "").strip()
    if not host:
        return {"ok": False, "state": "not_configured",
                "error": STATE_TEXT["not_configured"]}
    if not mac:
        return {"ok": False, "state": "error",
                "error": "没有填电视 MAC 地址：请先在设置页填 MAC 地址再开机"}
    ok, err = send_wol(mac)
    if not ok:
        return {"ok": False, "state": "error", "error": err}
    time.sleep(2)
    result = test_connection()
    if result["ok"]:
        result["error"] = ""
        return result
    # 电视刚被唤醒、系统还在启动，ADB 暂时连不上是正常的
    result["ok"] = True
    result["state"] = "waking"
    result["error"] = ("唤醒包已发出，电视正在启动，稍等几秒再点一次"
                       "“测试连接”确认")
    return result


# --------------------------------------------------------------------------
# 发现电视：扫本机所在 /24 网段的 ADB 端口
# --------------------------------------------------------------------------

def _local_ip():
    """本机局域网 IP（UDP connect 只查路由，不发包）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        pass
    finally:
        sock.close()
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return ""


def _probe_adb(ip, port):
    """对一个已开端口的 IP 做 ADB 握手，确认是不是电视/安卓设备。"""
    device, state, detail = _adb_connect(
        ip, port, auth_timeout_s=3.0, transport_timeout_s=2.0)
    if device is None:
        if state == "awaiting_auth":
            return {"ip": ip, "port": port, "state": "awaiting_auth",
                    "model": "", "detail": "ADB 设备，需在电视上点允许"}
        return {"ip": ip, "port": port, "state": "port_open",
                "model": "", "detail": "5555 端口开着，但握手没通过"}
    model = ""
    try:
        out = device.shell("getprop ro.product.model",
                           transport_timeout_s=3.0, read_timeout_s=3.0)
        model = (out or "").strip()
    except Exception:  # noqa: BLE001 - 型号读不到也算找到设备
        model = ""
    try:
        device.close()
    except Exception:  # noqa: BLE001
        pass
    return {"ip": ip, "port": port, "state": "connected",
            "model": model, "detail": model or "ADB 设备"}


def _tcp_port_open(ip, port, timeout=0.5):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((ip, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def discover_tvs():
    """扫描 /24 网段，返回 {ok, subnet, devices:[...]}。"""
    cfg = get_config_copy()
    port = cfg.get("tv_port") or DEFAULT_ADB_PORT
    local = _local_ip()
    if not local:
        return {"ok": False, "error": "拿不到本机局域网地址，无法扫描",
                "subnet": "", "devices": []}
    try:
        network = ipaddress.ip_network(local + "/24", strict=False)
    except ValueError:
        return {"ok": False, "error": "本机地址 %s 不是标准局域网地址" % local,
                "subnet": "", "devices": []}
    hosts = [str(ip) for ip in network.hosts() if str(ip) != local]
    open_hosts = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        futures = {pool.submit(_tcp_port_open, ip, port): ip
                   for ip in hosts}
        for fut in concurrent.futures.as_completed(futures):
            try:
                if fut.result():
                    open_hosts.append(futures[fut])
            except Exception:  # noqa: BLE001 - 单个探测失败跳过
                pass
    devices = []
    if open_hosts:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda ip: _probe_adb(ip, port), open_hosts))
        devices = sorted(results, key=lambda d: tuple(
            int(p) for p in d["ip"].split(".")))
    return {"ok": True, "error": "", "subnet": str(network),
            "devices": devices}


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
_ICON_RE = re.compile(r"^[A-Za-z0-9._-]+\.png$")
_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")
_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def send_bytes(handler, data, content_type, status=200,
               extra_headers=None):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    for key, value in (extra_headers or []):
        handler.send_header(key, value)
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(data)


def send_json(handler, obj, status=200, extra_headers=None):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    send_bytes(handler, data, "application/json; charset=utf-8",
               status, extra_headers)


def send_text(handler, text, content_type="text/html; charset=utf-8",
              status=200):
    send_bytes(handler, text.encode("utf-8"), content_type, status)


def _valid_host(host):
    if not host:
        return False
    match = _IPV4_RE.match(host)
    if match:
        return all(0 <= int(part) <= 255 for part in match.groups())
    return bool(_HOST_RE.match(host)) and len(host) <= 253


def _safe_join(root, *parts):
    """Join parts under root; returns None when the result escapes."""
    path = os.path.join(root, *parts)
    path = os.path.normpath(path)
    if os.path.commonpath([root, path]) != root:
        return None
    return path


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: D102 - silence access log
        pass

    # -- helpers ---------------------------------------------------------
    def _read_body(self, limit):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > limit:
            raise ValueError("请求体过大或为空")
        return self.rfile.read(length)

    def _read_json(self, limit=MAX_JSON_BODY):
        try:
            raw = self._read_body(limit)
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("请求不是有效的 JSON：%s" % exc)
        if not isinstance(data, dict):
            raise ValueError("请求 JSON 必须是一个对象")
        return data

    def _serve_static(self, rel_path, content_type=None):
        path = _safe_join(STATIC_DIR, *rel_path.split("/"))
        if not path or not os.path.isfile(path):
            self.send_error(404, "Not found")
            return
        ext = os.path.splitext(path)[1].lower()
        ctype = content_type or STATIC_TYPES.get(ext,
                                                 "application/octet-stream")
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            self.send_error(404, "Not found")
            return
        send_bytes(self, data, ctype)

    # -- auth ----------------------------------------------------------
    def _session_token(self):
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith(SESSION_COOKIE + "="):
                return part[len(SESSION_COOKIE) + 1:]
        return None

    def _current_user(self):
        return validate_session(self._session_token())

    def _set_session_cookie(self, token):
        if token:
            return [("Set-Cookie",
                     "%s=%s; HttpOnly; Path=/; Max-Age=%d; SameSite=Lax"
                     % (SESSION_COOKIE, token, SESSION_TTL))]
        return [("Set-Cookie",
                 "%s=; HttpOnly; Path=/; Max-Age=0; SameSite=Lax"
                 % SESSION_COOKIE)]

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _redirect_login(self):
        nxt = urllib.parse.quote(self.path, safe="")
        self._redirect("/login?next=" + nxt)

    def _do_login(self):
        try:
            data = self._read_json()
        except ValueError as exc:
            send_json(self, {"ok": False, "error": str(exc)}, status=400)
            return
        username = data.get("username")
        password = data.get("password")
        if (not isinstance(username, str) or not username.strip()
                or not isinstance(password, str) or not password):
            send_json(self, {"ok": False, "error": "请输入账号和密码"},
                      status=400)
            return
        username = username.strip()
        if not verify_login(username, password):
            send_json(self, {"ok": False, "error": "账号或密码错误"},
                      status=401)
            return
        token = create_session(username)
        send_json(self, {"ok": True, "username": username},
                  extra_headers=self._set_session_cookie(token))

    def _do_logout(self):
        destroy_session(self._session_token())
        send_json(self, {"ok": True},
                  extra_headers=self._set_session_cookie(None))

    def _do_change_password(self, user):
        try:
            data = self._read_json()
        except ValueError as exc:
            send_json(self, {"ok": False, "error": str(exc)}, status=400)
            return
        current = data.get("current_password")
        new_username = data.get("new_username")
        new_password = data.get("new_password")
        if (not isinstance(current, str)
                or not isinstance(new_password, str)):
            send_json(self, {"ok": False, "error": "参数无效"}, status=400)
            return
        if not verify_login(user, current):
            send_json(self, {"ok": False, "error": "当前密码不对"},
                      status=401)
            return
        if not new_password:
            send_json(self, {"ok": False, "error": "新密码不能为空"},
                      status=400)
            return
        username = (new_username.strip()
                    if isinstance(new_username, str)
                    and new_username.strip() else user)
        set_auth(username, new_password)
        # 只保留当前会话，其它设备/浏览器要重新登录。
        destroy_other_sessions(self._session_token())
        send_json(self, {"ok": True, "username": username})

    # -- routing ---------------------------------------------------------
    def do_GET(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def _dispatch(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if self.command == "POST":
                self._handle_post(path)
            else:
                self._handle_get(path)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - never crash a request
            sys.stderr.write("mitvremote: request %s %s failed: %r\n"
                             % (self.command, self.path, exc))
            try:
                send_json(self, {"ok": False, "error": str(exc)},
                          status=500)
            except Exception:  # noqa: BLE001 - client already gone
                pass

    def _handle_get(self, path):
        # 公开路径：登录页与静态资源（登录页要加载样式与图标）。
        if path == "/login":
            if self._current_user():
                self._redirect("/")
                return
            self._serve_static("login.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return
        if path.startswith("/icons/"):
            name = path[len("/icons/"):]
            if not _ICON_RE.match(name):
                self.send_error(404, "Not found")
                return
            self._serve_static("icons/" + name, "image/png")
            return
        if path == "/manifest.json":
            self._serve_static("manifest.webmanifest",
                               "application/manifest+json")
            return
        if path == "/sw.js":
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Service-Worker-Allowed", "/")
            with open(os.path.join(STATIC_DIR, "sw.js"), "rb") as fh:
                data = fh.read()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            return
        # 其余页面与接口一律鉴权：页面跳登录页，接口回 401。
        if self._current_user() is None:
            if path in ("/", "/settings"):
                self._redirect_login()
            else:
                send_json(self, {"ok": False, "error": "请先登录"},
                          status=401)
            return
        if path == "/":
            self._serve_static("index.html")
        elif path == "/settings":
            self._serve_static("settings.html")
        elif path == "/api/version":
            send_json(self, {"version": VERSION, "app": APPNAME})
        elif path == "/api/status":
            cfg = get_config_copy()
            st = get_status_copy()
            state = st["state"]
            if not (cfg.get("tv_ip") or "").strip():
                state = "not_configured"
            send_json(self, {
                "tv_name": cfg.get("tv_name") or "",
                "tv_ip": cfg.get("tv_ip") or "",
                "tv_port": cfg.get("tv_port") or DEFAULT_ADB_PORT,
                "has_mac": bool((cfg.get("tv_mac") or "").strip()),
                "state": state,
                "state_text": STATE_TEXT.get(state, state),
                "detail": st["detail"],
                "model": st["model"],
                "latency_ms": st["latency_ms"],
                "checked_at": st["checked_at"],
            })
        elif path == "/api/screenshot":
            png, state, detail = take_screenshot()
            if png is not None:
                send_bytes(self, png, "image/png", extra_headers=[
                    ("Cache-Control", "no-store, no-cache")])
            else:
                # 成功返回 PNG，失败返回 JSON（前端按 Content-Type 区分）。
                status = 429 if state == "busy" else (
                    200 if state == "not_configured" else 503)
                send_json(self, {"ok": False, "state": state,
                                 "error": detail}, status=status)
        elif path == "/api/settings":
            cfg = get_config_copy()
            send_json(self, {
                "tv_name": cfg.get("tv_name") or "",
                "tv_ip": cfg.get("tv_ip") or "",
                "tv_port": cfg.get("tv_port") or DEFAULT_ADB_PORT,
                "tv_mac": cfg.get("tv_mac") or "",
            })
        else:
            self.send_error(404, "Not found")

    def _handle_post(self, path):
        # 登录/退出不需事先登录；其余接口一律鉴权。
        if path == "/api/login":
            self._do_login()
            return
        if path == "/api/logout":
            self._do_logout()
            return
        user = self._current_user()
        if user is None:
            send_json(self, {"ok": False, "error": "请先登录"}, status=401)
            return
        if path == "/api/change-password":
            self._do_change_password(user)
            return
        if path == "/api/key":
            try:
                data = self._read_json()
            except ValueError as exc:
                send_json(self, {"ok": False, "error": str(exc)},
                          status=400)
                return
            key = data.get("key")
            if not isinstance(key, str) or key not in KEY_EVENTS:
                send_json(self, {"ok": False, "error": "未知的按键"},
                          status=400)
                return
            ok, state, detail = send_key(key)
            send_json(self, {"ok": ok, "state": state,
                             "error": "" if ok else detail})
            return
        if path == "/api/power-on":
            result = power_on()
            send_json(self, result)
            return
        if path == "/api/power-off":
            ok, state, detail = send_key("power")
            send_json(self, {"ok": ok, "state": state,
                             "error": "" if ok else detail})
            return
        if path == "/api/wake":
            cfg = get_config_copy()
            mac = (cfg.get("tv_mac") or "").strip()
            if not mac:
                send_json(self, {"ok": False,
                                 "error": "没有填电视 MAC 地址：请先在"
                                          "设置页填 MAC 地址再开机"})
                return
            ok, err = send_wol(mac)
            send_json(self, {"ok": ok, "error": err,
                             "state": "waking" if ok else "error"})
            return
        if path == "/api/test":
            send_json(self, test_connection())
            return
        if path == "/api/discover":
            send_json(self, discover_tvs())
            return
        if path == "/api/settings":
            try:
                data = self._read_json()
            except ValueError as exc:
                send_json(self, {"ok": False, "error": str(exc)},
                          status=400)
                return
            tv_name = (data.get("tv_name") or "").strip()
            tv_ip = (data.get("tv_ip") or "").strip()
            tv_mac = (data.get("tv_mac") or "").strip()
            try:
                tv_port = int(data.get("tv_port"))
            except (TypeError, ValueError):
                tv_port = DEFAULT_ADB_PORT
            if tv_ip and not _valid_host(tv_ip):
                send_json(self, {"ok": False,
                                 "error": "电视 IP 格式不对，如 192.168.1.10"},
                          status=400)
                return
            if not 1 <= tv_port <= 65535:
                send_json(self, {"ok": False,
                                 "error": "ADB 端口应在 1–65535 之间"},
                          status=400)
                return
            if tv_mac and parse_mac(tv_mac) is None:
                send_json(self, {"ok": False,
                                 "error": "MAC 地址格式不对，应为 "
                                          "AA:BB:CC:DD:EE:FF"},
                          status=400)
                return
            with _config_lock:
                _config["tv_name"] = tv_name or "小米电视"
                _config["tv_ip"] = tv_ip
                _config["tv_port"] = tv_port
                _config["tv_mac"] = tv_mac
            save_config()
            _drop_all_devices()
            _set_status("unknown", "")
            send_json(self, {"ok": True})
            return
        self.send_error(404, "Not found")


def main():
    load_config()
    load_sessions()
    port = resolve_port()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    sys.stderr.write("mitvremote %s listening on 0.0.0.0:%d\n"
                     % (VERSION, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
