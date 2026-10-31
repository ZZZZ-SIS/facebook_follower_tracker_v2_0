# -*- coding: utf-8 -*-
"""
Facebook 专页粉丝增长监控 v2.0 (PyQt6)

主要功能：
- PyQt6 现代界面，当前结果在左，设置在右。
- 自动识别 Windows 本机 Chrome / Edge 安装路径。
- 支持单账号固定使用，或多个独立账号配置轮换采集。
- 每个账号配置使用独立浏览器资料目录，避免互相覆盖登录状态。
- 支持“显示浏览器”与“静默执行(headless)”两种采集方式。
- 自动采集 Facebook 页面可见专页名称与粉丝数。
- 自定义采集/提醒间隔、增长阈值、铃声。
- 单个专页本轮提醒可手动停止；重新开始新一轮自动监控时恢复提醒。
- 双击结果表的专页 ID / 名称即可复制。

说明：本软件不会绕过登录验证、验证码或 Facebook 的访问限制。
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import tempfile
import urllib.request
import urllib.parse
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QUrl, QSize
from PyQt6.QtGui import QColor, QGuiApplication, QIcon, QPalette
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer, QSoundEffect
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QFileDialog,
    QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QRadioButton, QScrollArea,
    QSizePolicy, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget, QAbstractItemView,
    QButtonGroup, QInputDialog, QSystemTrayIcon
)

APP_NAME = "Facebook专页粉丝增长监控v2.0"
APP_VERSION = "2.0 PyQt6"

LOCALAPPDATA = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
APP_DIR = LOCALAPPDATA / "FacebookFollowerTracker"
APP_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = APP_DIR / "fb_follower_tracker.db"
SETTINGS_PATH = APP_DIR / "settings_v6_2.json"
ACCOUNT_ROOT = APP_DIR / "browser_accounts"
ACCOUNT_ROOT.mkdir(parents=True, exist_ok=True)

SCRIPT_DIR = Path(__file__).resolve().parent
BUILTIN_BELL = SCRIPT_DIR / "assets" / "bell.wav"
APP_ICON_PNG = SCRIPT_DIR / "assets" / "app_icon.png"
APP_ICON_ICO = SCRIPT_DIR / "assets" / "app_icon.ico"
PINNED_PLAYWRIGHT = "playwright==1.64.1"
ACCOUNT_ID_RE = re.compile(r"^acct_[A-Za-z0-9_-]{1,80}$")


def app_icon() -> QIcon:
    """Return the bundled application icon for windows, taskbar and tray."""
    for candidate in (APP_ICON_ICO, APP_ICON_PNG):
        try:
            if candidate.exists():
                icon = QIcon(str(candidate))
                if not icon.isNull():
                    return icon
        except Exception:
            pass
    return QIcon()


# ------------------------- 数据与设置 -------------------------

@dataclass
class BrowserInstall:
    name: str
    exe: str


@dataclass
class BrowserAccount:
    id: str
    label: str
    browser_name: str
    exe: str
    enabled: bool = True

    @property
    def profile_dir(self) -> Path:
        # Account IDs are persisted in a user-writable settings file, so never
        # use them in filesystem paths before strict validation.
        return ACCOUNT_ROOT / validate_account_id(self.id)


def validate_account_id(account_id: str) -> str:
    value = str(account_id or "")
    if not ACCOUNT_ID_RE.fullmatch(value):
        raise ValueError("无效的浏览器账号 ID。")
    return value


def _canonical_path(path: str | Path) -> str:
    try:
        return os.path.normcase(str(Path(path).resolve(strict=False)))
    except Exception:
        return os.path.normcase(os.path.abspath(str(path)))


def _path_is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except Exception:
        return False


def detect_installed_browsers() -> list[BrowserInstall]:
    """识别常见 Windows Chrome / Edge 安装位置。"""
    env = os.environ
    local = Path(env.get("LOCALAPPDATA", str(LOCALAPPDATA)))
    pf = Path(env.get("PROGRAMFILES", r"C:\Program Files"))
    pfx86 = Path(env.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))

    candidates = [
        ("Google Chrome", local / "Google" / "Chrome" / "Application" / "chrome.exe"),
        ("Google Chrome", pf / "Google" / "Chrome" / "Application" / "chrome.exe"),
        ("Google Chrome", pfx86 / "Google" / "Chrome" / "Application" / "chrome.exe"),
        ("Microsoft Edge", local / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
        ("Microsoft Edge", pf / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
        ("Microsoft Edge", pfx86 / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
    ]

    out: list[BrowserInstall] = []
    seen = set()
    for name, path in candidates:
        try:
            if path.exists():
                key = str(path.resolve()).lower()
                if key not in seen:
                    seen.add(key)
                    out.append(BrowserInstall(name, str(path)))
        except Exception:
            pass

    # Security: do not trust arbitrary chrome.exe/msedge.exe found through PATH.
    # PATH is user-controlled and could otherwise turn browser detection into
    # arbitrary code execution. Only the explicit Chrome/Edge install roots
    # above are accepted.
    return out


def validate_browser_executable(exe: str | Path) -> Path:
    """Allow execution only of Chrome/Edge binaries discovered by this app."""
    requested = _canonical_path(exe)
    allowed = {_canonical_path(b.exe): Path(b.exe) for b in detect_installed_browsers()}
    matched = allowed.get(requested)
    if matched is None:
        raise RuntimeError("浏览器程序路径不在本机受信任的 Chrome/Edge 安装列表中，请重新识别浏览器。")
    if matched.name.lower() not in {"chrome.exe", "msedge.exe"}:
        raise RuntimeError("仅允许启动本机识别到的 Chrome 或 Edge。")
    return matched


def _tail_text(path: Path, max_chars: int = 5000) -> str:
    """读取浏览器日志，尽量避免 Windows 本地编码造成乱码。"""
    try:
        if not path.exists():
            return ""
        data = path.read_bytes()[-max_chars * 4:]
        if not data:
            return ""
        candidates = []
        for enc in ("utf-8", "gb18030", "utf-16le", "cp1252"):
            try:
                text = data.decode(enc, errors="replace")
                # 乱码替换字符越少越优先；同时避免 NUL 过多。
                score = text.count("\ufffd") * 20 + text.count("\x00") * 5
                candidates.append((score, text))
            except Exception:
                pass
        if not candidates:
            return ""
        text = min(candidates, key=lambda x: x[0])[1]
        text = text.replace("\x00", "")
        return text[-max_chars:].strip()
    except Exception:
        return ""


def _free_tcp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _creation_flags() -> int:
    if os.name == "nt":
        return int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    return 0


def _hidden_creation_flags() -> int:
    if os.name == "nt":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def _compact_error(error: Exception | str, limit: int = 900) -> str:
    text = str(error).replace("\r", "").strip()
    # Playwright 的 Call log 很长，只保留真正的首段错误。
    for marker in ("\nCall log:", "\nBrowser logs:", "\n<launching>"):
        if marker in text:
            text = text.split(marker, 1)[0].strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:limit]


def _stop_profile_processes(profile_dir: Path) -> int:
    """仅停止命令行包含本软件账号资料目录的 Chrome/Edge 进程。不会动普通浏览器。"""
    if os.name != "nt":
        return 0
    profile = str(profile_dir.resolve())
    escaped = profile.replace("'", "''")
    ps = (
        "$p='" + escaped + "';"
        "$n=0;"
        "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
        "Where-Object { ($_.Name -ieq 'chrome.exe' -or $_.Name -ieq 'msedge.exe') -and "
        "$_.CommandLine -and $_.CommandLine.IndexOf($p,[System.StringComparison]::OrdinalIgnoreCase) -ge 0 } | "
        "ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $n++ } catch {} };"
        "Write-Output $n"
    )
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=12,
            creationflags=_hidden_creation_flags(),
        )
        out = (r.stdout or "").strip().splitlines()
        return int(out[-1]) if out and out[-1].strip().isdigit() else 0
    except Exception:
        return 0


def _remove_profile_locks(profile_dir: Path) -> None:
    """清理 Chrome/Edge 在异常退出后可能残留的锁文件。"""
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort"):
        p = profile_dir / name
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists() or p.is_symlink():
                p.unlink(missing_ok=True)
        except Exception:
            pass


def _profile_copy_ignore(_dir: str, names: list[str]) -> set[str]:
    skip_exact = {
        "Cache", "Code Cache", "GPUCache", "DawnCache", "GrShaderCache", "ShaderCache",
        "Crashpad", "BrowserMetrics", "OptimizationGuidePredictionModels", "Safe Browsing",
        "SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort",
    }
    return {n for n in names if n in skip_exact or n.endswith(".tmp")}


def _safe_remove_owned_tree(path: Path, root: Path, attempts: int = 4) -> bool:
    """Best-effort removal bounded to an application-owned root."""
    p = Path(path)
    root_resolved = Path(root).resolve(strict=False)
    resolved = p.resolve(strict=False)
    if not _path_is_within(resolved, root_resolved) or resolved == root_resolved:
        return False
    for attempt in range(max(1, attempts)):
        try:
            if not p.exists() and not p.is_symlink():
                return True
            shutil.rmtree(p)
            return True
        except FileNotFoundError:
            return True
        except Exception:
            if attempt + 1 < attempts:
                time.sleep(0.25 * (attempt + 1))
    return not p.exists()


def _purge_stale_runtime_profiles() -> int:
    """Delete stale temporary browser-profile copies left after crashes."""
    runtime_root = APP_DIR / "runtime_profiles"
    if not runtime_root.exists():
        return 0
    removed = 0
    for child in list(runtime_root.iterdir()):
        try:
            # Never follow a link/junction outside the application runtime root.
            if not _path_is_within(child, runtime_root):
                continue
            if child.is_dir():
                _stop_profile_processes(child)
                _remove_profile_locks(child)
                if _safe_remove_owned_tree(child, runtime_root):
                    removed += 1
        except Exception:
            continue
    return removed


def _make_runtime_profile(account: BrowserAccount) -> Path:
    """复制登录资料到唯一的临时运行目录，避免源资料目录锁死。"""
    runtime_root = APP_DIR / "runtime_profiles"
    runtime_root.mkdir(parents=True, exist_ok=True)
    safe_id = validate_account_id(account.id)
    dst = Path(tempfile.mkdtemp(prefix=f"{safe_id}_", dir=str(runtime_root)))
    src = account.profile_dir
    if not src.exists():
        src.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(src, dst, ignore=_profile_copy_ignore, dirs_exist_ok=True)
    except Exception as e:
        _safe_remove_owned_tree(dst, runtime_root)
        raise RuntimeError(f"无法复制账号登录资料用于采集：{e}")
    _remove_profile_locks(dst)
    return dst


def _cleanup_runtime_profile(profile_dir: Path) -> None:
    """Remove only runtime profile copies owned by this application."""
    runtime_root = APP_DIR / "runtime_profiles"
    p = Path(profile_dir)
    if not _path_is_within(p, runtime_root) or p.resolve(strict=False) == runtime_root.resolve(strict=False):
        return
    _stop_profile_processes(p)
    _remove_profile_locks(p)
    _safe_remove_owned_tree(p, runtime_root)


def delete_account_login_profile(account: BrowserAccount) -> bool:
    """Safely delete one account's persisted browser login profile.

    The account configuration itself is preserved. Only the profile directory
    owned by ACCOUNT_ROOT for the validated account ID can be removed.
    """
    validate_account_id(account.id)
    profile = account.profile_dir
    account_root = ACCOUNT_ROOT.resolve(strict=False)
    resolved = profile.resolve(strict=False)
    if not _path_is_within(resolved, account_root) or resolved == account_root:
        raise RuntimeError("拒绝删除：账号登录资料目录超出应用安全范围。")
    if not profile.exists():
        return False
    _stop_profile_processes(profile)
    _remove_profile_locks(profile)
    if not _safe_remove_owned_tree(profile, ACCOUNT_ROOT):
        raise RuntimeError("删除账号登录资料失败；请关闭该账号浏览器后重试。")
    return True


def _browser_args_for_profile(exe: Path, profile_dir: Path, *, headless: bool,
                              debug_port: int | None = None,
                              target_url: str | None = None) -> list[str]:
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        str(exe),
        f"--user-data-dir={profile_dir}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
        "--disable-popup-blocking",
    ]
    if debug_port is not None:
        args += [
            f"--remote-debugging-port={debug_port}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=http://127.0.0.1",
        ]
    if headless:
        args += ["--headless=new", "--disable-gpu", "--window-size=1365,900"]
    else:
        args += ["--new-window", "--window-size=1365,900"]
    if target_url:
        args.append(target_url)
    return args


def _browser_args(account: BrowserAccount, *, headless: bool, debug_port: int | None = None,
                  target_url: str | None = None) -> list[str]:
    return _browser_args_for_profile(
        Path(account.exe), account.profile_dir, headless=headless,
        debug_port=debug_port, target_url=target_url
    )


def open_login_browser_direct(account: BrowserAccount) -> subprocess.Popen:
    """直接用本机 Chrome/Edge 打开账号专用资料目录。"""
    validate_account_id(account.id)
    exe = validate_browser_executable(account.exe)
    account.profile_dir.mkdir(parents=True, exist_ok=True)
    # 只清理这个账号以前遗留的后台进程，避免资料目录被自己锁住。
    _stop_profile_processes(account.profile_dir)
    _remove_profile_locks(account.profile_dir)
    log_path = account.profile_dir / "login_browser.log"
    # Keep only the current session's browser diagnostics. Historical browser
    # stderr can contain URLs and should not accumulate indefinitely.
    logf = open(log_path, "wb", buffering=0)
    try:
        proc = subprocess.Popen(
            _browser_args_for_profile(
                exe, account.profile_dir, headless=False,
                target_url="https://www.facebook.com/"
            ),
            stdout=logf,
            stderr=logf,
            creationflags=_creation_flags(),
        )
    finally:
        logf.close()
    time.sleep(0.8)
    return proc


def _try_cdp_profile(p, exe: Path, profile_dir: Path, *, headless: bool,
                     log_path: Path, timeout_seconds: int = 25):
    """以指定资料目录启动 Chrome/Edge，并通过 CDP 接管。"""
    _stop_profile_processes(profile_dir)
    _remove_profile_locks(profile_dir)
    port = _free_tcp_port()
    logf = open(log_path, "wb", buffering=0)
    try:
        proc = subprocess.Popen(
            _browser_args_for_profile(
                exe, profile_dir, headless=headless,
                debug_port=port, target_url="about:blank"
            ),
            stdout=logf,
            stderr=logf,
            creationflags=_creation_flags(),
        )
    finally:
        logf.close()

    endpoint = f"http://127.0.0.1:{port}"
    last_error = ""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(endpoint + "/json/version", timeout=0.8) as resp:
                if resp.status == 200:
                    browser = p.chromium.connect_over_cdp(
                        endpoint, timeout=20000, headers={"Origin": "http://127.0.0.1"}
                    )
                    if not browser.contexts:
                        raise RuntimeError("浏览器已启动，但没有可用的浏览器上下文。")
                    return proc, browser, browser.contexts[0]
        except Exception as e:
            last_error = _compact_error(e, 400)
        time.sleep(0.3)

    # 清掉本次失败的、且只属于这个资料目录的浏览器进程。
    _stop_profile_processes(profile_dir)
    return None, None, last_error or "调试端口未在规定时间内就绪"


def launch_cdp_browser(p, account: BrowserAccount, *, headless: bool):
    """
    三层启动：
    1) 清理账号专用资料目录残留进程后，直接用源资料目录 CDP；
    2) 自动复制登录状态到运行时资料目录，再用 CDP；
    3) 对运行时资料目录使用 Playwright persistent context。
    """
    validate_account_id(account.id)
    exe = validate_browser_executable(account.exe)

    account.profile_dir.mkdir(parents=True, exist_ok=True)
    source_log = account.profile_dir / "collection_browser.log"

    # 第一通道：源账号资料目录。
    proc, browser, err1 = _try_cdp_profile(
        p, exe, account.profile_dir, headless=headless,
        log_path=source_log, timeout_seconds=22
    )
    if browser is not None:
        context = browser.contexts[0]
        return proc, browser, context, account.profile_dir

    # 第二通道：运行时快照，避开源资料目录锁与残留后台进程。
    runtime_dir = _make_runtime_profile(account)
    runtime_log = runtime_dir / "collection_browser.log"
    proc2, browser2, err2 = _try_cdp_profile(
        p, exe, runtime_dir, headless=headless,
        log_path=runtime_log, timeout_seconds=25
    )
    if browser2 is not None:
        context2 = browser2.contexts[0]
        return proc2, browser2, context2, runtime_dir

    # 第三通道：Playwright 对运行时快照直接启动。
    _stop_profile_processes(runtime_dir)
    _remove_profile_locks(runtime_dir)
    try:
        context3 = p.chromium.launch_persistent_context(
            user_data_dir=str(runtime_dir),
            executable_path=str(exe),
            headless=headless,
            args=[
                "--profile-directory=Default",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-mode",
                "--disable-session-crashed-bubble",
                "--hide-crash-restore-bubble",
            ],
            viewport={"width": 1365, "height": 900},
            timeout=30000,
        )
        return None, None, context3, runtime_dir
    except Exception as fallback_error:
        _stop_profile_processes(runtime_dir)
        detail1 = _tail_text(source_log, 1800)
        detail2 = _tail_text(runtime_log, 1800)
        parts = [
            "浏览器启动失败，已自动尝试 3 种启动方式。",
            f"① 账号资料目录调试启动：{err1}",
            f"② 临时资料副本调试启动：{err2}",
            f"③ 临时资料副本备用启动：{_compact_error(fallback_error)}",
        ]
        # 日志只附简短尾部，避免巨大弹窗。
        detail = detail2 or detail1
        if detail:
            clean = re.sub(r"[^\x09\x0A\x0D\x20-\x7E\u4e00-\u9fff]+", "?", detail)
            parts.append("浏览器日志尾部：" + clean[-700:])
        parts.append(
            "建议：先关闭该账号打开的 Chrome/Edge 窗口；如果仍失败，点一次“登录选中账号”，"
            "确认能正常进入 Facebook 后关闭该窗口，再重新采集。"
        )
        _cleanup_runtime_profile(runtime_dir)
        raise RuntimeError("\n".join(parts))

def default_settings() -> dict:
    return {
        "background": "#F4F7FB",
        "interval_value": 1,
        "interval_unit": "分钟",
        "duration_value": 24,
        "duration_mode": "无限",
        "show_browser": True,
        "account_mode": "single",
        "alert_enabled": True,
        "alert_threshold": 500,
        "sound_mode": "builtin",
        "sound_path": "",
        "page_ids": "",
        "accounts": [],
        "google_sheet_url": "",
        "google_sheet_tab": "当前结果",
        "google_credentials_path": "",
        "google_auto_sync": False,
    }


def load_settings() -> dict:
    d = default_settings()
    try:
        if SETTINGS_PATH.exists():
            saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                d.update(saved)
    except Exception:
        pass
    return d


def save_settings(d: dict):
    try:
        SETTINGS_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def parse_page_ids(text: str) -> list[str]:
    parts = re.split(r"[\s,;，；]+", text.strip())
    out, seen = [], set()
    for p in parts:
        if not p:
            continue
        p = p.strip()
        if not re.fullmatch(r"\d+", p):
            continue
        if p not in seen:
            out.append(p)
            seen.add(p)
    return out


def interval_seconds(value: int, unit: str) -> int:
    return int(value) * {"秒": 1, "分钟": 60, "小时": 3600}.get(unit, 60)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _csv_safe_cell(value):
    """Prevent spreadsheet formula execution when exported CSV is opened."""
    if not isinstance(value, str):
        return value
    probe = value.lstrip(" " + "\t\r\n" + "\ufeff")
    if probe.startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self._init()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=20)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init(self):
        with self._connect() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    collected_at TEXT NOT NULL,
                    page_id TEXT NOT NULL,
                    page_name TEXT,
                    followers_count INTEGER,
                    fan_count INTEGER,
                    source_metric TEXT,
                    api_version TEXT
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_page_time ON snapshots(page_id, id)")
            cols = {r[1] for r in c.execute("PRAGMA table_info(snapshots)")}
            for col, decl in [
                ("page_name", "TEXT"), ("followers_count", "INTEGER"),
                ("fan_count", "INTEGER"), ("source_metric", "TEXT"),
                ("api_version", "TEXT")
            ]:
                if col not in cols:
                    c.execute(f"ALTER TABLE snapshots ADD COLUMN {col} {decl}")

    def latest(self, page_id: str):
        with self.lock, self._connect() as c:
            row = c.execute("""
                SELECT collected_at, page_name, followers_count
                FROM snapshots WHERE page_id=? AND followers_count IS NOT NULL
                ORDER BY id DESC LIMIT 1
            """, (page_id,)).fetchone()
            return row

    def add(self, page_id: str, name: str, followers: int):
        with self.lock, self._connect() as c:
            c.execute("""
                INSERT INTO snapshots(collected_at,page_id,page_name,followers_count,source_metric,api_version)
                VALUES(?,?,?,?,?,?)
            """, (now_iso(), page_id, name, followers, "网页粉丝数", "browser-v6.7"))

    def export_csv(self, path: str):
        with self.lock, self._connect() as c, open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["collected_at", "page_id", "page_name", "followers_count", "source_metric"])
            for row in c.execute("""
                SELECT collected_at,page_id,page_name,followers_count,source_metric
                FROM snapshots ORDER BY id
            """):
                w.writerow([_csv_safe_cell(cell) for cell in row])


# ------------------------- 网页解析 -------------------------

# Facebook 页面里经常出现大量“看起来像标题”的 UI 文案。专页名称识别必须尽量
# 依赖身份区域，而不是把任意 H1/H2/大字号文本都当成名称。
BAD_NAME_EXACT = {
    "facebook", "全部", "all", "通知", "notifications", "详细信息", "详情",
    "首页", "home", "帖子", "posts", "照片", "photos", "关于", "about",
    "更多", "more", "筛选", "filter", "最近", "recent", "最相关", "most relevant",
    "精选", "featured", "查看全部", "see all", "关注", "following", "followers",
    "聊天", "chat", "message", "messages", "消息", "联系方式", "contact info", "contact",
    "公众人物主页", "公众人物", "public figure", "public figure page", "个人博客", "personal blog",
    "数字创作者", "digital creator", "社区", "community", "主页", "page", "简介", "intro",
}

BAD_NAME_PREFIXES = (
    "可能是包含下列内容的图片", "可能是包含以下内容的图片", "may be an image of",
    "image may contain", "照片中可能有", "图片中可能有", "查看 ", "see ",
)

BAD_NAME_CONTAINS = (
    "位粉丝", "个粉丝", " followers", " follower", "人关注", "关注者",
    "赞 ·", " likes", "人赞", "条评论", " comments", "次分享", " shares",
)


def clean_name(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"\s+", " ", str(s)).strip()
    # 浏览器标题前面的未读通知，如 (20+) Page Name | Facebook
    s = re.sub(r"^\(\s*\d+\+?\s*\)\s*", "", s).strip()
    # 图片 alt 常见“XXX 的头像 / XXX profile picture”
    s = re.sub(r"\s*(?:的)?(?:头像|大头贴|profile picture|profile photo)\s*$", "", s, flags=re.I).strip()
    # 页面标题尾部 Facebook
    s = re.sub(r"\s*[|\-–—]\s*Facebook\s*$", "", s, flags=re.I).strip()
    s = re.sub(r"\s*[-–—]?\s*Facebook\s*$", "", s, flags=re.I).strip()
    # 极少数标题会带“· Facebook”
    s = re.sub(r"\s*[·•]\s*Facebook\s*$", "", s, flags=re.I).strip()
    return s


def valid_name(s: str) -> bool:
    s = clean_name(s)
    if not s or len(s) < 2 or len(s) > 100:
        return False
    low = s.casefold().strip()
    if low in {x.casefold() for x in BAD_NAME_EXACT}:
        return False
    if any(low.startswith(x.casefold()) for x in BAD_NAME_PREFIXES):
        return False
    if any(x.casefold() in low for x in BAD_NAME_CONTAINS):
        return False
    # (20+)、99+、1,234 等通知/统计数字绝不能作为名称。
    if re.fullmatch(r"[\s()\[\]{}+\-–—\d,.万億亿kKmM]+", s):
        return False
    # 只包含图标/emoji 的候选不要。
    if not any(ch.isalpha() for ch in s):
        return False
    stat_patterns = [
        r"关注\s*[\d,.万億亿kKmM]+\s*人?", r"[\d,.万億亿kKmM]+\s*人?\s*关注",
        r"[\d,.万億亿kKmM]+\s*(followers?|following|likes?)",
        r"[\d,.万億亿kKmM]+\s*(位|个)?粉丝", r"[\d,.万億亿kKmM]+\s*(个)?赞",
    ]
    if any(re.search(p, low, re.I) for p in stat_patterns):
        return False
    return True


def parse_compact_number(raw: str) -> Optional[int]:
    """Parse Facebook counts such as 1,732 / 2.6万 / 3.2K / 1.1M."""
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace("，", "")
    s = re.sub(r"\s+", "", s)
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([kKmM]|万|萬|億|亿)?", s)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except Exception:
        return None
    suffix = m.group(2) or ""
    mul = {
        "k": 1_000, "K": 1_000,
        "m": 1_000_000, "M": 1_000_000,
        "万": 10_000, "萬": 10_000,
        "億": 100_000_000, "亿": 100_000_000,
    }.get(suffix, 1)
    value = int(round(num * mul))
    return value if value >= 0 else None


FOLLOWER_TOKEN = r"[0-9][0-9,，]*(?:\.[0-9]+)?\s*(?:[kKmM]|万|萬|億|亿)?"
FOLLOWER_PATTERNS = [
    re.compile(rf"(?P<num>{FOLLOWER_TOKEN})\s*(?:位|个)?\s*粉丝", re.I),
    re.compile(rf"(?P<num>{FOLLOWER_TOKEN})\s*(?:followers?|follower)\b", re.I),
    re.compile(rf"粉丝\s*[:：]?\s*(?P<num>{FOLLOWER_TOKEN})", re.I),
    re.compile(rf"followers?\s*[:：]?\s*(?P<num>{FOLLOWER_TOKEN})", re.I),
]


def follower_matches(text: str) -> list[dict]:
    """Return all follower-count candidates in one text fragment."""
    out = []
    if not text:
        return out
    for pat in FOLLOWER_PATTERNS:
        for m in pat.finditer(str(text)):
            token = re.sub(r"\s+", "", m.group("num"))
            n = parse_compact_number(token)
            if n is None:
                continue
            approximate = bool(re.search(r"[kKmM万萬億亿]", token))
            out.append({
                "value": int(n),
                "raw": m.group(0).strip(),
                "token": token,
                "approx": approximate,
            })
    return out


def choose_follower_count(follower_nodes: list[dict], body: str) -> tuple[Optional[int], str, bool]:
    """
    Choose the Page follower count from several visible candidates.

    Priority:
    1) short visible DOM nodes in the top/profile area;
    2) exact counts over compact approximations when both are from the same area;
    3) early body lines only as fallback.
    """
    candidates: list[tuple[float, int, str, bool]] = []

    for item in follower_nodes or []:
        text = str(item.get("text") or "").strip()
        y = float(item.get("y") or 99999)
        for match in follower_matches(text):
            score = 300.0
            # Header/profile text is normally near the top of the main content.
            if 0 <= y <= 1200:
                score += max(0.0, 120.0 - y / 10.0)
            if len(text) <= 60:
                score += 35
            elif len(text) <= 120:
                score += 15
            # Exact 1,732 is preferable to 1.7K when both represent the header.
            if not match["approx"]:
                score += 25
            candidates.append((score, match["value"], match["raw"], match["approx"]))

    # Fallback to body lines. Do NOT simply use the first occurrence: posts and sidebars
    # can contain unrelated follower counts.
    lines = [re.sub(r"\s+", " ", x).strip() for x in (body or "").splitlines() if x.strip()]
    for i, line in enumerate(lines[:500]):
        ms = follower_matches(line)
        if not ms:
            continue
        for match in ms:
            score = 120.0
            if i < 80:
                score += 60
            elif i < 180:
                score += 25
            if len(line) <= 60:
                score += 20
            if not match["approx"]:
                score += 15
            candidates.append((score, match["value"], match["raw"], match["approx"]))

    if not candidates:
        return None, "", False

    candidates.sort(key=lambda x: x[0], reverse=True)
    score, value, raw, approximate = candidates[0]

    # Consistency guard: a compact token must actually scale. This specifically prevents
    # cases like “2.6 万位粉丝” accidentally becoming 3.
    if approximate:
        token_match = None
        for m in follower_matches(raw):
            token_match = m
            break
        if token_match and token_match["value"] != value:
            value = token_match["value"]

    return int(value), raw, bool(approximate)


def page_url(page_id: str) -> str:
    return f"https://www.facebook.com/{page_id}"


def scrape_one(page, page_id: str) -> dict:
    page.goto(page_url(page_id), wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_timeout(4500)
    except Exception:
        pass
    try:
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
    except Exception:
        pass

    data = page.evaluate(r"""
    (pageId) => {
      const txt = (el) => (el && (el.innerText || el.textContent) || '').trim();
      const getMeta = (sel) => {
        const el = document.querySelector(sel); return el ? (el.content || '') : '';
      };
      const visible = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
      };

      const h1 = [...document.querySelectorAll('main h1, [role="main"] h1, h1')]
        .filter(visible).map(txt).filter(Boolean).slice(0,20);
      const h2 = [...document.querySelectorAll('main h2, [role="main"] h2')]
        .filter(visible).map(txt).filter(Boolean).slice(0,30);

      const ownLinks = [...document.querySelectorAll('a[role="link"], a')]
        .filter(a => visible(a) && (((a.href || '').includes('/' + pageId)) || ((a.href || '').includes('profile.php?id=' + pageId))))
        .map(txt).filter(Boolean).slice(0,40);

      const imgAlts = [...document.querySelectorAll('main img[alt], [role="main"] img[alt], img[alt]')]
        .filter(visible).map(x => x.alt || '').filter(Boolean).slice(0,80);

      // Collect large visible text near the top only as a low-priority fallback.
      const headerTexts = [...document.querySelectorAll('main span, main div, [role="main"] span, [role="main"] div')]
        .filter(el => {
          if (!visible(el)) return false;
          const t = txt(el);
          if (!t || t.length > 90 || t.includes('\n')) return false;
          const r = el.getBoundingClientRect();
          if (r.top < 0 || r.top > 1200) return false;
          const fs = parseFloat(getComputedStyle(el).fontSize || '0');
          return fs >= 18;
        })
        .map(el => ({text: txt(el), y: el.getBoundingClientRect().top, fs: parseFloat(getComputedStyle(el).fontSize || '0')}))
        .slice(0,120);

      // Locate visible follower text nodes. We intentionally keep their vertical position,
      // because the Page's own follower count is normally in the profile header, while
      // unrelated counts in posts appear much lower on the page.
      const followerRe = /(\d[\d,.]*\s*(?:[kKmM]|万|萬|億|亿)?\s*(?:位|个)?\s*粉丝)|(\d[\d,.]*\s*(?:[kKmM]|万|萬|億|亿)?\s*followers?\b)/i;
      const followerNodes = [...document.querySelectorAll('main span, main a, main div, [role="main"] span, [role="main"] a, [role="main"] div')]
        .filter(el => {
          if (!visible(el)) return false;
          const t = txt(el);
          if (!t || t.length > 140 || t.includes('\n')) return false;
          return followerRe.test(t);
        })
        .map(el => ({text: txt(el), y: el.getBoundingClientRect().top, tag: el.tagName}))
        .sort((a,b) => a.y - b.y)
        .slice(0,100);

      // Text immediately around a follower node is useful for finding the Page name when
      // metadata is generic. Keep it separate so Python can score it below metadata.
      const nearbyFollowerTexts = [];
      const followerEls = [...document.querySelectorAll('main span, main a, [role="main"] span, [role="main"] a')]
        .filter(el => visible(el) && followerRe.test(txt(el)) && txt(el).length <= 140)
        .slice(0,10);
      for (const el of followerEls) {
        let p = el.parentElement;
        for (let depth=0; p && depth<4; depth++, p=p.parentElement) {
          for (const child of [...p.querySelectorAll(':scope > a, :scope > span, :scope > div')]) {
            if (!visible(child)) continue;
            const t = txt(child);
            if (t && t.length <= 100 && !t.includes('\n')) nearbyFollowerTexts.push(t);
          }
        }
      }

      const body = (document.body && document.body.innerText) ? document.body.innerText : '';
      return {
        title: document.title || '',
        og: getMeta('meta[property="og:title"]'),
        twitter: getMeta('meta[name="twitter:title"]'),
        h1, h2, ownLinks, imgAlts, headerTexts, followerNodes,
        nearbyFollowerTexts: nearbyFollowerTexts.slice(0,80),
        body: body.slice(0, 220000)
      };
    }
    """, page_id)

    # Name scoring: identity metadata first. Generic UI headings are deliberately lower.
    candidates: list[tuple[int, str, str]] = []
    for s in [data.get("og", "")]: candidates.append((180, s, "og:title"))
    for s in [data.get("twitter", "")]: candidates.append((170, s, "twitter:title"))
    for s in [data.get("title", "")]: candidates.append((165, s, "document.title"))
    for s in data.get("ownLinks", []): candidates.append((150, s, "own-link"))
    for s in data.get("h1", []): candidates.append((135, s, "h1"))
    for s in data.get("imgAlts", []): candidates.append((120, s, "image-alt"))
    for s in data.get("nearbyFollowerTexts", []): candidates.append((105, s, "near-followers"))
    for item in data.get("headerTexts", []):
        # Larger text receives a modest bonus, but remains below identity sources.
        fs = float(item.get("fs") or 0)
        y = float(item.get("y") or 99999)
        score = 85 + min(20, int(max(0, fs - 18)))
        if y < 600:
            score += 8
        candidates.append((score, item.get("text", ""), "header-text"))
    for s in data.get("h2", []): candidates.append((70, s, "h2"))

    best_name, best_score, best_source = "", -1, ""
    seen_names = set()
    for score, s, source in candidates:
        c = clean_name(s)
        key = c.casefold()
        if key in seen_names:
            continue
        seen_names.add(key)
        if valid_name(c) and score > best_score:
            best_name, best_score, best_source = c, score, source

    # Very conservative body fallback: only early short lines, and only if identity sources
    # produced nothing. This prevents “聊天/联系方式/图片说明” from taking over names.
    body = data.get("body", "") or ""
    if not best_name:
        lines = [re.sub(r"\s+", " ", x).strip() for x in body.splitlines() if x.strip()]
        for s in lines[:80]:
            if valid_name(s) and len(s) <= 60:
                best_name = clean_name(s)
                best_source = "body-fallback"
                break

    followers, raw, approximate = choose_follower_count(data.get("followerNodes", []), body)
    if followers is None:
        # Metadata fallback is only for rare layouts.
        for s in [data.get("og", ""), data.get("title", "")]:
            ms = follower_matches(s)
            if ms:
                followers, raw, approximate = ms[0]["value"], ms[0]["raw"], ms[0]["approx"]
                break

    if followers is None:
        raise RuntimeError("未在网页专页头部识别到粉丝数；请确认已登录、页面可访问。")

    return {
        "page_id": page_id,
        "page_name": best_name,
        "name_source": best_source,
        "followers": int(followers),
        "raw": raw,
        "approximate": approximate,
    }

def _pip_install_playwright() -> tuple[bool, str]:
    """Best-effort install using the project's pinned requirements/constraints."""
    commands = []
    requirements = SCRIPT_DIR / "requirements.txt"
    constraints = SCRIPT_DIR / "constraints-security.txt"
    if not requirements.exists() or not constraints.exists():
        return False, "缺少 requirements.txt 或 constraints-security.txt，拒绝执行未固定版本的在线安装。"

    pip_args = [
        "-m", "pip", "install", "--disable-pip-version-check", "--only-binary=:all:",
        "-r", str(requirements), "-c", str(constraints),
    ]
    if getattr(sys, "frozen", False):
        # A bundled EXE cannot safely mutate its own environment. The release
        # build must contain dependencies at build time.
        return False, "打包版缺少 Playwright；请重新使用固定依赖构建 EXE。"

    commands.append([sys.executable, *pip_args])
    if os.name == "nt":
        commands.append(["py", *pip_args])

    errors = []
    for cmd in commands:
        try:
            cp = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=180,
                creationflags=_creation_flags(),
            )
            if cp.returncode == 0:
                return True, (cp.stdout or cp.stderr or "安装成功")[-3000:]
            errors.append((cp.stderr or cp.stdout or f"exit={cp.returncode}")[-2000:])
        except Exception as e:
            errors.append(str(e))
    return False, "\n".join(errors[-3:]) or "无法启动 pip 安装。"


