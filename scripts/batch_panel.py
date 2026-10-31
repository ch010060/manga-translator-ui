"""Desktop batch dashboard. Run with the same Python environment as translation."""
from __future__ import annotations

import codecs
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PyQt6.QtCore import QProcess, QProcessEnvironment, QSettings, QTimer, QUrl, Qt, QThread, pyqtSignal, QSize, QEvent, QObject
from PyQt6.QtGui import QDesktopServices, QFont, QColor, QIcon, QPixmap, QPainter
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget, QAbstractItemView, QDialog, QDialogButtonBox, QSizeGrip,
)

import headless_batch_by_first_level_dirs as batch
from batch_job_runtime import WindowsJob, expected_outputs, output_snapshot, validate_outputs


# QSpinBox is signed 32-bit. Use the full safe positive range rather than
# arbitrary workload caps; attempts=-1 is the documented unlimited sentinel.
BATCH_RANGES = {
    'concurrency': (1, 2147483647), 'batch_size': (1, 2147483647),
    'stall_timeout': (0, 2147483647), 'attempts': (-1, 2147483647),
    'timeout': (0, 2147483647), 'memory_limit': (0, 2147483647),
    'memory_percent': (0, 100), 'batch_per_restart': (0, 2147483647),
}
BATCH_FLAGS = ('intra_book_concurrent', 'use_subprocess', 'force')


STYLE = """
QWidget { background:#111a20; color:#dce6e9; font-family:'Microsoft JhengHei UI'; font-size:13px; }
QMainWindow { background:#111a20; }
QLabel#eyebrow { color:#57d8bc; font-size:12px; font-weight:700; }
QLabel#title { font-size:29px; font-weight:700; }
QLabel#stats { background:#1a2931; border:1px solid #30434d; border-radius:10px;
 padding:18px; font-size:18px; }
QLineEdit,QSpinBox,QPlainTextEdit { background:#0c1419; border:1px solid #344751;
 border-radius:6px; padding:8px; selection-background-color:#236756; }
QPushButton { background:#243842; border:1px solid #3c5360; border-radius:6px; padding:9px 16px; }
QPushButton:hover { background:#31505b; }
QPushButton#start { background:#57d8bc; color:#102920; font-weight:700; border:0; }
QPushButton:disabled { color:#667983; background:#1b282f; }
QTableWidget { background:#142129; alternate-background-color:#192831; border:1px solid #30434d;
 border-radius:8px; gridline-color:#263942; selection-background-color:#254b50; }
QHeaderView::section { background:#20323b; padding:11px; border:0; color:#a7bdc6; }
QProgressBar { background:#0b151b; border:0; border-radius:5px; text-align:center; height:20px; }
QProgressBar::chunk { background:#348d7a; border-radius:5px; }
QSplitter::handle { background:#30434d; height:3px; }
"""

# Keep the task layout; only the window chrome and control treatment change.
STYLE = """
QWidget { color:#292c32; font-family:'Microsoft JhengHei UI'; font-size:13px; }
QMainWindow, QDialog { background:#f3f3f5; }
QWidget#surface { background:#f7f7f9; border:1px solid #d2d2d8; border-radius:12px; }
QLabel { background:transparent; }
QLabel#eyebrow { color:#85858f; font-size:11px; font-weight:600; }
QLabel#title { font-size:25px; font-weight:600; }
QLabel#stats { background:#eceef2; border:1px solid #dfe1e6; border-radius:9px;
 padding:14px; font-size:15px; }
QLineEdit,QSpinBox,QPlainTextEdit { background:white; border:1px solid #d0d2d9;
 border-radius:6px; padding:7px; selection-background-color:#c9dfff; selection-color:#20252d; }
QPushButton { background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #ffffff,stop:1 #eeeeF2);
 border:1px solid #c9cbd2; border-radius:6px; padding:7px 13px; }
QPushButton:hover { border-color:#8da8cc; background:#f0f5ff; }
QPushButton:pressed { background:#dde7f6; }
QPushButton#start { background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #4799ff,stop:1 #1671e8);
 color:white; border:1px solid #2369c7; font-weight:600; }
QPushButton:disabled { color:#a4a5ad; background:#ededf1; border-color:#dedee4; }
QTableWidget { background:white; alternate-background-color:#f8f9fb; border:1px solid #dcdde3;
 border-radius:8px; selection-background-color:#e1edff; selection-color:#20252d; }
QHeaderView::section { background:#f0f1f4; padding:10px; border:0;
 border-bottom:1px solid #dcdde3; color:#6a6d77; }
QProgressBar { background:#e5e9ef; border:0; border-radius:5px; text-align:center; height:18px; color:#23344c; }
QProgressBar::chunk { background:#80b7fa; border-radius:5px; }
QSplitter::handle { background:#dedfe5; height:3px; }
QScrollBar:vertical { background:transparent; width:11px; margin:2px; }
QScrollBar::handle:vertical { background:#b5b8c0; border-radius:4px; min-height:30px; }
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical { height:0; }
QCheckBox { spacing:7px; }
"""


STYLE += """
QWidget { color:#e3e7ee; }
QMainWindow,QDialog { background:#15181e; }
QWidget#surface { background:#191d25; border:1px solid #454d5c; border-radius:12px; }
QWidget#titlebar { background:#101319; border:0; border-bottom:1px solid #454d5c;
 border-top-left-radius:12px; border-top-right-radius:12px; }
QWidget#controls { background:#272e3a; border:1px solid #465164; border-radius:9px; }
QWidget#controls QLabel,QWidget#controls QCheckBox { background:transparent; }
QLabel#stats { background:#183442; color:#bde8ff; border:1px solid #36647b;
 border-left:4px solid #63b5ee; }
QLineEdit,QSpinBox { background:#11161e; color:#e8edf7; border:1px solid #536078;
 selection-background-color:#315a86; selection-color:white; }
QPushButton { color:#e5eaf3; background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #3b4555,stop:1 #2c3441);
 border:1px solid #5b677a; }
QPushButton:hover { background:#46566e; border-color:#8ca8cf; }
QPushButton:pressed { background:#243b59; }
QPushButton#start { color:white; border-color:#599de9;
 background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #398fec,stop:1 #2064ba); }
QPushButton:disabled { color:#737e90; background:#252b35; border-color:#394252; }
QTableWidget { background:#1e2530; alternate-background-color:#283241; color:#e2e9f4;
 border:1px solid #536078; selection-background-color:#304f76; selection-color:white; }
QHeaderView::section { background:#354155; color:#cddbf0; border-bottom:1px solid #61718a;
 border-right:1px solid #71809a; }
QPlainTextEdit { background:#0d1117; color:#a9bbcf; border:1px solid #46546b;
 font-family:'Cascadia Mono','Consolas'; font-size:12px; }
QProgressBar { background:#111923; color:#e5f1ff; border:1px solid #46566d; }
QProgressBar::chunk { background:#367bc6; }
QSplitter::handle { background:#536078; height:5px; }
QScrollBar::handle:vertical { background:#6a7890; }
QCheckBox::indicator { width:16px; height:16px; border:1px solid #8594ac; border-radius:4px; background:#151b24; }
QCheckBox::indicator:checked { background:#438de1; border:2px solid #b4d8ff; }
QToolTip { color:#ecf2fc; background:#303c50; border:1px solid #6a7e9e; }
"""


