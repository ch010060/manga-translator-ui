"""P1 lifecycle regressions; fake checks, temporary libraries, real contained workers."""
import json
import os
import time
import unittest
from io import BytesIO
from unittest.mock import Mock, patch

from PyQt6.QtCore import QObject, pyqtSignal, Qt

import test_batch_panel_p0 as fixtures

panel = fixtures.panel


class ControlledCheck(QObject):
    result = pyqtSignal(bool, str, str)
    finished = pyqtSignal()

    def __init__(self, request, parent=None):
        super().__init__(parent)
        self.request = request

    def start(self):
        self.started.append(self)
        self.active.append(self)
        self.maximum[0] = max(self.maximum[0], len(self.active))

    def answer(self, ok=True, message='controlled reply', models='test-model'):
        self.result.emit(ok, message, models)
        self.active.remove(self)
        self.finished.emit()


class P1Tests(unittest.TestCase):
    setUpClass = classmethod(fixtures.P0Tests.setUpClass.__func__)
    book = fixtures.P0Tests.book
    existing = fixtures.P0Tests.existing
    pump = fixtures.P0Tests.pump
    settled = fixtures.P0Tests.settled
    launch = fixtures.P0Tests.launch
    beat = fixtures.P0Tests.beat

    def setUp(self):
        fixtures.P0Tests.setUp(self)
        # Reuse only the isolated worker/library fixtures, not their preflight bypass.
        self.patches[0].stop()
        self.window.check_delay.stop()
        self.window.check_delay.timeout.disconnect()
        self.window.check_delay.timeout.connect(self.window.check_backend)
        self.window.queued_check = None
        self.endpoint = 'http://controlled.invalid/v1'
        ControlledCheck.started, ControlledCheck.active, ControlledCheck.maximum = [], [], [0]
        self.extra_patches = [
            patch.object(panel, 'BackendCheck', ControlledCheck),
            patch.object(panel.batch, 'resolve_sakura_api_base', side_effect=lambda: self.endpoint),
            patch.object(panel.QMessageBox, 'information'), patch.object(panel.QMessageBox, 'warning'),
        ]
        for item in self.extra_patches:
            item.start()
        self.window.notify_completion = Mock()
        self.window.complete_batch = Mock(wraps=self.window.complete_batch)

    def tearDown(self):
        self.window.check_delay.stop()
        self.window.cancel_pending_start()
        self.window.queued_check = None
        self.window.backend_generation += 1
        while ControlledCheck.active:
            ControlledCheck.active[0].answer(False)
        try:
            fixtures.P0Tests.tearDown(self)
        finally:
            for item in reversed(self.extra_patches):
                item.stop()

    def ready_library(self, count=1):
        books = [self.book(f'book{i}') for i in range(count)]
        self.window.scan()
        return books

    def current_check(self):
        return ControlledCheck.active[0]

    def notifications(self):
        return self.window.notify_completion.call_count

    def test_B01_normal_preflight_starts_once(self):
        self.ready_library(); self.window.start()
        self.assertEqual(self.window.lifecycle, panel.Lifecycle.PREFLIGHT)
        self.current_check().answer(); self.settled()
        self.assertEqual(self.window.run_generation, 1)
        self.assertEqual(self.window.lifecycle, panel.Lifecycle.FINISHED)

    def test_B02_preflight_failure_has_no_completion(self):
        self.ready_library(); self.window.notify = True
        self.window.start(); self.current_check().answer(False)
        self.assertEqual(self.window.lifecycle, panel.Lifecycle.IDLE)
        self.assertFalse(self.window.has_owned_processes()); self.assertEqual(self.window.run_generation, 0)
        self.assertIsNone(self.window.ended); self.assertEqual(self.notifications(), 0)
        self.assertTrue(self.window.retry_backend.isVisibleTo(self.window))

    def test_B03_start_attaches_existing_check(self):
        self.ready_library(); self.window.check_backend()
        worker = self.current_check(); self.window.start()
        self.assertEqual(len(ControlledCheck.started), 1)
        self.assertTrue(self.window.pending_start)
        worker.answer(); self.settled(); self.assertEqual(self.window.run_generation, 1)

    def test_B03_new_explicit_start_preserved_for_queued_latest_config(self):
        self.ready_library(); self.window.start()
        config_b=self.root/'new.json'; config_b.write_text('{}')
        self.window.config.setText(str(config_b)); self.window.start()
        self.assertTrue(self.window.pending_start)
        self.current_check().answer()
        self.assertTrue(self.window.pending_start)
        self.assertEqual(self.current_check().request.config,config_b)
        self.current_check().answer(); self.settled()
        self.assertEqual(self.window.run_generation,1); self.assertEqual(ControlledCheck.maximum[0],1)

    def test_B04_repeated_start_no_duplicate_batches(self):
        self.ready_library(); self.window.start()
        self.window.start(); self.window.start()
        self.assertEqual(len(ControlledCheck.started), 1)
        self.current_check().answer(); self.window.start(); self.settled()
        self.assertEqual(self.window.run_generation, 1)

    def test_B05_config_A_to_B_rejects_old_result(self):
        self.ready_library(); self.window.check_backend(); old = self.current_check()
        config_b = self.root/'config-b.json'; config_b.write_text('{"b":1}')
        self.window.config.setText(str(config_b))
        old.answer(True)
        self.assertFalse(self.window.backend_ok)
        self.assertEqual(self.current_check().request.config, config_b)
        self.current_check().answer(True, models='B')
        self.assertIn('B', self.window.backend_status.text())
        self.assertEqual(self.window.run_generation, 0)

    def test_B06_out_of_order_stale_reply_ignored(self):
        self.ready_library(); self.window.check_backend(); old = self.current_check().request
        config_b = self.root/'b.json'; config_b.write_text('{}')
        self.window.config.setText(str(config_b)); self.current_check().answer()
        latest = self.current_check(); latest.answer(True, models='latest')
        self.window.backend_result(old, False, 'stale failure', 'old')
        self.assertTrue(self.window.backend_ok); self.assertIn('latest', self.window.backend_status.text())
        self.assertEqual(ControlledCheck.maximum[0], 1)

    def test_B06_A_B_A_same_identity_old_generation_rejected(self):
        self.ready_library(); self.window.start(); old = self.current_check().request
        config_b=self.root/'b.json'; config_b.write_text('{}')
        self.window.config.setText(str(config_b)); self.window.config.setText(str(self.config))
        self.current_check().answer()
        self.assertGreater(self.current_check().request.generation,old.generation)
        self.window.backend_result(old,True,'old A reply','old-A')
        self.assertFalse(self.window.backend_ok); self.assertEqual(self.window.run_generation,0)
        self.current_check().answer(True,models='fresh-A')
        self.assertIn('fresh-A',self.window.backend_status.text()); self.assertEqual(self.window.run_generation,0)

    def test_B07_config_change_cancels_pending_start(self):
        self.ready_library(); self.window.start()
        config_b = self.root/'b.json'; config_b.write_text('{}')
        self.window.config.setText(str(config_b))
        self.assertFalse(self.window.pending_start)
        self.current_check().answer(); self.current_check().answer()
        self.assertEqual(self.window.run_generation, 0)
        self.window.start(); self.current_check().answer(); self.settled()
        self.assertEqual(self.window.run_generation, 1)

    def test_B07_same_path_content_edit_cancels_start(self):
        self.ready_library(); self.window.start()
        self.config.write_text('{"changed":true}')
        self.current_check().answer()
        self.assertFalse(self.window.pending_start); self.assertFalse(self.window.backend_ok)
        self.current_check().answer()
        self.assertEqual(self.window.run_generation, 0)

    def test_B07_endpoint_change_cancels_start(self):
        self.ready_library(); self.window.start(); self.endpoint = 'http://new.invalid/v1'
        self.current_check().answer()
        self.assertFalse(self.window.pending_start)
        self.assertEqual(self.current_check().request.endpoint, self.endpoint)
        self.current_check().answer(); self.assertEqual(self.window.run_generation, 0)

    def test_B08_check_finishes_during_running_cannot_enable_start(self):
        self.book(); self.window.scan(); self.window.check_backend()
        job = self.launch('sleep')
        self.current_check().answer()
        self.assertEqual(self.window.lifecycle, panel.Lifecycle.RUNNING)
        self.assertFalse(self.window.start_button.isEnabled())
        self.window.stop(); self.settled()

    def test_B09_pending_start_guards_mutations(self):
        self.ready_library(); jobs = tuple(self.window.jobs); self.window.start()
        self.window.scan(); self.window.clear_tasks()
        with patch.object(panel.QFileDialog, 'getExistingDirectory') as browse, patch.object(
                panel.QFileDialog, 'getOpenFileName') as choose:
            self.window.choose_root(); self.window.choose_config()
            browse.assert_not_called(); choose.assert_not_called()
        self.window.batch_settings()
        self.assertEqual(tuple(self.window.jobs), jobs)
        self.assertFalse(self.window.scan_button.isEnabled()); self.assertFalse(self.window.clear_button.isEnabled())
        self.current_check().answer(); self.settled()

    def test_B10_close_waits_asynchronously_and_cancels_intent(self):
        self.ready_library(); self.window.show(); self.window.start()
        self.window.close()
        self.assertTrue(self.window.closing); self.assertFalse(self.window.pending_start)
        self.assertIsNotNone(self.window.backend_worker)
        self.current_check().answer()
        self.app.processEvents()
        self.assertIsNone(self.window.backend_worker)
        self.assertFalse(self.window.isVisible()); self.assertEqual(self.window.run_generation, 0)

    def test_B10_real_QThread_finishes_during_close(self):
        self.ready_library()
        self.config.write_text(json.dumps({'translator':{'translator':'sakura'}}))
        real_class=self.extra_patches[0].temp_original
        def delayed_reply(*args,**kwargs):
            time.sleep(.15)
            return BytesIO(b'{"data":[{"id":"thread-test"}]}')
        with patch.object(panel,'BackendCheck',real_class), patch.object(panel,'urlopen',side_effect=delayed_reply):
            self.window.show(); self.window.start()
            worker=self.window.backend_worker
            self.assertTrue(worker.isRunning()); before=self.heartbeats
            self.window.close()
            self.assertIs(self.window.backend_worker,worker)
            self.pump(lambda:self.window.backend_worker is None,3)
            self.app.processEvents()
        self.assertGreater(self.heartbeats,before)
        self.assertFalse(self.window.isVisible()); self.assertEqual(self.window.run_generation,0)

    def test_backend_missing_reply_is_explicit_failure(self):
        self.ready_library(); self.window.start(); worker=self.current_check()
        ControlledCheck.active.remove(worker); worker.finished.emit()
        self.assertFalse(self.window.backend_ok); self.assertEqual(self.window.run_generation,0)
        self.assertIn('未回傳結果',self.window.backend_status.toolTip())

    def test_backend_recovery_requires_explicit_start(self):
        self.ready_library(); self.window.start(); self.current_check().answer(False)
        self.window.check_backend(); self.current_check().answer(True)
        self.assertTrue(self.window.backend_ok); self.assertEqual(self.window.run_generation, 0)
        self.assertFalse(self.window.retry_backend.isVisibleTo(self.window))

    def test_backend_one_HTTP_call_and_request_snapshot(self):
        self.config.write_text(json.dumps({'translator': {'translator': 'sakura'}}))
        identity, config, content, endpoint = self.window.config_identity()
        request = panel.BackendRequest(42, identity, config, endpoint, content)
        # Obtain the real class hidden by the controlled factory patch.
        real_class = self.extra_patches[0].temp_original
        worker = real_class(request)
        results = []
        worker.result.connect(lambda *args: results.append(args))
        self.config.write_text('{}')  # Request uses captured bytes, not this new content.
        with patch.object(panel, 'urlopen', return_value=BytesIO(b'{"data":[{"id":"model-test"}]}')) as http:
            worker.run()
        self.assertEqual(http.call_count, 1)
        self.assertEqual(http.call_args.args[0].full_url, endpoint+'/models')
        self.assertEqual(results[0], (True, f'Sakura API endpoint reachable: {endpoint}/models', 'model-test'))

    def test_L01_normal_run_and_single_notification(self):
        self.book(); self.window.notify = True; self.launch(); self.settled()
        self.assertEqual(self.window.lifecycle, panel.Lifecycle.FINISHED)
        self.assertEqual(self.notifications(), 1)
        self.window.tick(); self.window.complete_batch(); self.assertEqual(self.notifications(), 1)

    def test_L02_rerun_fresh_log_timestamp_and_decoder(self):
        self.book(); job = self.launch(force=True); self.settled()
        path, timestamp, decoder = job['path'], job['start'], job['decoder']
        job['tail'] += 'STALE SENTINEL'; job['error'] = 'OLD ERROR'
        self.window.begin_translation()
        self.assertNotEqual(job['path'], path); self.assertGreater(job['start'], timestamp)
        self.assertIsNot(job['decoder'], decoder); self.assertNotIn('STALE SENTINEL',job['tail'])
        self.assertIsNone(job['error']); self.assertIsNone(job['end'])
        self.settled(); self.assertTrue(path.exists())

    def test_L03_failed_then_retry_clears_error(self):
        self.book(); job = self.launch('incomplete'); self.settled()
        self.assertIsNotNone(job['error']); oldpath = job['path']
        self.mode = 'normal'; self.window.begin_translation()
        self.assertIsNone(job['error']); self.assertNotEqual(job['path'], oldpath)
        self.settled(); self.assertEqual(job['status'], '已完成')

    def test_L04_stopped_then_resume_keeps_partial(self):
        book = self.book(); job = self.launch('sleep'); self.pump(lambda:'ready' in job['tail'])
        self.window.stop(); self.settled(); before = (book/'result/001.png').read_bytes()
        self.mode = 'normal'; self.window.begin_translation()
        self.assertIsNone(job['stop_reason']); self.settled()
        self.assertEqual(job['status'],'已完成'); self.assertEqual(before,(book/'result/001.png').read_bytes())

    @unittest.skipUnless(os.name=='nt','P0 Windows cleanup ownership')
    def test_L05_cleanup_failure_blocks_rerun_clear_scan_close(self):
        self.book(); job = self.launch('sleep'); self.pump(lambda:'ready' in job['tail'])
        with patch.object(job['guard'],'active_count',side_effect=OSError('test denied')):
            self.window.stop(); self.pump(lambda:job['status']=='清理失敗')
            generation = self.window.run_generation; process = job['process']
            self.window.begin_translation(); self.window.start(); self.window.scan(); self.window.clear_tasks()
            self.window.close()
            self.assertEqual(self.window.run_generation,generation); self.assertIs(job['process'],process)
            self.assertEqual(self.window.lifecycle,panel.Lifecycle.STOPPING)
            self.assertFalse(self.window.start_button.isEnabled()); self.assertTrue(self.window.stop_button.isEnabled())
        self.window.stop(); self.settled()

    def test_L06_selected_complete_skips_once(self):
        self.existing(self.book()); self.window.notify=True
        job=self.launch(); self.settled()
        self.assertIsNone(job['start']); self.assertIsNone(job['path'])
        self.assertEqual(self.notifications(),1); self.assertEqual(self.window.batch_metrics()['completed'],1)

    def test_L07_filter_does_not_change_run_selection(self):
        self.ready_library(2); self.window.table.item(1,0).setCheckState(Qt.CheckState.Unchecked)
        self.window.search.setText('book1'); self.window.begin_translation(); self.settled()
        self.assertEqual(len(self.window.run_jobs),1)
        self.assertEqual(self.window.run_jobs[0]['book'].name,'book0')
        self.assertEqual(self.window.table.item(0,0).checkState(),Qt.CheckState.Checked)

    def test_L08_duplicate_and_old_callbacks_do_not_refinalize(self):
        self.book(); self.window.notify=True; job=self.launch(force=True)
        process, token=job['process'],job['run_id']; self.settled()
        self.window.finished(job,0,None,process,token); self.window.finalize_job(job)
        self.assertEqual(self.notifications(),1)
        self.window.begin_translation(); newprocess=job['process']
        self.window.finished(job,99,None,process,token)
        self.assertIs(job['process'],newprocess); self.assertIsNone(job['exit_code'])
        self.settled(); self.assertEqual(self.notifications(),2)

    def test_L09_scan_clears_completion(self):
        self.book(); self.launch(); self.settled(); self.window.scan()
        self.assertEqual(self.window.lifecycle,panel.Lifecycle.IDLE)
        self.assertIsNone(self.window.ended); self.assertEqual(self.window.run_jobs,())
        self.assertTrue(self.window.stats.text().startswith('READY')); self.assertEqual(self.window.overall.value(),0)

    def test_L10_stop_running_and_queued(self):
        self.book('one'); self.book('two'); self.window.concurrent.setValue(1)
        self.launch('sleep'); self.pump(lambda:'ready' in self.window.jobs[0]['tail'])
        self.window.stop()
        self.assertEqual(self.window.lifecycle,panel.Lifecycle.STOPPING)
        self.assertFalse(self.window.clear_button.isEnabled()); self.settled()
        self.assertTrue(all(job['status']=='已停止' for job in self.window.run_jobs))
        self.assertEqual(self.window.lifecycle,panel.Lifecycle.FINISHED)

    def test_S01_only_selected_books_in_denominator(self):
        self.ready_library(4)
        for row in (1,3): self.window.table.item(row,0).setCheckState(Qt.CheckState.Unchecked)
        self.window.begin_translation(); self.settled(); metrics=self.window.batch_metrics()
        self.assertEqual((metrics['books'],metrics['pages_total'],metrics['completed']),(2,4,2))

    def test_S02_unselected_complete_excluded(self):
        books=self.ready_library(2); self.existing(books[1]); self.window.scan()
        self.window.begin_translation(); self.settled(); metrics=self.window.batch_metrics()
        self.assertEqual((metrics['books'],metrics['pages_total'],metrics['pages_before']),(1,2,0))
        self.assertEqual(self.window.jobs[1]['status'],'未選取')

    def test_S03_selected_complete_counts_as_skipped_complete(self):
        self.existing(self.book()); self.launch(); self.settled()
        metrics=self.window.batch_metrics()
        self.assertEqual((metrics['books'],metrics['completed'],metrics['pages_before'],metrics['pages_added']),(1,1,2,0))

    def test_S04_partial_resume_distinguishes_existing_and_added(self):
        book=self.book(); self.existing(book,('001.png',))
        self.launch(); self.settled(); metrics=self.window.batch_metrics()
        self.assertEqual((metrics['pages_before'],metrics['pages_added'],metrics['pages_done'],metrics['pages_available']),(1,1,2,2))

    def test_S05_overwrite_initial_progress_not_historical(self):
        self.existing(self.book()); self.launch(force=True)
        metrics=self.window.batch_metrics()
        self.assertEqual((metrics['pages_before'],metrics['pages_done'],metrics['pages_updated']),(2,0,0))
        self.settled(); self.assertEqual(self.window.batch_metrics()['pages_updated'],2)

    def test_S06_one_failure_one_success(self):
        self.ready_library(2)
        self.window.notify=True
        panel.batch.build_local_command.side_effect=lambda py,book,options: (
            [fixtures.sys.executable,'-u',str(self.worker),str(book),
             'incomplete' if book.name=='book1' else 'normal',str(options.force)])
        self.window.begin_translation(); self.settled(); metrics=self.window.batch_metrics()
        self.assertEqual((metrics['completed'],metrics['failed']),(1,1))
        self.assertIn('部分工作失敗',self.window.completion_text)
        self.window.complete_batch(); self.assertEqual(self.notifications(),1)

    def test_S07_stop_not_success_notification(self):
        self.book(); self.window.notify=True; job=self.launch('sleep')
        self.pump(lambda:'ready' in job['tail']); self.window.stop(); self.settled()
        self.assertEqual(self.window.batch_metrics()['stopped'],1)
        self.assertIn('工作已停止',self.window.completion_text); self.assertEqual(self.notifications(),0)

    def test_S08_elapsed_frozen_after_completion(self):
        self.book(); self.launch(); self.settled()
        ended=self.window.ended; summary=self.window.completion_text; metrics=self.window.batch_metrics()
        with patch.object(panel.time,'monotonic',return_value=ended+100):
            self.window.tick(); self.assertEqual(self.window.batch_metrics()['elapsed'],metrics['elapsed'])
        self.assertEqual(self.window.ended,ended); self.assertEqual(self.window.stats.text(),summary)

    def test_S09_retry_has_new_run_metrics(self):
        self.book(); self.launch('incomplete'); self.settled()
        self.assertEqual(self.window.batch_metrics()['failed'],1)
        self.mode='normal'; self.window.begin_translation()
        self.assertEqual(self.window.batch_metrics()['failed'],0)
        self.assertEqual(self.window.batch_metrics()['pages_before'],1)
        self.settled(); self.assertEqual(self.window.batch_metrics()['completed'],1)

    def test_S10_zero_selection_no_success(self):
        self.ready_library(); self.window.notify=True
        self.window.table.item(0,0).setCheckState(Qt.CheckState.Unchecked)
        self.window.start(); self.window.begin_translation(); self.window.tick()
        self.assertEqual(self.window.run_generation,0); self.assertEqual(self.notifications(),0)
        self.assertIsNone(self.window.ended); self.assertEqual(len(ControlledCheck.started),0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