def _load_sync_playwright(status_cb=None):
    """Import Playwright; if missing, try to install it automatically once."""
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except Exception as first_error:
        if status_cb:
            status_cb("未检测到 Playwright，正在自动安装运行组件…")
        ok, detail = _pip_install_playwright()
        if not ok:
            raise RuntimeError(
                "当前 Python 环境没有安装 Playwright，自动安装也失败。\n"
                "请关闭软件后运行压缩包里的 install_windows.bat。\n"
                f"安装详情：{detail}"
            ) from first_error
        # Invalidate import caches and retry in the same process.
        import importlib
        importlib.invalidate_caches()
        try:
            from playwright.sync_api import sync_playwright
            if status_cb:
                status_cb("Playwright 已自动安装，继续启动浏览器…")
            return sync_playwright
        except Exception as second_error:
            raise RuntimeError(
                "Playwright 已安装，但当前软件进程仍无法加载。请关闭软件后重新运行 start_windows.bat。"
            ) from second_error

GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
MAX_SERVICE_ACCOUNT_JSON_BYTES = 1024 * 1024


def load_validated_service_account_info(path: str | Path) -> dict:
    """Load only a standard Google service-account credential file.

    In particular, token_uri is restricted to Google's official endpoint so a
    modified JSON file cannot redirect signed OAuth assertions to another host.
    """
    p = Path(path)
    try:
        if not p.is_file():
            raise ValueError("服务账号 JSON 文件不存在。")
        size = p.stat().st_size
    except OSError as e:
        raise ValueError(f"无法读取服务账号 JSON：{e}") from e
    if size <= 0 or size > MAX_SERVICE_ACCOUNT_JSON_BYTES:
        raise ValueError("服务账号 JSON 文件大小异常。")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError("服务账号 JSON 不是有效的 UTF-8 JSON 文件。") from e
    if not isinstance(data, dict) or data.get("type") != "service_account":
        raise ValueError("请选择 Google Cloud 生成的 service_account JSON。")
    email = str(data.get("client_email") or "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.iam\.gserviceaccount\.com", email):
        raise ValueError("服务账号 JSON 的 client_email 格式不正确。")
    private_key = str(data.get("private_key") or "")
    if "-----BEGIN PRIVATE KEY-----" not in private_key or "-----END PRIVATE KEY-----" not in private_key:
        raise ValueError("服务账号 JSON 缺少有效的 private_key。")
    if str(data.get("token_uri") or "").strip() != GOOGLE_TOKEN_URI:
        raise ValueError("服务账号 JSON 的 token_uri 不是 Google 官方令牌端点，已拒绝使用。")
    return data


