import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

import desktop_app
from session_client import SingleInstance, SessionError


class WindowsSettingsTests(unittest.TestCase):
    def test_duplicate_instance_is_handled_and_lock_releases(self):
        base = "https://lock-test-" + uuid.uuid4().hex + ".invalid"
        first = SingleInstance(base)
        try:
            with self.assertRaises(SessionError):
                SingleInstance(base)
        finally:
            first.close()
        second = SingleInstance(base)
        second.close()

    def test_uses_local_appdata_outside_program_directory(self):
        with patch.object(desktop_app.sys, "platform", "win32"), patch.dict(os.environ, {"LOCALAPPDATA": "C:/Users/Test User/AppData/Local"}):
            self.assertEqual(desktop_app.settings_file(), Path("C:/Users/Test User/AppData/Local/LeedisDesktop/server.json"))

    def test_save_creates_directory_and_survives_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "配置" / "server.json"
            desktop_app.save_server("https://example.com", path)
            self.assertEqual(desktop_app.load_server(path), "https://example.com")
            self.assertEqual(json.loads(path.read_text()), {"server": "https://example.com"})
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_invalid_saved_server_uses_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server.json"
            for value in ("{", '{"server": null}', '{"server": "ftp://bad"}'):
                path.write_text(value)
                self.assertEqual(desktop_app.load_server(path), desktop_app.DEFAULT_SERVER)
