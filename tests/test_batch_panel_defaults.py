"""Batch Console GUI defaults (3 / 1 / 4) vs headless CLI defaults (5 / 4 / 0).

Run: python -m unittest tests/test_batch_panel_defaults.py -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtCore import QSettings, QTimer
from PyQt6.QtWidgets import QApplication, QPushButton, QSpinBox

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import batch_panel as panel

APPROVED = {'concurrency': 3, 'batch_size': 1, 'lane_books': 4}
UNCHANGED = {'memory_percent': 95, 'memory_limit': 0, 'batch_per_restart': 0, 'intra_book_concurrent': False,
             'force': False, 'stall_timeout': 600}


class GuiDefaultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        prefs = str(Path(self.temp.name) / 'prefs.ini')
        self.patches = [patch.object(panel.Panel, 'check_backend', lambda *a, **k: None),
                        patch.object(panel, 'QSettings', lambda *a: QSettings(prefs, QSettings.Format.IniFormat))]
        for item in self.patches:
            item.start()
        self.window = panel.Panel()  # fresh instance: no test harness overrides
        self.window.timer.stop()

    def tearDown(self):
        self.window.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def in_dialog(self, action):
        seen = {}

        def interact():
            dialog = QApplication.activeModalWidget()
            seen['ok'] = dialog is not None and dialog.objectName() == 'batch_settings_dialog'
            action(dialog)
            dialog.accept()
        QTimer.singleShot(0, interact)
        self.window.batch_settings()
        self.assertTrue(seen.get('ok'))

    def test_new_gui_starts_with_approved_defaults(self):
        snapshot = self.window.batch_settings_snapshot()
        self.assertEqual({k: snapshot[k] for k in APPROVED}, APPROVED)
        self.assertEqual({k: snapshot[k] for k in UNCHANGED}, UNCHANGED)
        self.assertEqual((self.window.concurrent.value(), self.window.batch_size.value()), (3, 1))
        self.assertTrue(self.window.advanced['use_subprocess'])  # legacy flag kept; lanes drop it per launch

    def test_dialog_shows_and_reset_restores_approved_defaults(self):
        self.in_dialog(lambda d: self.assertEqual(d.findChild(QSpinBox, 'lane_books').value(), 4))
        # change everything, then reset from the dialog
        self.window.concurrent.setValue(5)
        self.window.batch_size.setValue(4)
        self.window.advanced['lane_books'] = 0

        def reset(dialog):
            button = dialog.findChild(QPushButton, 'reset_batch_defaults')
            self.assertIn('GUI', button.text())
            button.click()
            self.assertEqual(dialog.findChild(QSpinBox, 'lane_books').value(), 4)
        self.in_dialog(reset)
        snapshot = self.window.batch_settings_snapshot()
        self.assertEqual({k: snapshot[k] for k in APPROVED}, APPROVED)
        self.assertEqual({k: snapshot[k] for k in UNCHANGED}, UNCHANGED)

    def test_headless_cli_defaults_unchanged(self):
        cli = vars(panel.batch.parse_args(['--root', '.']))
        self.assertEqual((cli['concurrency'], cli['batch_size'], cli['lane_books']), (5, 4, 0))
        self.assertTrue(cli['use_subprocess'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