# ------------------------- Google Sheets -------------------------

def parse_google_sheet_id(value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError("请填写 Google 表格链接。")
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", text):
        return text
    raise ValueError("Google 表格链接格式不正确。请粘贴完整 Google Sheets 链接。")


def _google_sheet_range(tab_name: str, cell_range: str) -> str:
    safe_tab = (tab_name or "当前结果").replace("'", "''")
    return f"'{safe_tab}'!{cell_range}"


class GoogleSheetWorker(QThread):
    status = pyqtSignal(str)
    success = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, sheet_url: str, tab_name: str, credentials_path: str,
                 values: list[list], parent=None):
        super().__init__(parent)
        self.sheet_url = sheet_url
        self.tab_name = (tab_name or "当前结果").strip() or "当前结果"
        self.credentials_path = credentials_path
        self.values = values

    def _raise_http(self, response, prefix: str):
        if 200 <= response.status_code < 300:
            return
        detail = ""
        try:
            data = response.json()
            detail = data.get("error", {}).get("message", "")
        except Exception:
            detail = (response.text or "")[:500]
        raise RuntimeError(f"{prefix}（HTTP {response.status_code}）：{detail or 'Google API 请求失败'}")

    def run(self):
        try:
            cred_path = Path(self.credentials_path)
            credential_info = load_validated_service_account_info(cred_path)
            spreadsheet_id = parse_google_sheet_id(self.sheet_url)
            self.status.emit("正在连接 Google 表格…")
            try:
                from google.oauth2 import service_account
                from google.auth.transport.requests import AuthorizedSession
            except Exception as e:
                raise RuntimeError(
                    "缺少 Google API 运行组件。请先运行 install_windows.bat 安装 google-auth 和 requests。"
                ) from e

            scopes = ["https://www.googleapis.com/auth/spreadsheets"]
            creds = service_account.Credentials.from_service_account_info(
                credential_info, scopes=scopes
            )
            session = AuthorizedSession(creds)
            base = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"

            meta = session.get(base + "?fields=sheets.properties.title", timeout=30)
            self._raise_http(meta, "无法打开 Google 表格")
            titles = {
                sh.get("properties", {}).get("title", "")
                for sh in meta.json().get("sheets", [])
            }
            if self.tab_name not in titles:
                self.status.emit(f"工作表“{self.tab_name}”不存在，正在自动创建…")
                add = session.post(
                    base + ":batchUpdate",
                    json={"requests": [{"addSheet": {"properties": {"title": self.tab_name}}}]},
                    timeout=30,
                )
                self._raise_http(add, "创建工作表失败")

            # 先清空该工作表旧结果，再完整写入当前结果。
            clear_range = urllib.parse.quote(
                _google_sheet_range(self.tab_name, "A:Z"), safe="'!:"
            )
            clear = session.post(base + f"/values/{clear_range}:clear", json={}, timeout=30)
            self._raise_http(clear, "清空旧结果失败")

            if self.values:
                end_row = len(self.values)
                update_range = urllib.parse.quote(
                    _google_sheet_range(self.tab_name, f"A1:H{end_row}"), safe="'!:"
                )
                update = session.put(
                    base + f"/values/{update_range}",
                    params={"valueInputOption": "RAW"},
                    json={
                        "range": _google_sheet_range(self.tab_name, f"A1:H{end_row}"),
                        "majorDimension": "ROWS",
                        "values": self.values,
                    },
                    timeout=30,
                )
                self._raise_http(update, "写入 Google 表格失败")

            self.success.emit(
                f"Google 表格同步完成：{self.tab_name}，写入 {max(0, len(self.values)-1)} 条结果。"
            )
        except Exception as e:
            self.failed.emit(_compact_error(e, 1200))


