import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "facebook_follower_tracker_v2_0.py"

class Dummy:
    def __init__(self, *args, **kwargs):
        pass
    def __getattr__(self, name):
        return Dummy()
    def __call__(self, *args, **kwargs):
        return Dummy()
    def __or__(self, other):
        return self

class DummySignal:
    def connect(self, *args, **kwargs):
        pass
    def emit(self, *args, **kwargs):
        pass


def _signal(*args, **kwargs):
    return DummySignal()


def load_module(tmpdir: str):
    os.environ["LOCALAPPDATA"] = tmpdir
    qtcore = types.ModuleType("PyQt6.QtCore")
    qtgui = types.ModuleType("PyQt6.QtGui")
    qtmulti = types.ModuleType("PyQt6.QtMultimedia")
    qtwidgets = types.ModuleType("PyQt6.QtWidgets")
    pyqt6 = types.ModuleType("PyQt6")

    qtcore.Qt = Dummy()
    qtcore.QThread = Dummy
    qtcore.pyqtSignal = _signal
    for name in ["QTimer", "QUrl", "QSize"]:
        setattr(qtcore, name, Dummy)
    for name in ["QColor", "QGuiApplication", "QIcon", "QPalette"]:
        setattr(qtgui, name, Dummy)
    for name in ["QAudioOutput", "QMediaPlayer", "QSoundEffect"]:
        setattr(qtmulti, name, Dummy)
    widget_names = [
        "QApplication", "QCheckBox", "QColorDialog", "QComboBox", "QDialog", "QFileDialog",
        "QFormLayout", "QFrame", "QHBoxLayout", "QHeaderView", "QLabel", "QLineEdit",
        "QMainWindow", "QMessageBox", "QPushButton", "QRadioButton", "QScrollArea",
        "QSizePolicy", "QSpinBox", "QSplitter", "QTableWidget", "QTableWidgetItem",
        "QTabWidget", "QTextEdit", "QVBoxLayout", "QWidget", "QAbstractItemView",
        "QButtonGroup", "QInputDialog", "QSystemTrayIcon"
    ]
    for name in widget_names:
        setattr(qtwidgets, name, Dummy)

    sys.modules.update({
        "PyQt6": pyqt6,
        "PyQt6.QtCore": qtcore,
        "PyQt6.QtGui": qtgui,
        "PyQt6.QtMultimedia": qtmulti,
        "PyQt6.QtWidgets": qtwidgets,
    })
    spec = importlib.util.spec_from_file_location("fbtracker_security_test", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class SecurityHelpersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.m = load_module(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_account_id_rejects_path_traversal(self):
        for bad in ["../victim", "acct_../../victim", "acct_x/y", "C:\\temp", ""]:
            with self.assertRaises(ValueError):
                self.m.validate_account_id(bad)
        self.assertEqual(self.m.validate_account_id("acct_123_ABC-x"), "acct_123_ABC-x")

    def test_csv_formula_injection_is_escaped(self):
        for raw in ["=CMD()", "+1+1", "-2+3", "@SUM(A1:A2)", "  =1+1", "\t=1+1"]:
            self.assertTrue(self.m._csv_safe_cell(raw).startswith("'"), raw)
        self.assertEqual(self.m._csv_safe_cell("Normal Page"), "Normal Page")
        self.assertEqual(self.m._csv_safe_cell(123), 123)

    def test_cdp_origin_not_wildcard(self):
        args = self.m._browser_args_for_profile(
            Path("chrome.exe"), Path(self.tmp.name) / "profile", headless=True, debug_port=9222
        )
        self.assertNotIn("--remote-allow-origins=*", args)
        self.assertIn("--remote-allow-origins=http://127.0.0.1", args)
        self.assertIn("--remote-debugging-address=127.0.0.1", args)

    def test_runtime_profile_is_unique_and_cleanable(self):
        self.m.APP_DIR = Path(self.tmp.name) / "app"
        self.m.ACCOUNT_ROOT = self.m.APP_DIR / "browser_accounts"
        self.m.ACCOUNT_ROOT.mkdir(parents=True, exist_ok=True)
        account = self.m.BrowserAccount("acct_test", "A", "Chrome", "chrome.exe")
        account.profile_dir.mkdir(parents=True, exist_ok=True)
        (account.profile_dir / "Cookies.txt").write_text("session", encoding="utf-8")
        p1 = self.m._make_runtime_profile(account)
        p2 = self.m._make_runtime_profile(account)
        self.assertNotEqual(p1, p2)
        self.assertTrue((p1 / "Cookies.txt").exists())
        self.m._cleanup_runtime_profile(p1)
        self.m._cleanup_runtime_profile(p2)
        self.assertFalse(p1.exists())
        self.assertFalse(p2.exists())

    def test_browser_path_must_be_discovered(self):
        allowed = Path(self.tmp.name) / "chrome.exe"
        allowed.write_bytes(b"")
        self.m.detect_installed_browsers = lambda: [self.m.BrowserInstall("Google Chrome", str(allowed))]
        self.assertEqual(self.m.validate_browser_executable(str(allowed)), allowed)
        with self.assertRaises(RuntimeError):
            self.m.validate_browser_executable(str(Path(self.tmp.name) / "evil.exe"))


    def test_browser_detection_does_not_trust_path(self):
        # PATH is user-controlled. Browser discovery must not use shutil.which().
        original_env = dict(os.environ)
        original_exists = Path.exists
        try:
            os.environ["LOCALAPPDATA"] = str(Path(self.tmp.name) / "local")
            os.environ["PROGRAMFILES"] = str(Path(self.tmp.name) / "pf")
            os.environ["PROGRAMFILES(X86)"] = str(Path(self.tmp.name) / "pfx86")
            self.assertEqual(self.m.detect_installed_browsers(), [])
        finally:
            os.environ.clear()
            os.environ.update(original_env)

    def test_google_service_account_rejects_non_google_token_uri(self):
        base = {
            "type": "service_account",
            "client_email": "tracker@test-project.iam.gserviceaccount.com",
            "private_key": "-----BEGIN PRIVATE KEY-----\nTEST\n-----END PRIVATE KEY-----\n",
            "token_uri": self.m.GOOGLE_TOKEN_URI,
        }
        with tempfile.TemporaryDirectory() as td:
            good = Path(td) / "good.json"
            import json
            good.write_text(json.dumps(base), encoding="utf-8")
            loaded = self.m.load_validated_service_account_info(good)
            self.assertEqual(loaded["token_uri"], self.m.GOOGLE_TOKEN_URI)

            bad = dict(base)
            bad["token_uri"] = "https://attacker.example/token"
            evil = Path(td) / "evil.json"
            evil.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ValueError):
                self.m.load_validated_service_account_info(evil)

    def test_stale_runtime_profile_purge_is_bounded(self):
        self.m.APP_DIR = Path(self.tmp.name) / "purge_app"
        runtime = self.m.APP_DIR / "runtime_profiles"
        runtime.mkdir(parents=True, exist_ok=True)
        stale = runtime / "acct_test_old"
        stale.mkdir()
        (stale / "Cookies").write_text("secret", encoding="utf-8")
        outside = self.m.APP_DIR.parent / "outside_keep"
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        removed = self.m._purge_stale_runtime_profiles()
        self.assertEqual(removed, 1)
        self.assertFalse(stale.exists())
        self.assertTrue((outside / "keep.txt").exists())

    def test_delete_account_login_profile_is_bounded(self):
        self.m.APP_DIR = Path(self.tmp.name) / "delete_test_app"
        self.m.ACCOUNT_ROOT = self.m.APP_DIR / "browser_accounts"
        self.m.ACCOUNT_ROOT.mkdir(parents=True, exist_ok=True)
        account = self.m.BrowserAccount("acct_delete_me", "A", "Chrome", "chrome.exe")
        account.profile_dir.mkdir(parents=True, exist_ok=True)
        (account.profile_dir / "Cookies").write_text("secret", encoding="utf-8")
        self.assertTrue(self.m.delete_account_login_profile(account))
        self.assertFalse(account.profile_dir.exists())
        self.assertFalse(self.m.delete_account_login_profile(account))
        with self.assertRaises(ValueError):
            self.m.delete_account_login_profile(
                self.m.BrowserAccount("../escape", "bad", "Chrome", "chrome.exe")
            )


if __name__ == "__main__":
    unittest.main()