ICON_PATHS = {
    'settings': '<path d="M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z"/><circle cx="12" cy="12" r="3"/>',
    'folder': '<path d="M3 7V5h7l2 2h9v13H3z"/><path d="M3 10h18"/>',
    'play': '<path d="M7 4l13 8-13 8z"/>',
    'stop': '<rect x="5" y="5" width="14" height="14" rx="2"/>',
    'refresh': '<path d="M20 8a9 9 0 1 0 0 9M20 3v6h-6"/>',
    'target': '<circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="2"/><path d="M12 1v4m0 14v4M1 12h4m14 0h4"/>',
    'file': '<path d="M5 2h9l5 5v15H5zM14 2v6h5M8 12h8M8 16h8"/>',
    'search': '<circle cx="10" cy="10" r="6"/><path d="M15 15l6 6"/>',
    'reset': '<path d="M4 10a8 8 0 1 1 1 8M4 4v6h6"/>',
}


def action_icon(name, color='#9fc8fa'):
    icon = QIcon()
    for mode, tint in ((QIcon.Mode.Normal, color), (QIcon.Mode.Disabled, '#677386')):
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
               f'fill="none" stroke="{tint}" stroke-width="1.8" stroke-linecap="round" '
               f'stroke-linejoin="round">{ICON_PATHS[name]}</svg>')
        pixmap = QPixmap(48, 48)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        QSvgRenderer(svg.encode()).render(painter)
        painter.end()
        pixmap.setDevicePixelRatio(2)
        icon.addPixmap(pixmap, mode)
    return icon


def decorate_buttons(parent):
    for button in parent.findChildren(QPushButton):
        text = button.text()
        name = next((name for word, name in (
            ('設定', 'settings'), ('偏好', 'settings'), ('書庫', 'folder'),
            ('開始', 'play'), ('停止', 'stop'), ('重新', 'refresh'),
            ('定位', 'target'), ('輸出', 'folder'), ('日誌', 'file'), ('還原', 'reset'),
        ) if word in text), None)
        if name:
            color = '#ff939b' if name == 'stop' else ('#ffffff' if name == 'play' else '#9fc8fa')
            button.setIcon(action_icon(name, color))
            button.setIconSize(QSize(18, 18))


class ProcessCleanup(QObject):
    """Bounded asynchronous cleanup; success requires kernel/direct exit evidence."""
    done = pyqtSignal(bool, str)
    COMMAND_MS = 3000
    VERIFY_MS = 3000

    @staticmethod
    def command_for(pid):
        return ['taskkill', '/PID', str(pid), '/T', '/F']

    def __init__(self, process, guard, parent=None):
        super().__init__(parent)
        self.process, self.guard = process, guard
        self.notes = []
        self.completed = False
        self.fallback_started = False
        self.command = QProcess(self)
        self.deadline = QTimer(self)
        self.deadline.setSingleShot(True)
        self.deadline.timeout.connect(self.command_timeout)
        self.poll = QTimer(self)
        self.poll.setInterval(50)
        self.poll.timeout.connect(self.verify)
        self.command.finished.connect(self.command_finished)
        self.command.errorOccurred.connect(self.command_error)

    def start(self):
        pid = self.process.processId()
        if os.name == 'nt' and pid:
            self.deadline.start(self.COMMAND_MS)
            command = self.command_for(pid)
            self.command.start(command[0], command[1:])
        else:
            self.fallback('Direct process already exited or no Windows taskkill needed')

    def command_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.fallback('taskkill FailedToStart: ' + self.command.errorString())

    def command_timeout(self):
        self.command.kill()
        self.fallback('taskkill timed out')

    def command_finished(self, code, status):
        self.fallback(f'taskkill exit={code}, status={status.name}')

    def fallback(self, note):
        if self.completed or self.fallback_started:
            return
        self.fallback_started = True
        self.deadline.stop()
        self.notes.append(note)
        try:
            if self.guard is not None:
                if self.guard.active_count():
                    self.guard.terminate()
                    self.notes.append('TerminateJobObject issued; awaiting active_count=0')
            elif os.name == 'nt' and self.process.state() != QProcess.ProcessState.NotRunning:
                self.notes.append('No Windows containment handle: descendants cannot be verified')
            if self.process.state() != QProcess.ProcessState.NotRunning:
                self.process.kill()
        except Exception as exc:
            self.notes.append(f'Cleanup fallback error: {exc}')
        self.verify_until = time.monotonic() + self.VERIFY_MS / 1000
        self.poll.start()
        self.verify()

    def verify(self):
        if self.completed:
            return
        try:
            direct_gone = self.process.state() == QProcess.ProcessState.NotRunning
            tree_gone = self.guard.active_count() == 0 if self.guard is not None else os.name != 'nt'
            if direct_gone and tree_gone:
                self.finish(True, 'Verified direct process and contained descendants exited')
                return
        except Exception as exc:
            self.notes.append(f'Cleanup verification error: {exc}')
            self.finish(False, 'Unable to verify process-tree termination')
            return
        if time.monotonic() >= self.verify_until:
            self.finish(False, 'Cleanup deadline exceeded; termination NOT confirmed')

    def finish(self, ok, note):
        if self.completed:
            return
        self.completed = True
        self.poll.stop()
        self.deadline.stop()
        self.notes.append(note)
        self.done.emit(ok, '; '.join(self.notes))


class Lifecycle(Enum):
    IDLE = auto()
    PREFLIGHT = auto()
    RUNNING = auto()
    STOPPING = auto()
    FINISHED = auto()


@dataclass(frozen=True)
class BackendRequest:
    generation: int
    identity: tuple
    config: Path
    endpoint: str
    content: bytes


class BackendCheck(QThread):
    result = pyqtSignal(bool, str, str)

    def __init__(self, request, parent=None):
        super().__init__(parent)
        self.request = request

    def run(self):
        try:
            data = json.loads(self.request.content)
            translator = str((data.get('translator') or {}).get('translator', '')).lower()
            if translator != 'sakura':
                self.result.emit(False, f'目前連線檢查僅支援 Sakura，設定使用 {translator or "未指定"}；請選用 Sakura 設定檔。', '')
                return
            # One request replaces the previous preflight + duplicate /models
            # fetch. Retain the CLI preflight's 4xx-reachable compatibility;
            # neither reachability nor a model list is an inference guarantee.
            url = self.request.endpoint + '/models'
            request = Request(url, headers={'Authorization': 'Bearer sk-114514'})
            models = ''
            try:
                with urlopen(request, timeout=3) as response:
                    message = f'Sakura API endpoint reachable: {url}'
                    try:
                        payload = json.load(response)
                        models = ', '.join(str(item['id']) for item in payload.get('data', [])
                                           if isinstance(item, dict) and item.get('id'))
                    except Exception as exc:
                        message += f'\n無法取得模型清單：{exc}'
            except HTTPError as exc:
                if not 400 <= exc.code < 500:
                    raise
                message = f'Sakura API endpoint reachable: {url} (HTTP {exc.code}; 未驗證推論授權)'
            self.result.emit(True, message, models)
        except Exception as exc:
            self.result.emit(False, str(exc), '')


STYLE += """
QPushButton { padding:5px 10px; }
QLineEdit,QSpinBox { padding:5px; }
QLabel#stats { padding:8px; font-size:13px; }
QHeaderView::section { padding:6px; }
QProgressBar { border:0; border-radius:4px; height:14px; }
"""