# ------------------------- Playwright 线程 -------------------------

class BatchWorker(QThread):
    row = pyqtSignal(dict)
    status = pyqtSignal(str)
    fatal = pyqtSignal(str)
    finished_batch = pyqtSignal()

    def __init__(self, page_ids: list[str], accounts: list[BrowserAccount], show_browser: bool,
                 store: Store, rotation_start: int = 0, parent=None):
        super().__init__(parent)
        self.page_ids = page_ids
        self.accounts = accounts
        self.show_browser = show_browser
        self.store = store
        self.rotation_start = max(0, int(rotation_start))
        self._stop = threading.Event()

    def request_stop(self):
        self._stop.set()

    def run(self):
        try:
            sync_playwright = _load_sync_playwright(self.status.emit)
        except Exception as e:
            msg = str(e)
            self.status.emit(msg)
            self.fatal.emit(msg)
            self.finished_batch.emit()
            return

        if not self.accounts:
            msg = "没有可用的浏览器账号配置。"
            self.status.emit(msg)
            self.fatal.emit(msg)
            self.finished_batch.emit()
            return

        # 真正的轮换：不仅同一轮内不同专页交替使用账号，
        # 下一轮还会从下一个账号开始。这样即使只有 1 个专页 ID，
        # 也会出现：第1轮账号1 -> 第2轮账号2 -> 第3轮账号3 -> ...
        assignments: dict[str, list[str]] = {a.id: [] for a in self.accounts}
        for i, pid in enumerate(self.page_ids):
            account = self.accounts[(self.rotation_start + i) % len(self.accounts)]
            assignments[account.id].append(pid)

        plan_parts = []
        for i, pid in enumerate(self.page_ids):
            account = self.accounts[(self.rotation_start + i) % len(self.accounts)]
            plan_parts.append(f"{pid}->{account.label}")
        if plan_parts:
            self.status.emit("本轮账号分配：" + "；".join(plan_parts[:8]) + ("；..." if len(plan_parts) > 8 else ""))

        try:
            started_accounts = 0
            startup_errors = []
            with sync_playwright() as p:
                for account in self.accounts:
                    if self._stop.is_set():
                        break
                    ids = assignments.get(account.id, [])
                    if not ids:
                        continue

                    self.status.emit(
                        f"正在启动 {account.label} / {account.browser_name}："
                        + ("显示浏览器" if self.show_browser else "静默执行")
                    )
                    proc = None
                    browser = None
                    context = None
                    active_profile_dir = None
                    try:
                        proc, browser, context, active_profile_dir = launch_cdp_browser(
                            p, account, headless=not self.show_browser
                        )
                        page = context.pages[0] if context.pages else context.new_page()
                        started_accounts += 1
                        self.status.emit(f"{account.label} 浏览器启动成功，开始采集 {len(ids)} 个专页。")

                        for idx, pid in enumerate(ids, 1):
                            if self._stop.is_set():
                                break
                            self.status.emit(f"[{account.label}] 正在采集 {idx}/{len(ids)}：{pid}")
                            try:
                                previous = self.store.latest(pid)
                                result = scrape_one(page, pid)
                                prev_count = int(previous[2]) if previous and previous[2] is not None else None
                                current = result["followers"]
                                delta = None if prev_count is None else current - prev_count
                                self.store.add(pid, result["page_name"], current)
                                self.row.emit({
                                    **result,
                                    "delta": delta,
                                    "collected_at": now_iso(),
                                    "status": "成功" + (
                                        f"；网页显示约值：{result['raw']} ≈ {current:,}"
                                        if result.get("approximate")
                                        else ""
                                    ),
                                    "account": account.label,
                                })
                            except Exception as e:
                                self.row.emit({
                                    "page_id": pid,
                                    "page_name": "",
                                    "followers": None,
                                    "delta": None,
                                    "collected_at": now_iso(),
                                    "status": f"失败：{e}",
                                    "account": account.label,
                                })
                            if idx < len(ids):
                                time.sleep(0.5)

                    except Exception as e:
                        msg = str(e)
                        startup_errors.append(f"{account.label}：{msg}")
                        self.status.emit(f"{account.label} 浏览器启动失败：{msg}")
                        for pid in ids:
                            self.row.emit({
                                "page_id": pid,
                                "page_name": "",
                                "followers": None,
                                "delta": None,
                                "collected_at": now_iso(),
                                "status": f"浏览器启动失败：{msg}",
                                "account": account.label,
                            })
                    finally:
                        try:
                            if browser:
                                browser.close()
                            elif context:
                                context.close()
                        except Exception:
                            pass
                        try:
                            if proc and proc.poll() is None:
                                proc.terminate()
                                try:
                                    proc.wait(timeout=3)
                                except Exception:
                                    proc.kill()
                        except Exception:
                            pass
                        # Chrome/Edge 在 Windows 上可能把真正的浏览器交给子进程；
                        # 按本次实际使用的资料目录再清理一次，只影响本软件账号。
                        try:
                            if active_profile_dir:
                                active_path = Path(active_profile_dir)
                                runtime_root = APP_DIR / "runtime_profiles"
                                if _path_is_within(active_path, runtime_root):
                                    _cleanup_runtime_profile(active_path)
                                else:
                                    _stop_profile_processes(active_path)
                                    _remove_profile_locks(active_path)
                        except Exception:
                            pass
            if started_accounts == 0 and startup_errors:
                msg = "所有启用的浏览器账号都启动失败：\n" + "\n\n".join(startup_errors)
                self.status.emit(msg)
                self.fatal.emit(msg)
        except Exception as e:
            msg = f"采集线程异常：{e}"
            self.status.emit(msg)
            self.fatal.emit(msg)
        finally:
            self.finished_batch.emit()


