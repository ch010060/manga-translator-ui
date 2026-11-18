"""Transactional batch settings tests using real Qt modal dialogs and temp data."""
import sys
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import QApplication, QCheckBox, QPushButton, QSpinBox

import test_batch_panel_p1 as fixtures

panel = fixtures.panel


class P2Tests(unittest.TestCase):
    setUpClass = classmethod(fixtures.P1Tests.setUpClass.__func__)
    tearDown = fixtures.P1Tests.tearDown
    beat = fixtures.P1Tests.beat
    book = fixtures.P1Tests.book
    existing = fixtures.P1Tests.existing
    pump = fixtures.P1Tests.pump
    ready_library = fixtures.P1Tests.ready_library

    def setUp(self):
        fixtures.P1Tests.setUp(self)
        # The worker fixture disables stall detection for long-running P0 tests;
        # settings tests need the actual parser default rather than that override.
        self.window.stall.setValue(self.window.defaults['stall_timeout'])

    def different_values(self):
        self.window.concurrent.setValue(7)
        self.window.batch_size.setValue(9)
        self.window.stall.setValue(123)
        self.window.advanced.update(attempts=8,timeout=111,memory_limit=222,
                                    memory_percent=44,batch_per_restart=3,lane_books=2,
                                    intra_book_concurrent=True,use_subprocess=False)
        self.window.force.setChecked(True)

    def dialog_action(self, action, accept=True):
        errors=[]
        def interact():
            dialog=QApplication.activeModalWidget()
            try:
                self.assertIsNotNone(dialog)
                self.assertEqual(dialog.objectName(),'batch_settings_dialog')
                action(dialog)
                dialog.accept() if accept else dialog.reject()
            except BaseException as exc:
                errors.append(exc)
                if dialog is not None:
                    dialog.reject()
        QTimer.singleShot(0,interact)
        self.window.batch_settings()
        if errors:
            raise errors[0]

    def reset(self,dialog):
        dialog.findChild(QPushButton,'reset_batch_defaults').click()

    def test_R01_reset_all_batch_parameters(self):
        self.different_values()
        original=self.window.batch_settings_snapshot()
        def action(dialog):
            self.reset(dialog)
            self.assertEqual(self.window.batch_settings_snapshot(),original)
        self.dialog_action(action)
        self.assertEqual(self.window.batch_settings_snapshot(),self.window.default_batch_settings())

    def test_R02_config_path_and_contents_unchanged(self):
        config=self.root/'custom-config.json'; content=b'{"translator":{"translator":"sakura"}}'
        config.write_bytes(content); self.window.config.setText(str(config))
        self.window.check_delay.stop()
        self.different_values(); self.dialog_action(self.reset)
        self.assertEqual(self.window.config.text(),str(config)); self.assertEqual(config.read_bytes(),content)

    def test_R03_library_outputs_logs_preferences_unchanged(self):
        book=self.book(); self.existing(book)
        log=self.root/'existing.log'; log.write_text('preserve log',encoding='utf-8')
        before={str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        root=self.window.root.text()
        preferences=(self.window.follow,self.window.notify,self.window.auto_scan,self.window.log_follow)
        endpoint=self.window.config_identity()[3]
        self.different_values(); self.dialog_action(self.reset)
        self.assertEqual(root,self.window.root.text())
        self.assertEqual(endpoint,self.window.config_identity()[3])
        self.assertEqual(preferences,(self.window.follow,self.window.notify,self.window.auto_scan,self.window.log_follow))
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_R04_reset_cancel_leaves_all_active_values(self):
        self.different_values(); before=self.window.batch_settings_snapshot()
        with patch.object(self.window,'apply_batch_settings',wraps=self.window.apply_batch_settings) as apply:
            self.dialog_action(self.reset,False)
        self.assertEqual(before,self.window.batch_settings_snapshot()); apply.assert_not_called()

    def test_R05_edit_cancel_discards_every_change(self):
        before=self.window.batch_settings_snapshot()
        def edit(dialog):
            for widget in dialog.findChildren(QSpinBox): widget.setValue(widget.maximum())
            for widget in dialog.findChildren(QCheckBox): widget.toggle()
            self.assertEqual(before,self.window.batch_settings_snapshot())
        self.dialog_action(edit,False)
        self.assertEqual(before,self.window.batch_settings_snapshot())

    def test_R06_reset_manual_edit_OK_keeps_final_visible_draft(self):
        self.different_values()
        def edit(dialog):
            self.reset(dialog)
            dialog.findChild(QSpinBox,'attempts').setValue(-1)
            dialog.findChild(QSpinBox,'timeout').setValue(987)
            dialog.findChild(QCheckBox,'force').setChecked(True)
            dialog.findChild(QCheckBox,'use_subprocess').setChecked(False)
        self.dialog_action(edit)
        expected=self.window.default_batch_settings()
        expected.update(attempts=-1,timeout=987,force=True,use_subprocess=False)
        self.assertEqual(expected,self.window.batch_settings_snapshot())

    def test_R07_edit_OK_applies_once(self):
        before=self.window.batch_settings_snapshot()
        def edit(dialog):
            dialog.findChild(QSpinBox,'attempts').setValue(45)
            dialog.findChild(QCheckBox,'intra_book_concurrent').setChecked(True)
        with patch.object(self.window,'apply_batch_settings',wraps=self.window.apply_batch_settings) as apply:
            self.dialog_action(edit)
        self.assertEqual(apply.call_count,1)
        before.update(attempts=45,intra_book_concurrent=True)
        self.assertEqual(before,self.window.batch_settings_snapshot())

    def test_R08_reopen_shows_active_values_and_open_is_readonly(self):
        self.different_values()
        before=self.window.batch_settings_snapshot()
        def check(dialog):
            for key in self.window.advanced:
                widget=dialog.findChild(QCheckBox if isinstance(before[key],bool) else QSpinBox,key)
                self.assertEqual(widget.isChecked() if isinstance(widget,QCheckBox) else widget.value(),before[key])
            self.assertEqual(dialog.findChild(QCheckBox,'force').isChecked(),before['force'])
            self.assertEqual(before,self.window.batch_settings_snapshot())
        self.dialog_action(check,False); self.dialog_action(check,False)

    def test_R09_invalid_lifecycles_block_settings(self):
        before=self.window.batch_settings_snapshot()
        for state in (panel.Lifecycle.PREFLIGHT,panel.Lifecycle.RUNNING,panel.Lifecycle.STOPPING):
            self.window.transition(state)
            with patch.object(panel,'QDialog') as dialog:
                self.window.batch_settings(); dialog.assert_not_called()
            self.assertEqual(before,self.window.batch_settings_snapshot())
        self.window.transition(panel.Lifecycle.FINISHED)

    def test_R10_unverified_cleanup_handles_preserved(self):
        self.ready_library(); before=self.window.batch_settings_snapshot()
        job=self.window.jobs[0]; process=object(); guard=object()
        job.update(process=process,guard=guard,status='清理失敗')
        self.window.transition(panel.Lifecycle.RUNNING); self.window.transition(panel.Lifecycle.STOPPING)
        try:
            with patch.object(panel,'QDialog') as dialog:
                self.window.batch_settings(); dialog.assert_not_called()
            draft=self.window.default_batch_settings(); self.window.apply_batch_settings(draft)
            self.assertIs(job['process'],process); self.assertIs(job['guard'],guard)
            self.assertEqual(before,self.window.batch_settings_snapshot())
        finally:
            job.update(process=None,guard=None,status='已停止')
            self.window.transition(panel.Lifecycle.FINISHED)

    def test_R11_no_backend_side_effects_or_implicit_start(self):
        self.different_values()
        identity=self.window.config_identity(); generation=self.window.backend_generation
        queued=self.window.queued_check
        with patch.object(self.window,'new_check',wraps=self.window.new_check) as check, patch.object(
                self.window,'begin_translation',wraps=self.window.begin_translation) as begin:
            self.dialog_action(self.reset)
        self.assertEqual(identity,self.window.config_identity())
        self.assertEqual(generation,self.window.backend_generation)
        self.assertIs(queued,self.window.queued_check)
        self.assertFalse(self.window.check_delay.isActive())
        check.assert_not_called(); begin.assert_not_called()
        self.assertEqual(len(fixtures.ControlledCheck.started),0)
        # Reset did not bypass the explicit Start preflight contract.
        self.ready_library(); self.window.start()
        self.assertEqual(self.window.lifecycle,panel.Lifecycle.PREFLIGHT)
        self.assertEqual(len(fixtures.ControlledCheck.started),1)
        self.assertFalse(self.window.has_owned_processes())

    def test_R12_defaults_match_current_headless_parser(self):
        current=vars(panel.batch.parse_args(['--root','.']))
        expected={key:current[key] for key in (*panel.BATCH_RANGES,*panel.BATCH_FLAGS)}
        expected.update(panel.GUI_DEFAULTS)  # validated GUI-only defaults override the headless parser
        # The shared harness pins lane_books=0 for legacy suites, so compare the defaults themselves;
        # a fresh Panel's initial values are covered in test_batch_panel_defaults.py.
        self.assertEqual(self.window.default_batch_settings(),expected)
        self.different_values(); self.dialog_action(self.reset)
        self.assertEqual(self.window.batch_settings_snapshot(),expected)

    def test_R13_sentinels_and_safe_ranges(self):
        def check(dialog):
            for key in self.window.advanced:
                widget=dialog.findChild(QSpinBox,key)
                if widget:
                    self.assertEqual((widget.minimum(),widget.maximum()),panel.BATCH_RANGES[key])
                    widget.setValue(panel.BATCH_RANGES[key][0])
            self.assertEqual(dialog.findChild(QSpinBox,'attempts').value(),-1)
        self.dialog_action(check)
        self.assertEqual(self.window.advanced['attempts'],-1)
        parsed=panel.batch.parse_args(['--root','.','--attempts','-1'])
        self.assertEqual(parsed.attempts,-1)
        command=self.patches[-1].temp_original(
            sys.executable,self.root,panel.batch.BatchOptions(attempts=-1))
        self.assertEqual(command[command.index('--attempts')+1],'-1')
        for widget,key in ((self.window.concurrent,'concurrency'),(self.window.batch_size,'batch_size'),
                           (self.window.stall,'stall_timeout')):
            self.assertEqual((widget.minimum(),widget.maximum()),panel.BATCH_RANGES[key])
        self.assertEqual(self.window.advanced['memory_percent'],0)

    def test_R14_overwrite_reset_disabled(self):
        self.window.force.setChecked(True); self.dialog_action(self.reset)
        self.assertFalse(self.window.force.isChecked())

    def test_R15_reset_cancel_preserves_jobs_and_selection(self):
        self.ready_library(2)
        self.window.table.item(1,0).setCheckState(Qt.CheckState.Unchecked)
        before=[(id(job),job.copy()) for job in self.window.jobs]
        selection=[self.window.table.item(i,0).checkState() for i in range(2)]
        for accept in (False,True):
            self.dialog_action(self.reset,accept)
            self.assertEqual(before,[(id(job),job.copy()) for job in self.window.jobs])
            self.assertEqual(selection,[self.window.table.item(i,0).checkState() for i in range(2)])

    def test_commit_rechecks_lifecycle_after_nested_dialog(self):
        self.different_values(); before=self.window.batch_settings_snapshot()
        def action(dialog):
            self.reset(dialog)
            self.window.pending_start=True
            self.window.transition(panel.Lifecycle.PREFLIGHT)
        self.dialog_action(action)
        self.assertEqual(before,self.window.batch_settings_snapshot())
        self.window.cancel_pending_start()

    def test_initial_values_follow_parser_not_copied_constants(self):
        original=panel.batch.parse_args
        overrides=dict(concurrency=7,batch_size=9,stall_timeout=17,attempts=-1,timeout=23,
                       memory_limit=456,memory_percent=60,batch_per_restart=2,lane_books=3,
                       intra_book_concurrent=True,use_subprocess=False,force=True)
        def parser(argv):
            result=original(argv)
            for key,value in overrides.items(): setattr(result,key,value)
            return result
        with patch.object(panel.batch,'parse_args',side_effect=parser):
            other=panel.Panel()
            try:
                # Parser values flow through, except the validated GUI-only defaults which win.
                self.assertEqual({**overrides,**panel.GUI_DEFAULTS},other.batch_settings_snapshot())
            finally:
                other.close()


if __name__=='__main__':
    unittest.main(verbosity=2)