class TitleBar(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.owner = window
        self.setObjectName('titlebar')
        self.setMinimumHeight(40)
        row = QHBoxLayout(self)
        row.setContentsMargins(18, 5, 18, 5)
        for color, label, action in (
            ('#ff5f57', '關閉', window.close),
            ('#febc2e', '最小化', window.showMinimized),
            ('#28c840', '最大化／還原', self.toggle_maximized),
        ):
            button = QPushButton()
            button.setFixedSize(14, 14)
            button.setToolTip(label)
            button.setAccessibleName(label)
            button.setStyleSheet(f'QPushButton {{background:{color};border:1px solid rgba(0,0,0,35);border-radius:7px;padding:0;}}')
            button.clicked.connect(action)
            row.addWidget(button)
        row.addStretch()
        title = QLabel('批次翻譯工作台')
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(title)
        row.addStretch()
        self.preferences = QPushButton('介面偏好')
        row.addWidget(self.preferences)

    def toggle_maximized(self):
        self.owner.showNormal() if self.owner.isMaximized() else self.owner.showMaximized()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.owner.windowHandle():
            self.owner.windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, event):
        self.toggle_maximized()


class Panel(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Translation Atelier | Batch Console')
        self.resize(1220, 840)
        self.setMinimumSize(980, 600)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.settings = QSettings('MangaTranslator', 'BatchPanel')
        self.defaults = vars(batch.parse_args(['--root', '.']))
        self.advanced = {key: self.defaults[key] for key in (
            'attempts', 'timeout', 'memory_limit', 'memory_percent', 'batch_per_restart',
            'intra_book_concurrent', 'use_subprocess')}
        self.follow = self.settings.value('follow', False, type=bool)
        self.notify = self.settings.value('notify', True, type=bool)
        self.auto_scan = self.settings.value('auto_scan', True, type=bool)
        self.log_follow = self.settings.value('log_follow', True, type=bool)
        self.backend_ok = False
        self.backend_worker = None
        self.pending_start = False
        self.pending_identity = None
        self.pending_selection = None
        self.lifecycle = Lifecycle.IDLE
        self.closing = False
        self.backend_generation = 0
        self.backend_request = None
        self.queued_check = None
        self.backend_identity = None
        self.backend_reply = None
        self.run_generation = 0
        self.run_jobs = ()
        self.scanned_root = None
        self.jobs = []
        self.running = False
        self.started = 0.0
        self.ended = None
        self.batch_stopped = False
        self.cleaners = []
        container = QWidget()
        container.setObjectName('surface')
        self.setCentralWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(12, 0, 12, 14)
        layout.setSpacing(8)
        self.titlebar = TitleBar(self)
        self.titlebar.preferences.clicked.connect(self.preferences)
        layout.addWidget(self.titlebar)
        self.controls = QWidget()
        self.controls.setObjectName('controls')
        form = QFormLayout(self.controls)
        form.setContentsMargins(12, 8, 12, 8)
        form.setVerticalSpacing(6)
        self.root = QLineEdit(str(self.settings.value('root', '')))
        root_row = QHBoxLayout()
        root_row.addWidget(self.root)
        browse = QPushButton('選擇書庫')
        browse.clicked.connect(self.choose_root)
        root_row.addWidget(browse)
        form.addRow('書庫目錄', root_row)
        self.config = QLineEdit(str(self.settings.value('config', batch.resolve_default_config_path())))
        config_row = QHBoxLayout()
        config_row.addWidget(self.config)
        config_button = QPushButton('選擇設定檔')
        config_button.clicked.connect(self.choose_config)
        config_row.addWidget(config_button)
        form.addRow('翻譯設定', config_row)
        options = QHBoxLayout()
        self.concurrent = self.spin(options, '同時處理', *BATCH_RANGES['concurrency'], self.defaults['concurrency'])
        self.batch_size = self.spin(options, '批次大小', *BATCH_RANGES['batch_size'], self.defaults['batch_size'])
        self.stall = self.spin(options, '停滯上限（秒，0 關閉）', *BATCH_RANGES['stall_timeout'], self.defaults['stall_timeout'])
        self.force = QCheckBox('覆寫既有輸出')
        self.force.setChecked(self.defaults['force'])
        self.force.setParent(self)
        self.force.hide()
        options.addStretch()
        form.addRow(options)
        # Keep the library visible on displays with limited logical height.
        # Optional controls expand in place without changing the task layout.
        form.setRowVisible(1, False)
        form.setRowVisible(2, False)
        expand_controls = QPushButton('展開常用參數')
        expand_controls.setCheckable(True)
        def toggle_controls(expanded):
            form.setRowVisible(1, expanded)
            form.setRowVisible(2, expanded)
            expand_controls.setText('收合常用參數' if expanded else '展開常用參數')
        expand_controls.toggled.connect(toggle_controls)
        root_row.addWidget(expand_controls)
        layout.addWidget(self.controls)
        backend_row = QHBoxLayout()
        self.backend_status = QLabel('正在檢查翻譯後端…')
        self.backend_status.setWordWrap(False)
        self.backend_status.setToolTip('連線成功只代表服務可達，不保證模型能正常完成翻譯。')
        backend_row.addWidget(self.backend_status, 1)
        self.retry_backend = QPushButton('重新檢查')
        self.retry_backend.hide()
        self.retry_backend.clicked.connect(lambda: self.check_backend())
        backend_row.addWidget(self.retry_backend)
        actions = QHBoxLayout()
        self.scan_button = QPushButton('掃描書庫')
        self.scan_button.clicked.connect(self.scan)
        self.start_button = QPushButton('開始翻譯')
        self.start_button.setObjectName('start')
        self.start_button.clicked.connect(self.start)
        self.stop_button = QPushButton('停止所有工作')
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        for button in (self.scan_button, self.start_button, self.stop_button):
            actions.addWidget(button)
        self.clear_button = QPushButton('清空任務')
        self.clear_button.setToolTip('清空面板清單與進度；不刪除原圖、輸出或日誌檔案。')
        self.clear_button.setIcon(action_icon('reset'))
        self.clear_button.clicked.connect(self.clear_tasks)
        actions.addWidget(self.clear_button)
        actions.addStretch()
        self.batch_settings_button = QPushButton('批次設定')
        self.batch_settings_button.clicked.connect(self.batch_settings)
        actions.addWidget(self.batch_settings_button)
        locate = QPushButton('定位執行中')
        locate.clicked.connect(self.locate_running)
        actions.addWidget(locate)
        open_button = QPushButton('開啟所選輸出')
        open_button.clicked.connect(self.open_result)
        actions.addWidget(open_button)
        log_button = QPushButton('查看完整日誌')
        log_button.clicked.connect(self.open_log)
        actions.addWidget(log_button)
        layout.addLayout(actions)
        self.stats = QLabel('READY    選擇書庫後開始')
        self.stats.setObjectName('stats')
        layout.addWidget(self.stats)
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜尋書名…')
        self.search.setClearButtonEnabled(True)
        self.search.addAction(action_icon('search'), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self.filter_books)
        layout.addWidget(self.search)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.content_splitter = splitter
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['書籍', '狀態', '輸出進度', '經過時間'])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setMinimumSectionSize(80)
        for col, width in ((1, 100), (2, 240), (3, 100)):
            self.table.setColumnWidth(col, width)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.horizontalHeader().setFixedHeight(32)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalScrollBar().sliderPressed.connect(self.pause_follow)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self.show_log)
        splitter.addWidget(self.table)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1500)
        self.log.setPlaceholderText('選取書籍，查看最近的執行日誌。完整日誌保存在 result/batch_logs。')
        splitter.addWidget(self.log)
        self.log.hide()
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([600, 0])
        layout.addWidget(splitter, 1)
        self.log_toggle = QCheckBox('顯示執行日誌')
        self.log_toggle.toggled.connect(self.toggle_log)
        layout.addWidget(self.log_toggle)
        self.overall = QProgressBar()
        self.overall.setFormat('等待開始')
        layout.addWidget(self.overall)
        self.backend_bar = QWidget()
        self.backend_bar.setStyleSheet('background:#10151d;border-top:1px solid #465164;')
        backend_row.setContentsMargins(10, 5, 10, 5)
        self.backend_bar.setLayout(backend_row)
        layout.addWidget(self.backend_bar)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1500)
        self.check_delay = QTimer(self)
        self.check_delay.setSingleShot(True)
        self.check_delay.setInterval(500)
        self.check_delay.timeout.connect(self.check_backend)
        self.config.textChanged.connect(self.config_changed)
        QTimer.singleShot(0, self.check_backend)
        decorate_buttons(self)
        for widget in [self, *self.findChildren(QWidget)]:
            widget.setMouseTracking(True)
            widget.installEventFilter(self)
        self.sync_ui()

    def has_owned_processes(self):
        return any(job.get('process') is not None or job.get('guard') is not None
                   or job.get('cleanup') is not None for job in self.jobs)

    def can_edit_jobs(self):
        return (not self.closing and self.lifecycle in (Lifecycle.IDLE, Lifecycle.FINISHED)
                and not self.pending_start and not self.has_owned_processes())

    def transition(self, state):
        legal = {
            Lifecycle.IDLE: {Lifecycle.PREFLIGHT, Lifecycle.RUNNING},
            Lifecycle.PREFLIGHT: {Lifecycle.IDLE, Lifecycle.FINISHED, Lifecycle.RUNNING},
            Lifecycle.RUNNING: {Lifecycle.STOPPING, Lifecycle.FINISHED},
            Lifecycle.STOPPING: {Lifecycle.FINISHED},
            Lifecycle.FINISHED: {Lifecycle.IDLE, Lifecycle.PREFLIGHT, Lifecycle.RUNNING},
        }
        if state != self.lifecycle and state not in legal[self.lifecycle]:
            raise RuntimeError(f'Illegal lifecycle transition: {self.lifecycle.name} -> {state.name}')
        self.lifecycle = state
        self.running = state == Lifecycle.RUNNING  # Compatibility for existing P0 callbacks/tests.
        self.sync_ui()

    def sync_ui(self):
        editable = self.can_edit_jobs()
        checking = self.backend_worker is not None or self.queued_check is not None
        self.controls.setEnabled(editable)
        self.batch_settings_button.setEnabled(editable)
        self.scan_button.setEnabled(editable)
        self.clear_button.setEnabled(editable)
        self.start_button.setEnabled(editable and (self.backend_ok or checking))
        self.stop_button.setEnabled(self.has_owned_processes() and
                                    any(j.get('process') is not None and j.get('cleanup') is None for j in self.jobs))
        self.retry_backend.setEnabled(editable and not checking)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None:
                flags = item.flags()
                item.setFlags(flags | Qt.ItemFlag.ItemIsUserCheckable if editable
                              else flags & ~Qt.ItemFlag.ItemIsUserCheckable)

    def reset_summary(self):
        if hasattr(self, 'completion_box'):
            self.completion_box.close()
        self.run_jobs = ()
        self.started = 0.0
        self.ended = None
        self.batch_stopped = False
        self.stats.setStyleSheet('')
        self.overall.setRange(0, 100)
        self.overall.setValue(0)
        self.overall.setFormat('等待開始')

    def clear_tasks(self):
        if not self.can_edit_jobs():
            return
        self.reset_summary()
        self.jobs.clear()
        self.table.setRowCount(0)
        self.log.clear()
        self.search.clear()
        self.scanned_root = None
        self.stats.setText('任務已清空')
        self.transition(Lifecycle.IDLE)

    def toggle_log(self, visible):
        self.log.setVisible(visible)
        if visible:
            available = max(1, self.content_splitter.height())
            log_height = min(220, max(100, available // 3))
            self.content_splitter.setSizes([max(1, available - log_height), log_height])
            if self.table.currentRow() < 0 and self.jobs:
                self.table.selectRow(0)
            self.show_log()
        else:
            self.content_splitter.setSizes([self.content_splitter.height(), 0])

    def eventFilter(self, watched, event):
        if (isinstance(watched, QWidget) and watched.window() is self
                and event.type() in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress)):
            point = self.mapFromGlobal(event.globalPosition().toPoint())
            edges = Qt.Edge(0)
            if not self.isMaximized():
                if point.x() < 7:
                    edges |= Qt.Edge.LeftEdge
                elif point.x() >= self.width() - 7:
                    edges |= Qt.Edge.RightEdge
                if point.y() < 7:
                    edges |= Qt.Edge.TopEdge
                elif point.y() >= self.height() - 7:
                    edges |= Qt.Edge.BottomEdge
            if edges:
                horizontal = bool(edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge))
                vertical = bool(edges & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge))
                if horizontal and vertical:
                    cursor = (Qt.CursorShape.SizeFDiagCursor if edges in (
                        Qt.Edge.LeftEdge | Qt.Edge.TopEdge, Qt.Edge.RightEdge | Qt.Edge.BottomEdge
                    ) else Qt.CursorShape.SizeBDiagCursor)
                else:
                    cursor = Qt.CursorShape.SizeHorCursor if horizontal else Qt.CursorShape.SizeVerCursor
                watched.setCursor(cursor)
                if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                    if self.windowHandle():
                        self.windowHandle().startSystemResize(edges)
                        return True
            else:
                watched.unsetCursor()
        return super().eventFilter(watched, event)

    def filter_books(self):
        query = self.search.text().strip().casefold()
        for row, job in enumerate(self.jobs):
            self.table.setRowHidden(row, query not in job['book'].name.casefold())

    def config_identity(self):
        config = Path(self.config.text()).expanduser().resolve()
        try:
            content = config.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
        except OSError as exc:
            content, digest = b'', str(exc)
        endpoint = batch.resolve_sakura_api_base()
        return (str(config), digest, endpoint), config, content, endpoint

    def new_check(self, identity_data):
        identity, config, content, endpoint = identity_data
        self.backend_generation += 1
        request = BackendRequest(self.backend_generation, identity, config, endpoint, content)
        self.queued_check = request
        return request

    def cancel_pending_start(self):
        self.pending_start = False
        self.pending_identity = None
        self.pending_selection = None
        if self.lifecycle == Lifecycle.PREFLIGHT:
            self.transition(Lifecycle.FINISHED if self.ended is not None else Lifecycle.IDLE)

    def config_changed(self):
        if self.closing:
            return
        self.cancel_pending_start()
        self.backend_ok = False
        self.backend_identity = None
        self.new_check(self.config_identity())
        self.backend_status.setText('設定已變更，等待重新檢查…')
        self.backend_status.setStyleSheet('color:#b8c8dd;border:0;padding:0;')
        if self.backend_worker is None and not self.has_owned_processes():
            self.check_delay.start()
        self.sync_ui()

    def check_backend(self, launch=False):
        if self.closing or self.lifecycle in (Lifecycle.RUNNING, Lifecycle.STOPPING) or self.has_owned_processes():
            return
        data = self.config_identity()
        identity = data[0]
        if launch:
            if self.pending_start:
                return  # Repeated Start is the same intent, never another batch.
            self.pending_start = True
            self.pending_identity = identity
            self.pending_selection = tuple(self.jobs[row] for row in range(len(self.jobs))
                                           if self.table.item(row, 0).checkState() == Qt.CheckState.Checked)
            self.transition(Lifecycle.PREFLIGHT)
        request = self.queued_check
        if request is None or request.identity != identity:
            current = self.backend_request
            if (self.backend_worker is not None and current is not None
                    and current.generation == self.backend_generation and current.identity == identity):
                self.sync_ui()
                return  # An explicit Start can attach to this in-flight check.
            request = self.new_check(data)
        self.check_delay.stop()
        self.backend_ok = False
        self.backend_identity = None
        self.backend_status.setText('正在連接翻譯後端…')
        self.backend_status.setStyleSheet('color:#b8c8dd;border:0;padding:0;')
        if self.backend_worker is None:
            self.queued_check = None
            self.backend_request = request
            self.backend_reply = None
            worker = BackendCheck(request, self)
            self.backend_worker = worker
            worker.result.connect(lambda ok, message, models, r=request:
                                  self.backend_result(r, ok, message, models))
            worker.finished.connect(lambda r=request, w=worker: self.backend_finished(r, w))
            worker.start()
        self.sync_ui()

    def applicable_check(self, request):
        return (not self.closing and request is self.backend_request
                and request.generation == self.backend_generation
                and request.identity == self.config_identity()[0]
                and self.lifecycle not in (Lifecycle.RUNNING, Lifecycle.STOPPING)
                and not self.has_owned_processes())

    def backend_result(self, request, ok, message, models=''):
        if not self.applicable_check(request):
            return
        self.backend_reply = (ok, message, models)

    def display_backend_result(self, ok, message, models):
        self.backend_ok = ok
        self.retry_backend.setVisible(not ok)
        summary = ('已連線  |  模型：' + (models or '未取得模型資訊')) if ok else '後端離線  |  模型：不可用'
        self.backend_status.setText(summary if len(summary) <= 110 else summary[:107] + '…')
        self.backend_status.setToolTip(summary + '\n' + message +
                                       '\n連線成功只代表服務可達，不保證模型能正常完成翻譯。'
                                       '\n模型名稱為後端回報的可用模型，不代表已完成推論驗證。')
        self.backend_status.setStyleSheet(
            'border:0;padding:0;color:' + ('#86dfac;' if ok else '#ff8e97;'))
    def backend_finished(self, request, worker):
        if self.backend_worker is not worker:
            return
        self.backend_worker = None
        worker.deleteLater()
        if self.closing:
            QTimer.singleShot(0, self.close)
            return
        if request.identity != self.config_identity()[0] and request.generation == self.backend_generation:
            self.config_changed()  # Same-path edits or endpoint changes without a textChanged signal.
        if self.queued_check is not None:
            self.check_backend()
            return
        if not self.applicable_check(request):
            self.sync_ui()
            return
        # A thread finishing without a result is not success. Fail closed rather
        # than reusing a previous reply; covered by the missing-reply regression.
        ok, message, models = self.backend_reply or (False, '後端檢查未回傳結果', '')
        self.backend_identity = request.identity
        self.display_backend_result(ok, message, models)
        launch = self.pending_start and self.pending_identity == request.identity
        if launch and ok:
            self.begin_translation(approved=request)
        else:
            self.cancel_pending_start()
            if launch:
                QMessageBox.warning(self, '無法開始翻譯', message + '\n\n請啟動後端服務後按「重新檢查」。')
        self.sync_ui()

    def pause_follow(self):
        self.follow = False

    def locate_running(self):
        for row, job in enumerate(self.jobs):
            if job['status'] == '執行中':
                self.table.scrollToItem(self.table.item(row, 0))
                break

    def preferences(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('介面偏好')
        layout = QVBoxLayout(dialog)
        follow = QCheckBox('跟隨新開始的書籍（手動拖曳捲軸會暫停）')
        follow.setChecked(self.follow)
        layout.addWidget(follow)
        auto_scan = QCheckBox('開啟時自動掃描上次書庫')
        auto_scan.setChecked(self.auto_scan)
        notify = QCheckBox('批次結束時提示音並提醒視窗')
        notify.setChecked(self.notify)
        log_follow = QCheckBox('日誌停在底部時跟隨新訊息')
        log_follow.setChecked(self.log_follow)
        for widget in (auto_scan, notify, log_follow):
            layout.addWidget(widget)
        layout.addWidget(QLabel('開啟時載入上次書庫；不會自動開始翻譯。\n日誌在底部時自動跟隨，往上捲動時保留位置。'))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        decorate_buttons(dialog)
        if dialog.exec():
            self.follow = follow.isChecked()
            self.auto_scan = auto_scan.isChecked()
            self.notify = notify.isChecked()
            self.log_follow = log_follow.isChecked()
            for key in ('follow', 'auto_scan', 'notify', 'log_follow'):
                self.settings.setValue(key, getattr(self, key))

    def batch_settings_snapshot(self):
        return dict(self.advanced, concurrency=self.concurrent.value(), batch_size=self.batch_size.value(),
                    stall_timeout=self.stall.value(), force=self.force.isChecked())

    def default_batch_settings(self):
        return {key: self.defaults[key] for key in (*BATCH_RANGES, *BATCH_FLAGS)}

    def apply_batch_settings(self, draft):
        # The nested dialog event loop can process lifecycle changes. Recheck
        # ownership at commit time, not only when the dialog was opened.
        if not self.can_edit_jobs():
            return
        for key, (minimum, maximum) in BATCH_RANGES.items():
            if type(draft[key]) is not int or not minimum <= draft[key] <= maximum:
                raise ValueError(f'{key} must be between {minimum} and {maximum}')
        if any(type(draft[key]) is not bool for key in BATCH_FLAGS):
            raise ValueError('Batch flags must be boolean')
        self.advanced = {key: draft[key] for key in self.advanced}
        self.concurrent.setValue(draft['concurrency'])
        self.batch_size.setValue(draft['batch_size'])
        self.stall.setValue(draft['stall_timeout'])
        self.force.setChecked(draft['force'])

    def batch_settings(self):
        if not self.can_edit_jobs():
            return
        draft = self.batch_settings_snapshot()
        dialog = QDialog(self)
        dialog.setObjectName('batch_settings_dialog')
        dialog.setWindowTitle('批次設定')
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('常用設定保留於主畫面；以下為進階參數。'))
        form = QFormLayout()
        widgets = {}
        for key, label in (
            ('attempts', '重試次數（-1 無限重試）'), ('timeout', '單本總逾時（秒，0 關閉）'),
            ('memory_limit', '固定記憶體限制（MB，0 關閉）'),
            ('memory_percent', '記憶體比例限制（%，0 關閉）'),
            ('batch_per_restart', '每隔幾批重啟（0 關閉）'),
        ):
            widget = QSpinBox()
            widget.setObjectName(key)
            widget.setRange(*BATCH_RANGES[key])
            widget.setValue(draft[key])
            widgets[key] = widget
            form.addRow(label, widget)
        for key, label in (('intra_book_concurrent', '書內並行'), ('use_subprocess', '子程序模式')):
            widget = QCheckBox()
            widget.setObjectName(key)
            widget.setChecked(draft[key])
            widgets[key] = widget
            form.addRow(label, widget)
        layout.addLayout(form)
        overwrite = QCheckBox('覆寫既有輸出（關閉時跳過已存在的圖片）')
        overwrite.setObjectName('force')
        overwrite.setChecked(draft['force'])
        layout.addWidget(overwrite)
        reset = QPushButton('還原腳本預設（包含主畫面參數）')
        reset.setObjectName('reset_batch_defaults')
        layout.addWidget(reset)
        def reset_values():
            draft.update(self.default_batch_settings())
            overwrite.setChecked(draft['force'])
            for key, widget in widgets.items():
                if isinstance(widget, QCheckBox):
                    widget.setChecked(draft[key])
                else:
                    widget.setValue(draft[key])
        reset.clicked.connect(reset_values)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        decorate_buttons(dialog)
        if dialog.exec():
            draft.update({key: widget.isChecked() if isinstance(widget, QCheckBox) else widget.value()
                          for key, widget in widgets.items()})
            draft['force'] = overwrite.isChecked()
            self.apply_batch_settings(draft)

    @staticmethod
    def spin(row, label, minimum, maximum, value):
        row.addWidget(QLabel(label))
        widget = QSpinBox()
        widget.setRange(minimum, maximum)
        widget.setValue(value)
        row.addWidget(widget)
        return widget

    def choose_root(self):
        if not self.can_edit_jobs():
            return
        path = QFileDialog.getExistingDirectory(self, '選擇含書籍子資料夾的書庫', self.root.text())
        if path:
            self.root.setText(path)
            self.scan()

    def choose_config(self):
        if not self.can_edit_jobs():
            return
        path, _ = QFileDialog.getOpenFileName(self, '選擇設定檔', self.config.text(), 'JSON (*.json)')
        if path:
            self.config.setText(path)

    def scan(self):
        if not self.can_edit_jobs():
            return
        try:
            if not self.root.text().strip():
                return
            books = batch.discover_book_dirs(Path(self.root.text()).expanduser())
            config = batch.load_config_dict(Path(self.config.text()))
            output_format = (config.get('cli') or {}).get('format')
            self.reset_summary()
            self.jobs = []
            self.table.setRowCount(0)
            for book in books:
                total = batch.count_source_images(book)
                outputs = expected_outputs(book, output_format)
                done, validation_errors = validate_outputs(outputs)
                row = self.table.rowCount()
                self.table.insertRow(row)
                for col, text in ((0, book.name), (1, '待處理'), (3, '--:--')):
                    self.table.setItem(row, col, QTableWidgetItem(text))
                self.table.item(row, 0).setCheckState(Qt.CheckState.Checked if done < total else Qt.CheckState.Unchecked)
                self.table.item(row, 0).setToolTip(str(book))
                status = '已完成' if done >= total else ('部分完成' if done else '待處理')
                self.table.item(row, 1).setText(status)
                progress = QProgressBar()
                progress.setRange(0, total)
                progress.setValue(min(done, total))
                progress.setFormat(f'{done} / {total}')
                self.table.setCellWidget(row, 2, progress)
                self.table.setRowHeight(row, 32)
                self.jobs.append(dict(book=book, total=total, done=done, status=status,
                                      process=None, start=None, end=None, last=time.monotonic(),
                                      complete=None, tail='', decoder=None, path=None, file=None,
                                      outputs=outputs, guard=None, cleanup=None, exit_code=None,
                                      stop_reason=None, error=None))
            self.stats.setText(f'READY    {len(books)} 本書籍  /  {sum(j["total"] for j in self.jobs)} 頁')
            self.settings.setValue('root', self.root.text())
            self.scanned_root = Path(self.root.text()).expanduser().resolve()
            self.filter_books()
            self.transition(Lifecycle.IDLE)
        except Exception as exc:
            QMessageBox.warning(self, '無法掃描書庫', str(exc))

    def start(self):
        if not self.can_edit_jobs():
            return
        if not self.jobs or self.scanned_root != Path(self.root.text()).expanduser().resolve():
            self.scan()
        if not any(self.table.item(row, 0).checkState() == Qt.CheckState.Checked
                   for row in range(len(self.jobs))):
            QMessageBox.information(self, '尚未選擇書籍', '請先勾選要翻譯的書籍。')
            return
        self.check_backend(launch=True)

    def reset_job(self, job, outputs, selected):
        if job.get('process') is not None or job.get('guard') is not None or job.get('cleanup') is not None:
            raise RuntimeError('Cannot reset an owned/unverified process')
        valid, _ = validate_outputs(outputs)
        job.update(outputs=outputs, total=len(outputs), selected=selected, initial_valid=valid,
                   initial_snapshot=output_snapshot(outputs), available=valid, done=valid,
                   start=None, end=None, complete=None, exit_code=None, exit_status=None,
                   stop_reason=None, error=None, tail='', decoder=None, path=None, file=None,
                   baseline={}, snapshot={}, exit_seen=False, finalized=False,
                   run_id=self.run_generation, status='待處理' if selected else '未選取')
        if selected and self.options.force:
            job['done'] = 0
        elif selected and valid == len(outputs):
            job['status'] = '已完成'

    def begin_translation(self, approved=None):
        # Internal entry used by synthetic P0 tests. GUI Start enters through the
        # request-identified PREFLIGHT path; never reset a live/uncleaned job.
        if self.closing or self.has_owned_processes() or self.lifecycle in (Lifecycle.RUNNING, Lifecycle.STOPPING):
            return
        if self.lifecycle == Lifecycle.PREFLIGHT and (
                approved is None or not self.applicable_check(approved) or not self.pending_start):
            return
        if self.scanned_root != Path(self.root.text()).expanduser().resolve():
            if self.lifecycle == Lifecycle.PREFLIGHT:
                self.cancel_pending_start()
                return
            self.scan()
        if not self.jobs:
            self.scan()
        if not self.jobs:
            return
        selected = (self.pending_selection if self.lifecycle == Lifecycle.PREFLIGHT else
                    tuple(self.jobs[row] for row in range(len(self.jobs))
                          if self.table.item(row, 0).checkState() == Qt.CheckState.Checked))
        if not selected:
            QMessageBox.information(self, '尚未選擇書籍', '請先勾選要翻譯的書籍。')
            self.cancel_pending_start()
            return
        try:
            batch.load_config_dict(Path(self.config.text()))
            if not Path(self.config.text()).is_file():
                raise ValueError('設定檔不存在')
            output_format = (batch.load_config_dict(Path(self.config.text())).get('cli') or {}).get('format')
            output_sets = [expected_outputs(job['book'], output_format) for job in self.jobs]
        except Exception as exc:
            QMessageBox.warning(self, '設定檔錯誤', str(exc))
            self.cancel_pending_start()
            return
        self.settings.setValue('config', self.config.text())
        self.options = batch.BatchOptions(
            config=Path(self.config.text()).resolve(), force=self.force.isChecked(),
            use_gpu=False, batch_size=self.batch_size.value(),
            **{key: value for key, value in self.advanced.items() if key != 'timeout'},
        )
        self.reset_summary()
        self.run_generation += 1
        self.started = time.monotonic()
        self.ended = None
        self.batch_stopped = False
        self.stats.setStyleSheet('')
        selected_ids = {id(job) for job in selected}
        for job, outputs in zip(self.jobs, output_sets):
            self.reset_job(job, outputs, id(job) in selected_ids)
        self.run_jobs = tuple(job for job in self.jobs if job['selected'])
        self.pending_start = False
        self.pending_selection = None
        self.pending_identity = None
        self.log.clear()
        self.transition(Lifecycle.RUNNING)
        self.schedule()

    def schedule(self):
        if self.lifecycle != Lifecycle.RUNNING:
            return
        active = sum(j['process'] is not None for j in self.jobs)
        for job in self.jobs:
            if active >= self.concurrent.value():
                break
            if job['status'] != '待處理':
                continue
            logs = batch.PROJECT_ROOT / 'result' / 'batch_logs'
            logs.mkdir(parents=True, exist_ok=True)
            path = batch.make_log_path(logs, self.jobs.index(job) + 1, job['book'].name)
            job['path'] = path.with_name(f'{path.stem}_{self.run_generation}_{uuid.uuid4().hex[:8]}{path.suffix}')
            try:
                job['file'] = job['path'].open('xb')
            except OSError as exc:
                job['status'] = '失敗'
                job['tail'] = str(exc)
                continue
            process = QProcess(self)
            process.setWorkingDirectory(str(batch.PROJECT_ROOT))
            process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            env = QProcessEnvironment.systemEnvironment()
            env.insert('PYTHONUNBUFFERED', '1')
            env.insert('PYTHONIOENCODING', 'utf-8')
            process.setProcessEnvironment(env)
            command = batch.build_local_command(Path(sys.executable), job['book'], self.options)
            if os.name == 'nt':
                try:
                    job['guard'] = WindowsJob()
                    command = [sys.executable, str(Path(__file__).with_name('batch_job_runtime.py')),
                               job['guard'].name, *command]
                except Exception as exc:
                    self.job_message(job, f'Cannot contain translator process: {exc}')
                    job['status'] = '失敗'
                    job['file'].close()
                    job['file'] = None
                    process.deleteLater()
                    continue
            job['baseline'] = output_snapshot(job['outputs'])
            job['snapshot'] = job['baseline']
            if self.options.force:
                job['done'] = 0
            job.update(process=process, status='執行中', start=time.monotonic(),
                       last=time.monotonic(), decoder=codecs.getincrementaldecoder('utf-8')('replace'))
            token = job['run_id']
            process.readyReadStandardOutput.connect(lambda j=job, p=process, t=token:
                                                    self.drain(j) if self.owns_callback(j, p, t) else None)
            process.finished.connect(lambda code, status, j=job, p=process, t=token:
                                     self.finished(j, code, status, p, t))
            process.errorOccurred.connect(lambda error, j=job, p=process, t=token:
                                          self.process_error(j, error) if self.owns_callback(j, p, t) else None)
            process.start(command[0], command[1:])
            if self.follow:
                self.table.scrollToItem(self.table.item(self.jobs.index(job), 0))
            active += 1
        if not any(j['process'] is not None or j['status'] == '待處理' for j in self.jobs):
            self.complete_batch()
        self.sync_ui()

    def owns_callback(self, job, process, token):
        return (job.get('process') is process and job.get('run_id') == token
                and token == self.run_generation and not job.get('finalized'))

    def drain(self, job):
        if job['process'] is None:
            return
        data = bytes(job['process'].readAllStandardOutput())
        if job['file']:
            job['file'].write(data)
            job['file'].flush()
        job['tail'] = (job['tail'] + job['decoder'].decode(data))[-60000:]

    def process_error(self, job, error):
        if job['process'] is not None and error == QProcess.ProcessError.FailedToStart:
            job['tail'] += '\n' + job['process'].errorString()
            self.finished(job, -1)

    def job_message(self, job, message):
        text = f'\n[Batch panel] {message}\n'
        job['tail'] = (job['tail'] + text)[-60000:]
        if job.get('file'):
            job['file'].write(text.encode('utf-8'))
            job['file'].flush()

    def finished(self, job, code, exit_status=None, process=None, token=None):
        if (job['process'] is None or job.get('exit_seen') or
                (process is not None and not self.owns_callback(job, process, token))):
            return
        job['exit_seen'] = True
        self.drain(job)
        job['exit_code'] = code
        job['exit_status'] = exit_status
        if job.get('cleanup') is not None:
            return  # Cleanup controller must verify the entire job first.
        process, token = job['process'], job['run_id']
        QTimer.singleShot(250, lambda j=job, p=process, t=token:
                          self.after_exit(j) if self.owns_callback(j, p, t) else None)

    def after_exit(self, job):
        if job['process'] is None or job.get('cleanup') is not None:
            return
        try:
            if job.get('guard') is not None and job['guard'].active_count():
                self.terminate_job(job, '異常退出', 'Parent exited but contained descendants remain')
                return
        except Exception as exc:
            self.terminate_job(job, '異常退出', f'Unable to verify descendants: {exc}')
            return
        self.finalize_job(job)

    def finalize_job(self, job):
        if job['process'] is None or job.get('finalized'):
            return
        job['done'], errors = validate_outputs(
            job['outputs'], job.get('baseline') if self.options.force else None)
        if job.get('stop_reason'):
            job['status'] = job['stop_reason']
        elif job.get('exit_code') != 0 or job.get('exit_status') == QProcess.ExitStatus.CrashExit:
            job['status'] = '異常退出' if not errors else '失敗'
        else:
            job['status'] = '失敗' if errors else '已完成'
        job['error'] = '; '.join(errors[:10]) or None
        # Availability and overwrite progress are distinct: unchanged valid old
        # pages can remain available without satisfying P0's current-run proof.
        job['available'] = job['done']
        if self.options.force:
            job['available'], _ = validate_outputs(job['outputs'])
        job['finalized'] = True
        self.job_message(job, f"Result={job['status']}; exit={job.get('exit_code')}; "
                         f"validated={job['done']}/{job['total']}; {job['error'] or ''}")
        job['end'] = time.monotonic()
        if job['file']:
            job['file'].close()
        job['file'] = None
        if job.get('guard') is not None:
            job['guard'].close()
            job['guard'] = None
        job['process'].deleteLater()
        job['process'] = None
        token = self.run_generation
        QTimer.singleShot(0, lambda: self.schedule() if token == self.run_generation else None)
        self.sync_ui()

    def terminate_job(self, job, status, detail=''):
        process = job['process']
        if process is None or job.get('cleanup') is not None:
            return
        job['stop_reason'] = status
        job['status'] = '清理中'
        self.job_message(job, f'{status} requested. {detail}')
        cleaner = ProcessCleanup(process, job.get('guard'), self)
        job['cleanup'] = cleaner
        self.cleaners.append(cleaner)
        def cleaned(ok, message):
            self.job_message(job, message)
            job['cleanup'] = None
            self.cleaners.remove(cleaner)
            cleaner.deleteLater()
            if ok:
                self.finalize_job(job)
            else:
                # Retain process/job handles and block rerun/close: not confirmed stopped.
                job['status'] = '清理失敗'
                job['error'] = message
                self.sync_ui()
        cleaner.done.connect(cleaned)
        cleaner.start()
        self.sync_ui()

    def stop(self):
        if not self.has_owned_processes() and self.lifecycle not in (Lifecycle.RUNNING, Lifecycle.STOPPING):
            return
        self.transition(Lifecycle.STOPPING)
        self.batch_stopped = True
        for job in self.jobs:
            if job['process'] is not None:
                self.terminate_job(job, '已停止')
            elif job['status'] == '待處理':
                job['status'] = '已停止'
        self.sync_ui()

    def complete_batch(self):
        if (self.ended is not None or not self.started or not self.run_jobs
                or self.lifecycle not in (Lifecycle.RUNNING, Lifecycle.STOPPING)
                or self.has_owned_processes() or any(j['status'] == '待處理' for j in self.run_jobs)):
            return
        self.ended = time.monotonic()
        metrics = self.batch_metrics()
        completed, failed = metrics['completed'], metrics['failed']
        if failed:
            title, color, background = '批次結束，部分工作失敗', '#ffacb4', '#43232d'
        elif self.batch_stopped:
            title, color, background = '工作已停止', '#ffd08c', '#423321'
        else:
            title, color, background = '全部翻譯完成', '#9aefbc', '#173c2b'
        self.completion_text = (f'{title}   |   {completed} / {metrics["books"]} 本完成'
                                f'   |   {failed} 本失敗   |   耗時 {batch.elapsed_text(self.started, self.ended)}')
        self.stats.setStyleSheet(f'background:{background};color:{color};border:1px solid {color};'
                                'border-radius:8px;padding:12px;font-size:16px;font-weight:600;')
        self.stats.setText(self.completion_text)
        self.transition(Lifecycle.FINISHED)
        if self.notify and not self.batch_stopped:
            self.notify_completion(title, failed)
        if self.queued_check is not None:
            self.check_delay.start()

    def notify_completion(self, title, failed):
        QApplication.beep()
        QApplication.alert(self, 5000)
        self.completion_box = QMessageBox(self)
        self.completion_box.setWindowTitle(title)
        self.completion_box.setIcon(QMessageBox.Icon.Warning if failed else QMessageBox.Icon.Information)
        self.completion_box.setText(self.completion_text)
        self.completion_box.setStandardButtons(QMessageBox.StandardButton.Ok)
        self.completion_box.show()

    def batch_metrics(self):
        jobs = self.run_jobs
        failed = {'失敗', '異常退出', '清理失敗', '停滯逾時', '總時間逾時'}
        progress = sum(min(j['done'], j['total']) for j in jobs)
        before = sum(j['initial_valid'] for j in jobs)
        return {
            'books': len(jobs), 'completed': sum(j['status'] == '已完成' for j in jobs),
            'failed': sum(j['status'] in failed for j in jobs),
            'running': sum(j['process'] is not None for j in jobs),
            'stopped': sum(j['status'] == '已停止' for j in jobs),
            'pages_total': sum(j['total'] for j in jobs), 'pages_done': progress,
            'pages_before': before, 'pages_available': sum(j['available'] for j in jobs),
            'pages_added': sum(max(0, j['done'] - j['initial_valid']) for j in jobs) if not self.options.force else 0,
            'pages_updated': progress if self.options.force else 0,
            'elapsed': batch.elapsed_text(self.started, self.ended) if self.started else '--:--',
        }

    def tick(self):
        now = time.monotonic()
        for row, job in enumerate(self.jobs):
            if job['process'] is not None and job['status'] == '執行中':
                snapshot = output_snapshot(job['outputs'])
                if snapshot != job.get('snapshot'):
                    job['last'] = now
                job['snapshot'] = snapshot
                job['available'] = len(snapshot)
                done = (sum(signature != job['baseline'].get(path) for path, signature in snapshot.items())
                        if self.options.force else len(snapshot))
                job['done'] = done
                if self.advanced['timeout'] and now - job['start'] >= self.advanced['timeout']:
                    self.terminate_job(job, '總時間逾時')
                elif self.stall.value() and now - job['last'] >= self.stall.value():
                    self.terminate_job(job, '停滯逾時')
            self.table.item(row, 1).setText(job['status'])
            color = {'執行中':'#80bbff', '已完成':'#78dba2', '失敗':'#ff8e97',
                     '異常退出':'#ff8e97', '清理失敗':'#ff8e97', '清理中':'#ffc17b',
                     '停滯逾時':'#ffc17b', '總時間逾時':'#ffc17b'}.get(job['status'], '#aab6c8')
            self.table.item(row, 1).setForeground(QColor(color))
            bar = self.table.cellWidget(row, 2)
            bar.setValue(min(job['done'], job['total']))
            bar.setFormat(f'{job["done"]} / {job["total"]}')
            self.table.item(row, 3).setText(batch.elapsed_text(job['start'], job['end']))
        self.complete_batch()
        self.sync_ui()
        if self.run_jobs and self.started:
            metrics = self.batch_metrics()
            done, pages, total = metrics['completed'], metrics['pages_done'], metrics['pages_total']
            self.overall.setRange(0, total)
            self.overall.setValue(pages)
            self.overall.setFormat(f'{pages} / {total} 頁')
            self.stats.setText(f'{self.lifecycle.name}    '
                               f'{done} / {metrics["books"]} 本完成     {metrics["running"]} 本執行中     '
                               f'{pages} / {total} 頁     {metrics["elapsed"]}')
            self.overall.setToolTip(f'本次選取 {metrics["books"]} 本；既有有效頁 {metrics["pages_before"]}；'
                                    f'新增頁 {metrics["pages_added"]}；覆寫更新頁 {metrics["pages_updated"]}；'
                                    f'目前輸出頁 {metrics["pages_available"]}（執行中為檔案進展，結束時驗證）')
            if self.ended is not None:
                self.stats.setText(self.completion_text)
        self.show_log()

    def show_log(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self.jobs):
            return
        job = self.jobs[row]
        text = (str(job['path']) + '\n\n' if job['path'] else '') + job['tail']
        if self.log.toPlainText() != text:
            scrollbar = self.log.verticalScrollBar()
            follow = self.log_follow and scrollbar.value() >= scrollbar.maximum() - 3
            previous = scrollbar.value()
            self.log.setPlainText(text)
            scrollbar.setValue(scrollbar.maximum() if follow else previous)

    def open_log(self):
        row = self.table.currentRow()
        if 0 <= row < len(self.jobs) and self.jobs[row]['path']:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.jobs[row]['path'])))

    def open_result(self):
        row = self.table.currentRow()
        if 0 <= row < len(self.jobs):
            path = self.jobs[row]['book'] / 'result'
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path if path.exists() else path.parent)))

    def mousePressEvent(self, event):
        # Include the narrow outer edge above/beside the custom titlebar.
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() <= self.titlebar.geometry().bottom():
            if self.windowHandle():
                self.windowHandle().startSystemMove()
                return
        super().mousePressEvent(event)

    def closeEvent(self, event):
        if self.has_owned_processes():
            QMessageBox.information(self, '工作尚未結束', '請先停止工作，等待子程序結束後再關閉視窗。')
            event.ignore()
            return
        self.closing = True
        self.check_delay.stop()
        self.cancel_pending_start()
        self.backend_generation += 1
        self.queued_check = None
        self.sync_ui()
        if self.backend_worker is not None:
            event.ignore()  # Finish asynchronously; never destroy a running QThread.
            return
        self.timer.stop()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont('Microsoft JhengHei UI', 10))
    app.setStyleSheet(STYLE)
    panel = Panel()
    if len(sys.argv) > 1:
        panel.root.setText(sys.argv[1])
    if panel.root.text().strip() and (len(sys.argv) > 1 or panel.auto_scan):
        QTimer.singleShot(0, panel.scan)
    panel.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