class LoginWorker(QThread):
    status = pyqtSignal(str)
    failed = pyqtSignal(str)
    done = pyqtSignal()

    def __init__(self, account: BrowserAccount, parent=None):
        super().__init__(parent)
        self.account = account

    def run(self):
        try:
            self.status.emit(
                f"正在直接启动 {self.account.label} / {self.account.browser_name}…"
            )
            open_login_browser_direct(self.account)
            self.status.emit(
                f"{self.account.label} 登录浏览器已打开。请在浏览器里登录 Facebook；"
                "登录完成后关闭该浏览器窗口，再开始采集。"
            )
        except Exception as e:
            msg = str(e)
            self.status.emit(f"打开登录浏览器失败：{msg}")
            self.failed.emit(msg)
        finally:
            self.done.emit()


# ------------------------- UI -------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        icon = app_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.settings = load_settings()
        _purge_stale_runtime_profiles()
        self.store = Store(DB_PATH)
        self.detected = detect_installed_browsers()
        self.accounts: list[BrowserAccount] = []
        self.worker: Optional[BatchWorker] = None
        self.login_worker: Optional[LoginWorker] = None
        self.sheet_worker: Optional[GoogleSheetWorker] = None
        self.google_sync_pending = False
        self.batch_failed = False
        self.batch_error_message = ""
        self.monitoring = False
        self.round_running = False
        # 多账号模式的跨轮次轮换游标。每完成/启动一轮后向后移动 1 个账号。
        # 单个专页时也会在后续轮次切换账号。
        self.account_rotation_index = 0
        self.monitor_started = 0.0
        self.current_rows: dict[str, dict] = {}
        self.active_alerts: dict[str, dict] = {}
        self.session_muted: set[str] = set()
        self.last_ring_at: dict[str, float] = {}
        self.sound_effect = QSoundEffect(self)
        self.media_player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.media_player.setAudioOutput(self.audio_output)
        self.tray = QSystemTrayIcon(self)
        tray_icon = app_icon()
        if tray_icon.isNull():
            tray_icon = self.style().standardIcon(self.style().StandardPixmap.SP_MessageBoxInformation)
        self.tray.setIcon(tray_icon)
        self.tray.show()

        self.setWindowTitle(APP_NAME)
        self.resize(1520, 940)
        self.setMinimumSize(1120, 720)

        self._load_accounts()
        self._build_ui()
        self._apply_theme()
        self._sync_accounts_table()
        self._restore_settings_to_ui()

        self.monitor_timer = QTimer(self)
        self.monitor_timer.timeout.connect(self._monitor_tick)
        self.monitor_timer.start(1000)

        self.alert_timer = QTimer(self)
        self.alert_timer.timeout.connect(self._alert_tick)
        self.alert_timer.start(1000)

        self.id_sync_timer = QTimer(self)
        self.id_sync_timer.setSingleShot(True)
        self.id_sync_timer.timeout.connect(self._sync_results_to_ids)
        self.page_ids_edit.textChanged.connect(lambda: self.id_sync_timer.start(350))

    # ---------- Build UI ----------
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(12, 10, 12, 8)
        outer.setSpacing(8)

        top = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Facebook 专页粉丝增长监控")
        title.setObjectName("Title")
        sub = QLabel("网页登录采集 · 多浏览器账号轮换 · 历史增长 · 自定义增长提醒")
        sub.setObjectName("Muted")
        title_box.addWidget(title)
        title_box.addWidget(sub)
        top.addLayout(title_box)
        top.addStretch(1)
        self.color_swatch = QLabel("  ")
        self.color_swatch.setFixedSize(24,24)
        self.color_swatch.setObjectName("Swatch")
        top.addWidget(self.color_swatch)
        b = QPushButton("选择背景色")
        b.clicked.connect(self.choose_background)
        top.addWidget(b)
        b2 = QPushButton("恢复默认背景")
        b2.clicked.connect(self.reset_background)
        top.addWidget(b2)
        self.state_badge = QLabel("● 就绪")
        self.state_badge.setObjectName("Badge")
        top.addWidget(self.state_badge)
        outer.addLayout(top)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        outer.addWidget(splitter, 1)

        # 左：结果
        left = self.card()
        left_l = QVBoxLayout(left)
        hdr = QHBoxLayout()
        h = QLabel("当前结果")
        h.setObjectName("SectionTitle")
        hdr.addWidget(h)
        hdr.addStretch(1)
        exp = QPushButton("导出 CSV")
        exp.clicked.connect(self.export_csv)
        hdr.addWidget(exp)
        clr = QPushButton("清空当前结果")
        clr.clicked.connect(self.clear_results)
        hdr.addWidget(clr)
        left_l.addLayout(hdr)

        # Google 表格同步（只新增，不影响原有采集功能）
        gs_row = QHBoxLayout()
        gs_row.addWidget(QLabel("Google 表格"))
        self.google_sheet_url = QLineEdit()
        self.google_sheet_url.setPlaceholderText("粘贴 Google Sheets 链接")
        gs_row.addWidget(self.google_sheet_url, 3)
        self.google_sheet_tab = QLineEdit()
        self.google_sheet_tab.setPlaceholderText("工作表名称")
        self.google_sheet_tab.setMaximumWidth(150)
        gs_row.addWidget(self.google_sheet_tab)
        self.google_auto_sync = QCheckBox("每轮后自动同步")
        gs_row.addWidget(self.google_auto_sync)
        gs_btn = QPushButton("写入 Google 表格")
        gs_btn.clicked.connect(self.sync_google_sheet_manual)
        gs_row.addWidget(gs_btn)
        left_l.addLayout(gs_row)

        cred_row = QHBoxLayout()
        cred_row.addWidget(QLabel("Google 凭证"))
        self.google_credentials_edit = QLineEdit()
        self.google_credentials_edit.setReadOnly(True)
        self.google_credentials_edit.setPlaceholderText("选择 Google 服务账号 JSON；并把目标表格共享给 JSON 中的 client_email")
        cred_row.addWidget(self.google_credentials_edit, 1)
        cred_btn = QPushButton("选择 JSON")
        cred_btn.clicked.connect(self.choose_google_credentials)
        cred_row.addWidget(cred_btn)
        left_l.addLayout(cred_row)

        self.result_table = QTableWidget(0, 8)
        self.result_table.setHorizontalHeaderLabels([
            "专页 ID", "专页名称", "当前粉丝数", "较上次增长", "周期增长", "最后采集", "使用账号", "状态"
        ])
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.result_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.result_table.setAlternatingRowColors(True)
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.setWordWrap(False)
        hh = self.result_table.horizontalHeader()
        for i in range(8):
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        self.result_table.setColumnWidth(0, 150)
        self.result_table.setColumnWidth(2, 110)
        self.result_table.setColumnWidth(3, 100)
        self.result_table.setColumnWidth(4, 100)
        self.result_table.setColumnWidth(5, 180)
        self.result_table.setColumnWidth(6, 120)
        self.result_table.cellDoubleClicked.connect(self.copy_result_cell)
        left_l.addWidget(self.result_table, 1)
        splitter.addWidget(left)

        # 右：滚动设置
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        right_host = QWidget()
        right_l = QVBoxLayout(right_host)
        right_l.setContentsMargins(0,0,0,0)
        right_l.setSpacing(10)
        scroll.setWidget(right_host)
        splitter.addWidget(scroll)
        splitter.setSizes([960, 540])

        # 浏览器账号板块
        browser_card = self.card()
        bl = QVBoxLayout(browser_card)
        t = QLabel("浏览器 / Facebook 账号")
        t.setObjectName("SectionTitle")
        bl.addWidget(t)
        bl.addWidget(QLabel("自动识别本机 Chrome / Edge。登录时直接启动本机浏览器；采集时通过本机浏览器调试接口连接。每个账号使用独立资料目录。"))

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("账号使用模式："))
        self.single_radio = QRadioButton("单账号固定使用")
        self.multi_radio = QRadioButton("多个账号交替使用（跨轮次轮换）")
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.single_radio)
        self.mode_group.addButton(self.multi_radio)
        mode_row.addWidget(self.single_radio)
        mode_row.addWidget(self.multi_radio)
        mode_row.addStretch(1)
        bl.addLayout(mode_row)

        exec_row = QHBoxLayout()
        exec_row.addWidget(QLabel("浏览器运行方式："))
        self.visible_radio = QRadioButton("弹出显示")
        self.headless_radio = QRadioButton("静默执行")
        self.exec_group = QButtonGroup(self)
        self.exec_group.addButton(self.visible_radio)
        self.exec_group.addButton(self.headless_radio)
        exec_row.addWidget(self.visible_radio)
        exec_row.addWidget(self.headless_radio)
        exec_row.addStretch(1)
        bl.addLayout(exec_row)

        self.accounts_table = QTableWidget(0, 4)
        self.accounts_table.setHorizontalHeaderLabels(["使用", "账号名称", "浏览器", "程序路径"])
        self.accounts_table.verticalHeader().setVisible(False)
        self.accounts_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.accounts_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.accounts_table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.SelectedClicked)
        ah = self.accounts_table.horizontalHeader()
        ah.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        ah.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        ah.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        ah.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.accounts_table.setMinimumHeight(150)
        self.accounts_table.itemChanged.connect(self._account_item_changed)
        bl.addWidget(self.accounts_table)

        ar = QHBoxLayout()
        scan = QPushButton("重新识别浏览器")
        scan.clicked.connect(self.rescan_browsers)
        ar.addWidget(scan)
        add = QPushButton("添加账号配置")
        add.clicked.connect(self.add_account)
        ar.addWidget(add)
        rm = QPushButton("删除选中配置")
        rm.clicked.connect(self.remove_account)
        ar.addWidget(rm)
        bl.addLayout(ar)

        ar2 = QHBoxLayout()
        clear_login = QPushButton("删除登录资料")
        clear_login.setToolTip("删除选中账号保存的 Facebook/浏览器登录会话，但保留账号配置")
        clear_login.clicked.connect(self.delete_selected_login_profile)
        ar2.addWidget(clear_login)
        login = QPushButton("登录选中账号")
        login.setObjectName("Primary")
        login.clicked.connect(self.login_selected_account)
        ar2.addWidget(login)
        bl.addLayout(ar2)
        right_l.addWidget(browser_card)

        # 专页与采集设置
        collect_card = self.card()
        cl = QVBoxLayout(collect_card)
        t2 = QLabel("专页 ID 与采集设置")
        t2.setObjectName("SectionTitle")
        cl.addWidget(t2)
        self.page_ids_edit = QTextEdit()
        self.page_ids_edit.setPlaceholderText("每行输入一个 Facebook 专页 ID")
        self.page_ids_edit.setMinimumHeight(145)
        cl.addWidget(QLabel("专页 ID"))
        cl.addWidget(self.page_ids_edit)

        timing = QHBoxLayout()
        timing.addWidget(QLabel("采集 / 提醒间隔"))
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 9999)
        timing.addWidget(self.interval_spin)
        self.interval_unit = QComboBox()
        self.interval_unit.addItems(["秒", "分钟", "小时"])
        timing.addWidget(self.interval_unit)
        timing.addSpacing(20)
        timing.addWidget(QLabel("监控时长"))
        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(1, 9999)
        timing.addWidget(self.duration_spin)
        self.duration_mode = QComboBox()
        self.duration_mode.addItems(["无限", "分钟", "小时", "天"])
        timing.addWidget(self.duration_mode)
        timing.addStretch(1)
        cl.addLayout(timing)

        btnrow = QHBoxLayout()
        self.once_btn = QPushButton("立即采集一次")
        self.once_btn.clicked.connect(self.collect_once)
        btnrow.addWidget(self.once_btn)
        self.start_btn = QPushButton("开始自动监控")
        self.start_btn.setObjectName("Primary")
        self.start_btn.clicked.connect(self.start_monitoring)
        btnrow.addWidget(self.start_btn)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("Danger")
        self.stop_btn.clicked.connect(self.stop_monitoring)
        btnrow.addWidget(self.stop_btn)
        cl.addLayout(btnrow)
        right_l.addWidget(collect_card)

        # 增长提醒
        alert_card = self.card()
        al = QVBoxLayout(alert_card)
        at = QLabel("增长提醒")
        at.setObjectName("SectionTitle")
        al.addWidget(at)
        self.alert_check = QCheckBox("启用增长提醒")
        al.addWidget(self.alert_check)
        th = QHBoxLayout()
        th.addWidget(QLabel("每个采集周期，粉丝增长 ≥"))
        self.threshold_spin = QSpinBox()
        self.threshold_spin.setRange(1, 999_999_999)
        th.addWidget(self.threshold_spin, 1)
        th.addWidget(QLabel("时提醒"))
        al.addLayout(th)

        al.addWidget(QLabel("提醒铃声"))
        sr = QHBoxLayout()
        self.sound_combo = QComboBox()
        self.sound_combo.addItems(["内置铃声", "自定义音频"])
        sr.addWidget(self.sound_combo)
        choose_sound = QPushButton("选择音频")
        choose_sound.clicked.connect(self.choose_sound)
        sr.addWidget(choose_sound)
        preview = QPushButton("试听")
        preview.clicked.connect(self.play_sound)
        sr.addWidget(preview)
        al.addLayout(sr)
        self.sound_path_label = QLabel("")
        self.sound_path_label.setObjectName("Muted")
        al.addWidget(self.sound_path_label)

        self.last_alert_label = QLabel("最近提醒：暂无")
        self.last_alert_label.setTextFormat(Qt.TextFormat.PlainText)
        self.last_alert_label.setObjectName("AlertBox")
        self.last_alert_label.setWordWrap(True)
        al.addWidget(self.last_alert_label)

        al.addWidget(QLabel("待确认提醒（未手动停止的专页会按采集间隔重复响铃）"))
        self.alert_table = QTableWidget(0, 3)
        self.alert_table.setHorizontalHeaderLabels(["专页", "增长", "操作"])
        self.alert_table.verticalHeader().setVisible(False)
        self.alert_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        ah2 = self.alert_table.horizontalHeader()
        ah2.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        ah2.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        ah2.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.alert_table.setMinimumHeight(150)
        al.addWidget(self.alert_table)
        right_l.addWidget(alert_card)
        right_l.addStretch(1)

        self.status_label = QLabel(f"就绪。数据库：{DB_PATH}")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setObjectName("Muted")
        outer.addWidget(self.status_label)
        v = QLabel(APP_VERSION)
        v.setAlignment(Qt.AlignmentFlag.AlignRight)
        v.setObjectName("Muted")
        outer.addWidget(v)

    def card(self):
        w = QFrame()
        w.setObjectName("Card")
        return w

    # ---------- Settings ----------
    def _load_accounts(self):
        saved = self.settings.get("accounts") or []
        allowed = {_canonical_path(b.exe): b for b in self.detected}
        for x in saved:
            try:
                if not isinstance(x, dict):
                    continue
                account_id = validate_account_id(x.get("id", ""))
                matched = allowed.get(_canonical_path(x.get("exe", "")))
                if matched is None:
                    # Settings are user-writable; never execute an arbitrary path from them.
                    continue
                label = str(x.get("label", "账号")).strip()[:80] or "账号"
                a = BrowserAccount(
                    id=account_id, label=label, browser_name=matched.name, exe=matched.exe,
                    enabled=bool(x.get("enabled", True))
                )
                self.accounts.append(a)
            except Exception:
                pass
        if not self.accounts:
            for i, b in enumerate(self.detected[:2], 1):
                self.accounts.append(BrowserAccount(
                    id=f"acct_{int(time.time())}_{i}", label=f"账号{i}",
                    browser_name=b.name, exe=b.exe, enabled=True
                ))

    def _settings_from_ui(self):
        self.settings["background"] = self.settings.get("background", "#F4F7FB")
        self.settings["interval_value"] = self.interval_spin.value()
        self.settings["interval_unit"] = self.interval_unit.currentText()
        self.settings["duration_value"] = self.duration_spin.value()
        self.settings["duration_mode"] = self.duration_mode.currentText()
        self.settings["show_browser"] = self.visible_radio.isChecked()
        self.settings["account_mode"] = "multi" if self.multi_radio.isChecked() else "single"
        self.settings["alert_enabled"] = self.alert_check.isChecked()
        self.settings["alert_threshold"] = self.threshold_spin.value()
        self.settings["sound_mode"] = "custom" if self.sound_combo.currentText() == "自定义音频" else "builtin"
        self.settings["page_ids"] = self.page_ids_edit.toPlainText()
        self.settings["accounts"] = [asdict(a) for a in self.accounts]
        self.settings["google_sheet_url"] = self.google_sheet_url.text().strip()
        self.settings["google_sheet_tab"] = self.google_sheet_tab.text().strip() or "当前结果"
        self.settings["google_credentials_path"] = self.google_credentials_edit.text().strip()
        self.settings["google_auto_sync"] = self.google_auto_sync.isChecked()
        save_settings(self.settings)

    def _restore_settings_to_ui(self):
        self.interval_spin.setValue(int(self.settings.get("interval_value", 1)))
        self.interval_unit.setCurrentText(self.settings.get("interval_unit", "分钟"))
        self.duration_spin.setValue(int(self.settings.get("duration_value", 24)))
        self.duration_mode.setCurrentText(self.settings.get("duration_mode", "无限"))
        self.visible_radio.setChecked(bool(self.settings.get("show_browser", True)))
        self.headless_radio.setChecked(not bool(self.settings.get("show_browser", True)))
        self.multi_radio.setChecked(self.settings.get("account_mode") == "multi")
        self.single_radio.setChecked(self.settings.get("account_mode") != "multi")
        self.alert_check.setChecked(bool(self.settings.get("alert_enabled", True)))
        self.threshold_spin.setValue(int(self.settings.get("alert_threshold", 500)))
        self.sound_combo.setCurrentText("自定义音频" if self.settings.get("sound_mode") == "custom" else "内置铃声")
        self.page_ids_edit.setPlainText(self.settings.get("page_ids", ""))
        self.google_sheet_url.setText(self.settings.get("google_sheet_url", ""))
        self.google_sheet_tab.setText(self.settings.get("google_sheet_tab", "当前结果"))
        self.google_credentials_edit.setText(self.settings.get("google_credentials_path", ""))
        self.google_auto_sync.setChecked(bool(self.settings.get("google_auto_sync", False)))
        self._update_sound_label()

    def closeEvent(self, event):
        self._settings_from_ui()
        if self.worker and self.worker.isRunning():
            self.worker.request_stop()
        event.accept()

    # ---------- Theme ----------
    def _apply_theme(self):
        bg = self.settings.get("background", "#F4F7FB")
        self.color_swatch.setStyleSheet(f"background:{bg}; border:1px solid #AAB8CC; border-radius:6px;")
        QApplication.instance().setStyle("Fusion")
        pal = QPalette()
        pal.setColor(QPalette.ColorRole.Window, QColor(bg))
        pal.setColor(QPalette.ColorRole.WindowText, QColor("#10233F"))
        pal.setColor(QPalette.ColorRole.Base, QColor("#FFFFFF"))
        pal.setColor(QPalette.ColorRole.AlternateBase, QColor("#F8FAFD"))
        pal.setColor(QPalette.ColorRole.Text, QColor("#10233F"))
        pal.setColor(QPalette.ColorRole.Button, QColor("#FFFFFF"))
        pal.setColor(QPalette.ColorRole.ButtonText, QColor("#10233F"))
        pal.setColor(QPalette.ColorRole.Highlight, QColor("#DCEBFF"))
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#10233F"))
        QApplication.instance().setPalette(pal)
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background: {bg}; color:#10233F; font-family:'Microsoft YaHei UI','Segoe UI'; font-size:13px; }}
            QFrame#Card {{ background:#FFFFFF; border:1px solid #DCE5F0; border-radius:14px; }}
            QLabel#Title {{ font-size:24px; font-weight:700; }}
            QLabel#SectionTitle {{ font-size:17px; font-weight:700; }}
            QLabel#Muted {{ color:#6B7B91; }}
            QLabel#Badge {{ background:#E8F2FF; color:#145CAC; border:1px solid #BCD8FF; border-radius:12px; padding:5px 10px; font-weight:600; }}
            QLabel#AlertBox {{ background:#FFF8E7; color:#8A5A00; border:1px solid #F6D68A; border-radius:8px; padding:9px; }}
            QPushButton {{ background:#FFFFFF; color:#10233F; border:1px solid #CDD9E7; border-radius:9px; padding:8px 12px; min-height:20px; font-weight:600; }}
            QPushButton:hover {{ background:#F1F6FD; }}
            QPushButton#Primary {{ background:#2F80ED; color:white; border-color:#2F80ED; }}
            QPushButton#Danger {{ background:#EB5757; color:white; border-color:#EB5757; }}
            QPushButton:disabled {{ background:#DCE4EE; color:#8A97A8; border-color:#DCE4EE; }}
            QLineEdit, QTextEdit, QSpinBox, QComboBox {{ background:#FFFFFF; color:#10233F; border:1px solid #C9D7E7; border-radius:8px; padding:6px; selection-background-color:#D8E9FF; selection-color:#10233F; }}
            QComboBox QAbstractItemView {{ background:#FFFFFF; color:#10233F; selection-background-color:#D8E9FF; selection-color:#10233F; }}
            QTableWidget {{ background:#FFFFFF; color:#10233F; alternate-background-color:#F8FAFD; border:1px solid #DCE5F0; border-radius:9px; gridline-color:#E8EEF6; selection-background-color:#DCEBFF; selection-color:#10233F; }}
            QHeaderView::section {{ background:#F5F8FC; color:#26435F; border:none; border-bottom:1px solid #DCE5F0; padding:9px; font-weight:700; }}
            QCheckBox, QRadioButton {{ color:#10233F; spacing:8px; }}
            QScrollArea {{ border:none; background:transparent; }}
        """)

    def choose_background(self):
        c = QColorDialog.getColor(QColor(self.settings.get("background", "#F4F7FB")), self, "选择软件背景色")
        if c.isValid():
            self.settings["background"] = c.name()
            self._apply_theme()
            self._settings_from_ui()

    def reset_background(self):
        self.settings["background"] = "#F4F7FB"
        self._apply_theme()
        self._settings_from_ui()

    # ---------- Accounts ----------
    def _sync_accounts_table(self):
        self.accounts_table.blockSignals(True)
        self.accounts_table.setRowCount(0)
        for a in self.accounts:
            r = self.accounts_table.rowCount()
            self.accounts_table.insertRow(r)
            use = QTableWidgetItem("")
            use.setFlags(use.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            use.setCheckState(Qt.CheckState.Checked if a.enabled else Qt.CheckState.Unchecked)
            use.setData(Qt.ItemDataRole.UserRole, a.id)
            self.accounts_table.setItem(r, 0, use)
            self.accounts_table.setItem(r, 1, QTableWidgetItem(a.label))
            browser_item = QTableWidgetItem(a.browser_name)
            browser_item.setFlags(browser_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.accounts_table.setItem(r, 2, browser_item)
            path_item = QTableWidgetItem(a.exe)
            path_item.setFlags(path_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.accounts_table.setItem(r, 3, path_item)
        self.accounts_table.blockSignals(False)

    def _account_item_changed(self, item: QTableWidgetItem):
        r = item.row()
        if r < 0 or r >= len(self.accounts):
            return
        a = self.accounts[r]
        if item.column() == 0:
            a.enabled = item.checkState() == Qt.CheckState.Checked
        elif item.column() == 1:
            a.label = item.text().strip() or a.label
        self._settings_from_ui()

    def rescan_browsers(self):
        self.detected = detect_installed_browsers()
        if not self.detected:
            QMessageBox.warning(self, "未识别到浏览器", "没有在常见安装目录识别到 Chrome 或 Edge。")
            return
        names = "\n".join(f"• {b.name}: {b.exe}" for b in self.detected)
        QMessageBox.information(self, "识别完成", f"识别到：\n\n{names}\n\n可以点击“添加账号配置”选择其中任意浏览器。")

    def add_account(self):
        if not self.detected:
            self.detected = detect_installed_browsers()
        if not self.detected:
            QMessageBox.warning(self, "无浏览器", "未识别到 Chrome / Edge。")
            return
        opts = [f"{b.name}  |  {b.exe}" for b in self.detected]
        choice, ok = QInputDialog.getItem(self, "添加账号配置", "选择浏览器：", opts, 0, False)
        if not ok:
            return
        idx = opts.index(choice)
        b = self.detected[idx]
        n = len(self.accounts) + 1
        a = BrowserAccount(id=f"acct_{int(time.time()*1000)}", label=f"账号{n}", browser_name=b.name, exe=b.exe, enabled=True)
        self.accounts.append(a)
        self._sync_accounts_table()
        self._settings_from_ui()

    def remove_account(self):
        r = self.accounts_table.currentRow()
        if r < 0 or r >= len(self.accounts):
            return
        a = self.accounts[r]
        if QMessageBox.question(self, "删除账号配置", f"删除 {a.label}？\n不会自动删除其浏览器资料目录。") != QMessageBox.StandardButton.Yes:
            return
        self.accounts.pop(r)
        self._sync_accounts_table()
        self._settings_from_ui()

    def delete_selected_login_profile(self):
        a = self.selected_account()
        if not a:
            QMessageBox.warning(self, "提示", "请先选中一个浏览器账号配置。")
            return
        msg = (
            f"确定删除 {a.label} 保存的登录资料吗？\n\n"
            "这会清除该账号在本软件中的 Facebook 登录状态、Cookie 和浏览器会话。\n"
            "账号配置本身会保留，之后可以点击“登录选中账号”重新登录。"
        )
        if QMessageBox.question(self, "删除登录资料", msg) != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = delete_account_login_profile(a)
            if removed:
                QMessageBox.information(self, "已删除", f"{a.label} 的登录资料已安全删除。")
                self.set_status(f"已删除 {a.label} 的登录资料；账号配置已保留。")
            else:
                QMessageBox.information(self, "无需删除", f"{a.label} 当前没有已保存的登录资料。")
                self.set_status(f"{a.label} 没有已保存的登录资料。")
        except Exception as e:
            QMessageBox.critical(self, "删除失败", str(e))
            self.set_status(f"删除 {a.label} 登录资料失败：{e}")

    def selected_account(self) -> Optional[BrowserAccount]:
        r = self.accounts_table.currentRow()
        if r < 0 and self.accounts:
            r = 0
        return self.accounts[r] if 0 <= r < len(self.accounts) else None

    def login_selected_account(self):
        if self.login_worker and self.login_worker.isRunning():
            QMessageBox.information(self, "提示", "已有登录浏览器正在运行。")
            return
        a = self.selected_account()
        if not a:
            QMessageBox.warning(self, "提示", "请先添加并选中一个浏览器账号配置。")
            return
        self.login_worker = LoginWorker(a, self)
        self.login_worker.status.connect(self.set_status)
        self.login_worker.failed.connect(self._show_login_error)
        self.login_worker.done.connect(self._login_worker_done)
        self.login_worker.start()

    def _show_login_error(self, message: str):
        QMessageBox.critical(
            self,
            "浏览器启动失败",
            message + "\n\n请先确认表格中的浏览器程序路径真实存在；也可以点击‘重新识别浏览器’后再试。"
        )

    def _login_worker_done(self):
        # 线程结束不代表浏览器被关闭；登录浏览器由本机 Chrome/Edge 独立运行。
        pass

    def active_accounts(self) -> list[BrowserAccount]:
        enabled = [a for a in self.accounts if a.enabled and Path(a.exe).exists()]
        if self.single_radio.isChecked():
            return enabled[:1]
        return enabled

    # ---------- Collection ----------
    def validate_collect(self) -> tuple[list[str], list[BrowserAccount]]:
        ids = parse_page_ids(self.page_ids_edit.toPlainText())
        if not ids:
            raise ValueError("请至少输入一个有效的数字专页 ID。")
        accounts = self.active_accounts()
        if not accounts:
            raise ValueError("请至少启用一个浏览器账号配置。")
        return ids, accounts

    def collect_once(self):
        if self.round_running:
            return
        try:
            ids, accounts = self.validate_collect()
        except Exception as e:
            QMessageBox.warning(self, "设置不完整", str(e))
            return
        self._settings_from_ui()
        self._start_batch(ids, accounts)

    def _start_batch(self, ids: list[str], accounts: list[BrowserAccount]):
        if self.round_running:
            self.set_status("已有一轮采集正在运行，请等待当前采集完成。")
            return
        self.batch_failed = False
        self.batch_error_message = ""
        self.round_running = True
        self.once_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.state_badge.setText("● 正在启动")
        account_text = "、".join(a.label for a in accounts)
        rotation_start = 0
        if self.multi_radio.isChecked() and len(accounts) > 1:
            rotation_start = self.account_rotation_index % len(accounts)
            first_account = accounts[rotation_start].label
            self.set_status(
                f"已收到采集任务：{len(ids)} 个专页；启用账号：{account_text}；"
                f"本轮从 {first_account} 开始轮换。正在检查浏览器组件…"
            )
            # 下一轮从下一个账号开始。即使只有 1 个专页，也会跨轮次切换。
            self.account_rotation_index = (rotation_start + 1) % len(accounts)
        else:
            self.set_status(f"已收到采集任务：{len(ids)} 个专页；账号：{account_text}。正在检查浏览器组件…")

        self.worker = BatchWorker(
            ids, accounts, self.visible_radio.isChecked(), self.store,
            rotation_start=rotation_start, parent=self
        )
        self.worker.row.connect(self.on_row)
        self.worker.status.connect(self.set_status)
        self.worker.fatal.connect(self.on_batch_fatal)
        self.worker.finished_batch.connect(self.on_batch_finished)
        self.worker.start()

    def start_monitoring(self):
        if self.monitoring:
            return
        try:
            ids, accounts = self.validate_collect()
        except Exception as e:
            QMessageBox.warning(self, "设置不完整", str(e))
            return
        self._settings_from_ui()
        # 新一轮自动监控从第一个已启用账号开始，之后每轮自动切到下一个账号。
        self.account_rotation_index = 0
        # 新一轮：恢复所有专页提醒资格
        self.session_muted.clear()
        self.active_alerts.clear()
        self.last_ring_at.clear()
        self.refresh_alert_table()
        self.monitoring = True
        self.monitor_started = time.monotonic()
        self.stop_btn.setEnabled(True)
        self.state_badge.setText("● 监控中")
        self._start_batch(ids, accounts)

    def stop_monitoring(self):
        self.monitoring = False
        if self.worker and self.worker.isRunning():
            self.worker.request_stop()
        self.state_badge.setText("● 已停止")
        self.set_status("自动监控已停止。")

    def _monitor_tick(self):
        if not self.monitoring or self.round_running:
            return
        # 时长控制
        mode = self.duration_mode.currentText()
        if mode != "无限":
            sec = self.duration_spin.value() * {"分钟":60,"小时":3600,"天":86400}.get(mode, 3600)
            if time.monotonic() - self.monitor_started >= sec:
                self.stop_monitoring()
                self.set_status("已达到设定监控时长，自动停止。")
                return

        last = getattr(self, "last_batch_end", 0.0)
        gap = interval_seconds(self.interval_spin.value(), self.interval_unit.currentText())
        if time.monotonic() - last >= gap:
            try:
                ids, accounts = self.validate_collect()
            except Exception as e:
                self.set_status(str(e))
                return
            self._start_batch(ids, accounts)

    def on_batch_fatal(self, message: str):
        self.batch_failed = True
        self.batch_error_message = message
        self.state_badge.setText("● 采集失败")
        self.set_status(message)
        # Do not leave monitoring silently running after a fatal runtime/dependency failure.
        if self.monitoring:
            self.monitoring = False
        QMessageBox.critical(self, "采集无法启动", message)

    def on_batch_finished(self):
        self.round_running = False
        self.last_batch_end = time.monotonic()
        self.once_btn.setEnabled(True)
        self.start_btn.setEnabled(not self.monitoring)
        if self.batch_failed:
            self.state_badge.setText("● 采集失败")
            if self.batch_error_message:
                self.set_status(self.batch_error_message)
            return
        self.state_badge.setText("● 监控中" if self.monitoring else "● 就绪")
        if self.monitoring:
            sec = interval_seconds(self.interval_spin.value(), self.interval_unit.currentText())
            self.set_status(f"本轮完成，约 {sec} 秒后开始下一轮。")
        else:
            self.set_status("本轮采集完成。")

        # 可选：每轮采集完成后把当前结果覆盖同步到 Google 表格。
        if self.google_auto_sync.isChecked() and self.result_table.rowCount() > 0:
            self.sync_google_sheet(silent=True)

    def on_row(self, row: dict):
        pid = row["page_id"]
        old = self.current_rows.get(pid, {})
        name = row.get("page_name") or old.get("page_name") or ""
        row["page_name"] = name
        self.current_rows[pid] = row
        self.upsert_result(row)

        delta = row.get("delta")
        if row.get("followers") is not None and delta is not None:
            # 当前轮已手动关闭提醒的专页不再触发新提醒
            if self.alert_check.isChecked() and pid not in self.session_muted and delta >= self.threshold_spin.value():
                if pid not in self.active_alerts:
                    self.active_alerts[pid] = {
                        "page_id": pid,
                        "name": name or pid,
                        "delta": int(delta),
                        "followers": int(row["followers"]),
                    }
                    self.trigger_alert(pid, immediate=True)
                    self.refresh_alert_table()

    def upsert_result(self, row: dict):
        pid = row["page_id"]
        target = -1
        for r in range(self.result_table.rowCount()):
            if self.result_table.item(r,0) and self.result_table.item(r,0).text() == pid:
                target = r; break
        if target < 0:
            target = self.result_table.rowCount(); self.result_table.insertRow(target)
        delta = row.get("delta")
        vals = [
            pid,
            row.get("page_name", ""),
            f"{row['followers']:,}" if row.get("followers") is not None else "",
            self.fmt_delta(delta),
            self.fmt_delta(delta),
            row.get("collected_at", ""),
            row.get("account", ""),
            row.get("status", "") + ("；本轮提醒已停止" if pid in self.session_muted and row.get("followers") is not None else ""),
        ]
        for c, v in enumerate(vals):
            it = QTableWidgetItem(str(v))
            if c in [0,1,2,3,4,5,6]:
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.result_table.setItem(target, c, it)

    @staticmethod
    def fmt_delta(v):
        if v is None:
            return "—"
        v = int(v)
        return f"+{v}" if v > 0 else str(v)

    def _sync_results_to_ids(self):
        ids = set(parse_page_ids(self.page_ids_edit.toPlainText()))
        # 删除 UI 当前结果中已从上方 ID 列表移除的行
        for r in range(self.result_table.rowCount()-1, -1, -1):
            it = self.result_table.item(r,0)
            pid = it.text() if it else ""
            if pid and pid not in ids:
                self.result_table.removeRow(r)
                self.current_rows.pop(pid, None)
                self.active_alerts.pop(pid, None)
                self.session_muted.discard(pid)
                self.last_ring_at.pop(pid, None)
        self.refresh_alert_table()

    # ---------- Alerts ----------
    def _alert_tick(self):
        if not self.alert_check.isChecked():
            return
        gap = interval_seconds(self.interval_spin.value(), self.interval_unit.currentText())
        now = time.monotonic()
        for pid in list(self.active_alerts):
            if pid in self.session_muted:
                self.active_alerts.pop(pid, None)
                continue
            last = self.last_ring_at.get(pid, 0.0)
            if now - last >= gap:
                self.trigger_alert(pid, immediate=False)
        if self.active_alerts:
            self.refresh_alert_table()

    def trigger_alert(self, pid: str, immediate=True):
        a = self.active_alerts.get(pid)
        if not a or pid in self.session_muted:
            return
        self.play_sound()
        self.last_ring_at[pid] = time.monotonic()
        text = f"{a['name']} · 周期增长 {self.fmt_delta(a['delta'])}，当前粉丝 {a['followers']:,}"
        self.last_alert_label.setText("最近提醒：" + text)
        try:
            self.tray.showMessage("Facebook 粉丝增长提醒", text, QSystemTrayIcon.MessageIcon.Information, 6000)
        except Exception:
            pass

    def stop_page_alert(self, pid: str):
        self.session_muted.add(pid)
        self.active_alerts.pop(pid, None)
        self.last_ring_at.pop(pid, None)
        row = self.current_rows.get(pid)
        if row:
            self.upsert_result(row)
        self.last_alert_label.setText(f"最近提醒：已停止 {row.get('page_name') if row else pid} 本轮铃声；重新开始自动监控后恢复。")
        self.refresh_alert_table()

    def refresh_alert_table(self):
        self.alert_table.setRowCount(0)
        for pid, a in self.active_alerts.items():
            if pid in self.session_muted:
                continue
            r = self.alert_table.rowCount(); self.alert_table.insertRow(r)
            name = QTableWidgetItem(a.get("name") or pid)
            name.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.alert_table.setItem(r,0,name)
            d = QTableWidgetItem(self.fmt_delta(a.get("delta")))
            d.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.alert_table.setItem(r,1,d)
            btn = QPushButton("停")
            btn.setFixedSize(38, 24)
            btn.setToolTip("本轮停止这个专页的提醒；重新开始自动监控后恢复")
            btn.clicked.connect(lambda _=False, x=pid: self.stop_page_alert(x))
            wrap = QWidget(); lay = QHBoxLayout(wrap); lay.setContentsMargins(2,2,2,2); lay.addStretch(); lay.addWidget(btn); lay.addStretch()
            self.alert_table.setCellWidget(r,2,wrap)

    def choose_sound(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择提醒音频", "", "音频文件 (*.wav *.mp3 *.m4a *.ogg *.flac);;所有文件 (*.*)")
        if path:
            self.settings["sound_path"] = path
            self.sound_combo.setCurrentText("自定义音频")
            self._update_sound_label()
            self._settings_from_ui()

    def _update_sound_label(self):
        p = self.settings.get("sound_path", "")
        self.sound_path_label.setText(f"自定义：{p}" if p else f"内置：{BUILTIN_BELL.name}")

    def play_sound(self):
        mode = "custom" if self.sound_combo.currentText() == "自定义音频" else "builtin"
        p = Path(self.settings.get("sound_path", "")) if mode == "custom" else BUILTIN_BELL
        if not p.exists():
            p = BUILTIN_BELL
        try:
            if p.suffix.lower() == ".wav":
                self.sound_effect.setSource(QUrl.fromLocalFile(str(p)))
                self.sound_effect.setVolume(0.95)
                self.sound_effect.play()
            else:
                self.media_player.setSource(QUrl.fromLocalFile(str(p)))
                self.audio_output.setVolume(0.95)
                self.media_player.play()
        except Exception:
            pass

    # ---------- Google Sheets ----------
    def choose_google_credentials(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 Google 服务账号 JSON", "", "JSON 文件 (*.json);;所有文件 (*.*)"
        )
        if path:
            self.google_credentials_edit.setText(path)
            self._settings_from_ui()
            try:
                data = load_validated_service_account_info(path)
                email = data.get("client_email", "")
                QMessageBox.information(
                    self,
                    "下一步",
                    "请把目标 Google 表格共享给下面这个服务账号，并给予“编辑者”权限：\n\n"
                    + email
                )
            except Exception as e:
                self.google_credentials_edit.clear()
                self._settings_from_ui()
                QMessageBox.warning(self, "Google 凭证无效", str(e))

    def _google_values_from_table(self) -> list[list]:
        headers = [
            "专页 ID", "专页名称", "当前粉丝数", "较上次增长",
            "周期增长", "最后采集", "使用账号", "状态"
        ]
        values: list[list] = [headers]
        for r in range(self.result_table.rowCount()):
            row = []
            for c in range(self.result_table.columnCount()):
                item = self.result_table.item(r, c)
                text = item.text() if item else ""
                # 粉丝数/增长列尽量写成数字，便于 Google Sheets 后续排序和计算。
                if c in (2, 3, 4) and text not in ("", "—"):
                    raw = text.replace(",", "").replace("+", "").strip()
                    if re.fullmatch(r"-?\d+", raw):
                        row.append(int(raw))
                        continue
                row.append(text)
            values.append(row)
        return values

    def sync_google_sheet_manual(self):
        self.sync_google_sheet(silent=False)

    def sync_google_sheet(self, silent: bool = False):
        if self.sheet_worker and self.sheet_worker.isRunning():
            # 自动同步遇到上一轮仍在写时，记住一次，完成后同步最新结果。
            if silent:
                self.google_sync_pending = True
                return
            QMessageBox.information(self, "Google 表格", "已有 Google 表格同步任务正在执行。")
            return

        sheet_url = self.google_sheet_url.text().strip()
        tab_name = self.google_sheet_tab.text().strip() or "当前结果"
        cred_path = self.google_credentials_edit.text().strip()
        try:
            parse_google_sheet_id(sheet_url)
            if not cred_path:
                raise ValueError("请先选择 Google 服务账号 JSON 文件。")
            load_validated_service_account_info(cred_path)
            if self.result_table.rowCount() <= 0:
                raise ValueError("当前结果为空，没有可写入的数据。")
        except Exception as e:
            if not silent:
                QMessageBox.warning(self, "Google 表格设置不完整", str(e))
            else:
                self.set_status(f"Google 表格自动同步跳过：{e}")
            return

        self._settings_from_ui()
        values = self._google_values_from_table()
        self.sheet_worker = GoogleSheetWorker(sheet_url, tab_name, cred_path, values, self)
        self.sheet_worker.status.connect(self.set_status)
        self.sheet_worker.success.connect(lambda msg: self._google_sync_done(True, msg, silent))
        self.sheet_worker.failed.connect(lambda msg: self._google_sync_done(False, msg, silent))
        self.sheet_worker.finished.connect(self._google_worker_finished)
        self.sheet_worker.start()

    def _google_sync_done(self, ok: bool, message: str, silent: bool):
        self.set_status(message if ok else f"Google 表格同步失败：{message}")
        if not silent:
            if ok:
                QMessageBox.information(self, "Google 表格", message)
            else:
                QMessageBox.critical(
                    self,
                    "Google 表格同步失败",
                    message + "\n\n请确认：\n1. 已把表格共享给服务账号 client_email；\n2. 服务账号有编辑权限；\n3. Google Sheets API 已启用。"
                )

    def _google_worker_finished(self):
        if self.google_sync_pending:
            self.google_sync_pending = False
            QTimer.singleShot(200, lambda: self.sync_google_sheet(silent=True))

    # ---------- Utilities ----------
    def copy_result_cell(self, row: int, col: int):
        if col not in (0,1):
            return
        it = self.result_table.item(row,col)
        if it:
            QGuiApplication.clipboard().setText(it.text())
            self.set_status(f"已复制：{it.text()}")

    def export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出历史 CSV", "facebook_followers.csv", "CSV 文件 (*.csv)")
        if path:
            try:
                self.store.export_csv(path)
                QMessageBox.information(self, "完成", f"已导出：\n{path}")
            except Exception as e:
                QMessageBox.critical(self, "导出失败", str(e))

    def clear_results(self):
        if QMessageBox.question(self, "清空当前结果", "只清空界面当前结果，不删除数据库历史。继续吗？") == QMessageBox.StandardButton.Yes:
            self.result_table.setRowCount(0)
            self.current_rows.clear()

    def set_status(self, text: str):
        self.status_label.setText(text)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    icon = app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
