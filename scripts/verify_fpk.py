#!/usr/bin/env python3
"""Verify a built mitvremote .fpk against the documented fnOS format.

Checks (each printed PASS/FAIL):
  1. Outer gzip-tar members are package-root entries, no top-level dir.
  2. All required outer members exist.
  3. The manifest's checksum line equals the MD5 of the app.tgz
     member bytes (this is fnpack's mechanism; there is no separate
     manifest.checksum member in fnpack output).
  4. manifest parses and declares appname / version / platform=arm /
     service_port=18091.
  5. Inner app.tgz members are the app/ contents (no app/ prefix) and
     include all app files.
  6. Vendored adb-shell / rsa / pyasn1 are present (pure Python).
  7. All Python files in app.tgz compile.
  8. All JSON files in the package parse.
  9. Service Worker cache name contains the current version.
 10. LICENSE is the MIT license text.
"""

import hashlib
import io
import json
import sys
import tarfile

REQUIRED_OUTER = {
    "manifest", "ICON.PNG", "ICON_256.PNG", "LICENSE",
    "app.tgz",
    "cmd/main", "cmd/install_callback", "cmd/uninstall_init",
    "cmd/config_callback", "cmd/upgrade_callback",
    "config/privilege", "config/resource",
    "wizard/install", "wizard/config",
    "wizard/upgrade", "wizard/uninstall",
    "cmd/install_init", "cmd/uninstall_callback", "cmd/upgrade_init",
}

REQUIRED_INNER = {
    "app.py",
    "vendor/adb_shell/__init__.py",
    "vendor/adb_shell/adb_device.py",
    "vendor/adb_shell/auth/sign_pythonrsa.py",
    "vendor/rsa/__init__.py",
    "vendor/pyasn1/__init__.py",
    "static/index.html", "static/settings.html", "static/login.html",
    "static/style.css", "static/remote.js", "static/settings.js",
    "static/sw.js", "static/manifest.webmanifest",
    "static/icons/icon-192.png", "static/icons/icon-512.png",
    "ui/config", "ui/images/icon_64.png", "ui/images/icon_256.png",
}


def check(label, ok, detail=""):
    print("%s %s %s" % ("PASS" if ok else "FAIL", label, detail))
    return ok


def main():
    path = sys.argv[1]
    all_ok = True
    with open(path, "rb") as fh:
        outer_bytes = fh.read()
    outer = tarfile.open(fileobj=io.BytesIO(outer_bytes), mode="r:gz")
    members = outer.getmembers()
    names = [m.name for m in members]
    all_ok &= check(
        "outer members at package root (no top-level dir)",
        all("/" not in n or not n.startswith("mitvremote")
            for n in names) and not any(
                n.startswith("mitvremote-") for n in names),
        "(%d members)" % len(names),
    )
    all_ok &= check(
        "required outer members present",
        REQUIRED_OUTER.issubset(set(names)),
        "missing: %s" % sorted(REQUIRED_OUTER - set(names)),
    )
    blob = {}
    for m in members:
        if m.isfile():
            blob[m.name] = outer.extractfile(m).read()
    app_tgz = blob.get("app.tgz", b"")
    manifest = {}
    for line in blob.get("manifest", b"").decode("utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            manifest[key.strip()] = value.strip()
    actual = hashlib.md5(app_tgz).hexdigest()
    all_ok &= check(
        "manifest checksum line == md5(app.tgz)",
        manifest.get("checksum") == actual,
        "(%s)" % manifest.get("checksum"),
    )
    all_ok &= check(
        "no stale manifest.checksum member (fnpack does not emit one)",
        "manifest.checksum" not in names,
    )
    version = manifest.get("version", "")
    all_ok &= check(
        "manifest fields",
        manifest.get("platform") == "arm"
        and manifest.get("service_port") == "18091"
        and manifest.get("appname") == "mitvremote"
        and manifest.get("source") == "thirdparty",
        str(manifest),
    )
    inner = tarfile.open(fileobj=io.BytesIO(app_tgz), mode="r:gz")
    inner_names = [m.name for m in inner.getmembers()]
    all_ok &= check(
        "inner app.tgz members (app/ contents, no app/ prefix)",
        REQUIRED_INNER.issubset(set(inner_names))
        and not any(n.startswith("app/") for n in inner_names),
        "missing: %s" % sorted(REQUIRED_INNER - set(inner_names)),
    )
    inner_blob = {}
    for m in inner.getmembers():
        if m.isfile():
            inner_blob[m.name] = inner.extractfile(m).read()
    # 7. All Python files compile.
    py_ok = True
    py_bad = []
    for name in inner_names:
        if name.endswith(".py"):
            try:
                compile(inner_blob[name].decode("utf-8"), name, "exec")
            except Exception as exc:  # noqa: BLE001
                py_ok = False
                py_bad.append("%s: %s" % (name, exc))
    all_ok &= check("all Python files in app.tgz compile", py_ok,
                    "; ".join(py_bad))
    # 8. All JSON files parse.
    json_ok = True
    json_bad = []
    for name, data in list(blob.items()) + list(inner_blob.items()):
        if name.endswith(".json") or name in (
                "wizard/install", "wizard/config", "wizard/upgrade",
                "wizard/uninstall", "config/resource",
                "config/privilege", "ui/config"):
            try:
                json.loads(data.decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                json_ok = False
                json_bad.append("%s: %s" % (name, exc))
    all_ok &= check("all JSON files in the package parse", json_ok,
                    "; ".join(json_bad))
    # 9. Service Worker cache name contains the current version.
    sw = inner_blob.get("static/sw.js", b"").decode("utf-8", "replace")
    all_ok &= check(
        "sw.js cache name contains version %s" % version,
        ("mitvremote-v%s" % version) in sw,
        "(cache name line: %s)" % next(
            (ln.strip() for ln in sw.splitlines() if "CACHE_NAME" in ln),
            "?"),
    )
    # 10. LICENSE is MIT.
    lic = blob.get("LICENSE", b"").decode("utf-8", "replace")
    all_ok &= check("LICENSE is MIT", lic.startswith("MIT License"))
    # Icons are RGBA PNGs.
    for icon in ("ICON.PNG", "ICON_256.PNG"):
        data = blob.get(icon, b"")
        # IHDR 布局：签名 8 + 长度 4 + "IHDR" 4 + 宽 4 + 高 4 + 位深 1，
        # 颜色类型在第 25 字节（6 = RGBA）。
        rgba = (len(data) > 25 and data[25:26] == b"\x06"
                and data[:8] == b"\x89PNG\r\n\x1a\n")
        all_ok &= check("%s is RGBA PNG" % icon, rgba)
    print("ALL OK" if all_ok else "FAILURES PRESENT")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
