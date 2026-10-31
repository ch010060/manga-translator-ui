"""Windows Qt presentation tests; generated books and controlled worker states."""
import time
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QPoint, QPointF, QRect, Qt, QEvent, QProcess
from PyQt6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPixmap, QWindow
from PyQt6.QtWidgets import QApplication, QPushButton, QStyleOptionViewItem, QProgressBar
from PyQt6.QtTest import QTest

import test_batch_panel_p1 as fixtures

panel = fixtures.panel


class V2Tests(unittest.TestCase):
    setUpClass = classmethod(fixtures.P1Tests.setUpClass.__func__)
    beat = fixtures.P1Tests.beat
    book = fixtures.P1Tests.book
    existing = fixtures.P1Tests.existing
    pump = fixtures.P1Tests.pump
    ready_library = fixtures.P1Tests.ready_library

    def setUp(self):
        fixtures.P1Tests.setUp(self)
        self.visual_processes = []
        panel.apply_theme(self.app)
        self.app.setFont(QFont('Microsoft JhengHei UI', 10))

    def release_visual_processes(self):
        for job in self.window.jobs:
            if job.get('process') in self.visual_processes:
                job['process'] = None
        for process in self.visual_processes:
            process.deleteLater()
        self.visual_processes.clear()

    def tearDown(self):
        self.release_visual_processes()
        self.window.run_jobs = ()
        self.window.started = 0
        fixtures.P1Tests.tearDown(self)

    def seed(self, states=('執行中', '待處理'), done=(1, 0), force=False):
        """Inert QProcesses are visual fixtures only; actual P0/P1 worker tests are separate."""
        self.release_visual_processes()
        if self.window.lifecycle in (panel.Lifecycle.RUNNING, panel.Lifecycle.STOPPING):
            self.window.transition(panel.Lifecycle.FINISHED)
        if self.window.lifecycle != panel.Lifecycle.IDLE:
            self.window.transition(panel.Lifecycle.IDLE)
        self.window.reset_summary()
        self.window.options = panel.batch.BatchOptions(force=force)
        self.window.run_generation += 1
        for job in self.window.jobs:
            self.window.reset_job(job, job['outputs'], True)
        self.window.run_jobs = tuple(self.window.jobs)
        self.window.started = time.monotonic()-154
        for job, state, count in zip(self.window.jobs, states, done):
            job.update(status=state, done=count, start=self.window.started, end=None, available=count)
            if state in ('執行中', '清理中', '清理失敗'):
                process = QProcess(self.window)
                self.visual_processes.append(process)
                job['process'] = process
            elif state != '待處理':
                job['end'] = self.window.started+80
        self.window.transition(panel.Lifecycle.RUNNING)
        for row, job in enumerate(self.window.jobs):
            self.window.render_job_row(row, job)
        self.window.update_summary()

    def show(self, width=1220, height=840):
        self.window.show()
        self.window.resize(width, height)
        self.app.processEvents()

    def test_V01_ready_counts_and_row_height_preserved(self):
        self.ready_library(2)
        self.assertEqual(self.window.stats.state_label.text(), 'READY')
        self.assertIn('2 本', self.window.stats.detail.text())
        self.assertIn('4 頁', self.window.stats.detail.text())
        self.assertEqual(self.window.table.rowHeight(0), 32)
        self.assertEqual(self.window.table.columnCount(), 4)

    def test_V02_running_uses_batch_metrics(self):
        self.ready_library(2); self.seed(('已完成', '執行中'), (2, 1))
        metrics = self.window.batch_metrics()
        self.assertIn(f'{metrics["completed"]} / {metrics["books"]} 本', self.window.stats.numbers.text())
        self.assertIn('3 / 4 頁', self.window.stats.numbers.text())
        self.assertEqual(self.window.stats.state_label.text(), '執行中')

    def test_V03_terminal_and_cleanup_semantics(self):
        self.ready_library(2)
        for states, done, stopped, label, tone in (
            (('已完成', '已完成'), (2, 2), False, '全部完成', 'success'),
            (('已完成', '失敗'), (2, 1), False, '部分失敗', 'error'),
            (('已停止', '已停止'), (1, 0), True, '已停止', 'warning'),
        ):
            self.seed(states, done)
            self.window.batch_stopped = stopped
            self.window.complete_batch()
            self.assertEqual(self.window.stats.state_label.text(), label)
            self.assertEqual(self.window.stats.property('tone'), tone)
            self.assertEqual(self.window.overall.property('tone'), tone)
        self.seed(('清理失敗', '已停止'), (1, 0))
        self.window.transition(panel.Lifecycle.STOPPING)
        self.assertEqual(self.window.stats.state_label.text(), '清理未確認')
        self.assertIsNone(self.window.ended)

    def test_V04_long_titles_elide_and_tooltip(self):
        title = '01_図書館で見つけた小さな物語と長い題名のサンプル書籍'*3
        self.book(title); self.window.scan(); self.show(980, 600)
        item = self.window.table.item(0, 0)
        self.assertIn(title, item.toolTip())
        self.assertEqual(self.window.table.textElideMode(), Qt.TextElideMode.ElideRight)
        self.assertFalse(self.window.table.wordWrap())
        self.assertGreater(self.window.table.fontMetrics().horizontalAdvance(title), self.window.table.columnWidth(0))
        self.assertEqual(self.window.table.rowHeight(0), 32)

    def paint_progress(self, done, total, status):
        item = self.window.table.item(0, 2)
        item.setData(panel.PROGRESS_ROLE, (done, total, status))
        pixmap = QPixmap(240, 32)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 240, 32)
        option.widget = self.window.table
        self.window.table.itemDelegateForColumn(2).paint(painter, option, self.window.table.model().index(0, 2))
        painter.end()
        return pixmap.toImage()

    def test_V05_progress_zero_partial_full_and_status_color(self):
        self.ready_library()
        zero = self.paint_progress(0, 2, '執行中')
        partial = self.paint_progress(1, 2, '執行中')
        active_full = self.paint_progress(2, 2, '執行中')
        success = self.paint_progress(2, 2, '已完成')
        self.assertNotEqual(zero.pixelColor(40, 6), partial.pixelColor(40, 6))
        self.assertEqual(active_full.pixelColor(40, 6), QColor(panel.THEME['info']))
        self.assertEqual(success.pixelColor(40, 6), QColor(panel.THEME['success']))

    def test_V06_overwrite_progress_uses_P0_data(self):
        book = self.book(); self.existing(book); self.window.scan()
        self.window.options = panel.batch.BatchOptions(force=True)
        job = self.window.jobs[0]
        self.window.reset_job(job, job['outputs'], True)
        self.window.render_job_row(0, job)
        self.assertEqual(self.window.table.item(0, 2).data(panel.PROGRESS_ROLE)[:2], (0, 2))
        self.assertEqual(job['initial_valid'], 2)

    def test_V07_filter_and_row_selection_do_not_change_checkboxes(self):
        self.ready_library(2)
        self.window.table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
        self.window.search.setText('book1')
        self.window.table.selectRow(1)
        self.assertTrue(self.window.table.isRowHidden(0))
        self.assertEqual(self.window.table.item(0, 0).checkState(), Qt.CheckState.Checked)
        self.assertEqual(self.window.table.item(1, 0).checkState(), Qt.CheckState.Unchecked)

    def test_V08_many_books_refresh_preserves_scroll(self):
        self.ready_library(100); self.show(980, 600)
        scrollbar = self.window.table.verticalScrollBar()
        scrollbar.setValue(40)
        before = scrollbar.value()
        for unused in range(3): self.window.tick()
        self.assertEqual(scrollbar.value(), before)
        self.assertGreater(scrollbar.maximum(), 0)

    def test_V09_log_expand_collapse_retains_splitter(self):
        self.ready_library(2); self.show()
        self.assertFalse(self.window.log.isVisible())
        self.window.jobs[0]['tail'] = 'controlled log\n'*20
        self.window.log_toggle.setChecked(True); self.app.processEvents()
        self.assertTrue(self.window.log.isVisible())
        self.assertGreater(self.window.content_splitter.sizes()[1], 0)
        self.assertIn('controlled log', self.window.log.toPlainText())
        self.window.log_toggle.setChecked(False); self.app.processEvents()
        self.assertEqual(self.window.content_splitter.sizes()[1], 0)

    def test_V10_minimum_window_actions_accessible(self):
        self.ready_library(2); self.show(980, 600)
        expand = next(b for b in self.window.findChildren(QPushButton) if b.text() == '展開常用參數')
        for common, log in ((False, False), (True, False), (False, True), (True, True)):
            expand.setChecked(common); self.window.log_toggle.setChecked(log)
            self.window.resize(980, 600); self.app.processEvents()
            self.assertEqual((self.window.width(), self.window.height()), (980, 600))
            for button in self.window.findChildren(QPushButton):
                if button.isVisibleTo(self.window):
                    rect = QRect(button.mapTo(self.window, button.rect().topLeft()), button.size())
                    self.assertTrue(self.window.rect().contains(rect), (common, log, button.text(), rect))

    def test_V11_DPI_icons_and_visible_checkmark(self):
        for scale in (1.0, 1.25, 1.5):
            icon = panel.action_icon('settings')
            pixmap = icon.pixmap(panel.QSize(18, 18), scale)
            self.assertFalse(pixmap.isNull())
            self.assertGreaterEqual(pixmap.width(), 18)
        self.ready_library()
        rendered = []
        for state in (panel.QStyle.StateFlag.State_On, panel.QStyle.StateFlag.State_Off):
            pixmap = QPixmap(24, 24)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            option = panel.QStyleOption()
            option.rect = QRect(0, 0, 24, 24)
            option.state = panel.QStyle.StateFlag.State_Enabled | state
            self.app.style().drawPrimitive(panel.QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck,
                                           option, painter, self.window.table)
            painter.end()
            rendered.append(pixmap.toImage())
        self.assertNotEqual(rendered[0], rendered[1])
        self.assertEqual(self.window.table.item(0, 0).checkState(), Qt.CheckState.Checked)

    def test_V12_lifecycle_guards_remain_authoritative(self):
        self.ready_library(); self.window.pending_start = True
        self.window.transition(panel.Lifecycle.PREFLIGHT)
        self.window.update_summary()
        self.assertEqual(self.window.stats.state_label.text(), '檢查中')
        for button in (self.window.start_button, self.window.clear_button, self.window.scan_button):
            self.assertFalse(button.isEnabled())
        self.window.cancel_pending_start()

    def test_V13_progress_updates_do_not_recreate_widgets_or_decode(self):
        self.ready_library(20)
        delegate = self.window.table.itemDelegateForColumn(2)
        item_ids = [id(self.window.table.item(i, 2)) for i in range(20)]
        with patch('PIL.Image.open', side_effect=AssertionError('decode during rendering')):
            for unused in range(5): self.window.tick()
        self.assertIs(delegate, self.window.table.itemDelegateForColumn(2))
        self.assertEqual(item_ids, [id(self.window.table.item(i, 2)) for i in range(20)])
        self.assertTrue(all(self.window.table.cellWidget(i, 2) is None for i in range(20)))
        self.assertEqual(len(self.window.findChildren(QProgressBar)), 1)

    def test_V14_backend_status_and_retry_presentation(self):
        self.window.display_backend_result(True, 'reachable; not inference proof', 'long-model-name'*10)
        self.assertFalse(self.window.retry_backend.isVisibleTo(self.window))
        self.assertIn('long-model-name', self.window.backend_status.toolTip())
        self.window.display_backend_result(False, 'controlled failure', '')
        self.assertTrue(self.window.retry_backend.isVisibleTo(self.window))
        self.assertIn('controlled failure', self.window.backend_status.toolTip())

    def test_V15_window_controls_and_eight_resize_hit_regions(self):
        self.ready_library(); self.show()
        self.window.showNormal(); self.pump(lambda: not self.window.isMaximized())
        self.assertEqual(self.app.platformName(), 'windows', 'V15 requires the visible Windows Qt platform')
        self.assertTrue(self.window.isVisible())
        self.assertFalse(self.window.isFullScreen())
        self.window.resize(1100, 720)
        self.window.move(self.window.screen().availableGeometry().topLeft()+QPoint(80, 80))
        self.app.processEvents()
        normal_geometry = QRect(self.window.geometry())
        handle = self.window.windowHandle()
        width, height = self.window.width(), self.window.height()
        positions = ((1, 1), (width//2, 1), (width-2, 1), (1, height//2),
                     (width-2, height//2), (1, height-2), (width//2, height-2), (width-2, height-2))
        with patch.object(handle, 'startSystemResize', return_value=True) as resize:
            for x, y in positions:
                local = QPointF(x, y)
                global_point = QPointF(self.window.mapToGlobal(local.toPoint()))
                event = QMouseEvent(QEvent.Type.MouseButtonPress, local, global_point,
                                    Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
                self.assertTrue(self.window.eventFilter(self.window, event))
            self.assertEqual(resize.call_count, 8)
        green = next(b for b in self.window.titlebar.findChildren(QPushButton)
                     if b.accessibleName() == '最大化／還原')
        QTest.mouseClick(green, Qt.MouseButton.LeftButton)
        self.pump(lambda: self.window.isMaximized()
                  and self.window.geometry() == self.window.screen().availableGeometry())
        self.assertTrue(self.window.isMaximized())
        self.assertFalse(self.window.isFullScreen())
        self.assertEqual(handle.windowState(), Qt.WindowState.WindowMaximized)
        self.assertEqual(handle.visibility(), QWindow.Visibility.Maximized)
        # A late native event must not undo the reconciled QWidget state.
        QTest.qWait(200)
        self.assertTrue(self.window.isMaximized())
        self.assertEqual(self.window.frameGeometry(), self.window.screen().availableGeometry())
        with patch.object(handle, 'startSystemResize', return_value=True) as resize:
            local = QPointF(1, 1)
            event = QMouseEvent(QEvent.Type.MouseButtonPress, local,
                               QPointF(self.window.mapToGlobal(local.toPoint())),
                               Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                               Qt.KeyboardModifier.NoModifier)
            self.window.eventFilter(self.window, event)
            resize.assert_not_called()
        QTest.mouseClick(green, Qt.MouseButton.LeftButton)
        self.pump(lambda: not self.window.isMaximized() and self.window.geometry() == normal_geometry)
        self.assertFalse(self.window.isMaximized())
        self.assertEqual(self.window.geometry(), normal_geometry)
        self.assertEqual(handle.windowState(), Qt.WindowState.WindowNoState)
        # The caption forwards input to the title bar; use a clear central point.
        point = self.window.titlebar.rect().center()
        QTest.mouseDClick(self.window.titlebar, Qt.MouseButton.LeftButton, pos=point)
        self.pump(lambda: self.window.isMaximized())
        self.assertEqual(self.window.geometry(), self.window.screen().availableGeometry())
        QTest.mouseDClick(self.window.titlebar, Qt.MouseButton.LeftButton, pos=point)
        self.pump(lambda: not self.window.isMaximized() and self.window.geometry() == normal_geometry)
        self.assertTrue(self.window.windowFlags() & Qt.WindowType.FramelessWindowHint)


if __name__ == '__main__':
    unittest.main(verbosity=2)
