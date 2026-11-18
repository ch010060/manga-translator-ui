"""P0 regression tests: real QProcess workers, temporary libraries, no ML imports.

Run: python -m unittest discover -s tests -p test_batch_panel_p0.py -v
"""
import ast
import ctypes
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from PyQt6.QtCore import QSettings, QTimer, Qt
from PyQt6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import batch_panel as panel
from batch_job_runtime import expected_outputs, output_snapshot, validate_outputs


WORKER = r'''
import json, os, subprocess, sys, time
from pathlib import Path
from PIL import Image
book=Path(sys.argv[1]); mode=sys.argv[2]; force=sys.argv[3]=='True'
result=book/'result'; result.mkdir(exist_ok=True)
def save(name):
    Image.new('RGB',(8,8),(23,45,67)).save(result/name)
if mode in ('tree','orphan'):
    child_code = "import subprocess,sys,time,json,os; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); Path(sys.argv[1]).write_text(json.dumps([os.getpid(),p.pid])); time.sleep(60)"
    p=subprocess.Popen([sys.executable,'-c',child_code,str(book/'descendants.json')])
    if mode=='orphan':
        while not (book/'descendants.json').exists(): time.sleep(.01)
        sys.exit(0)
if mode=='exit_on_marker':
    print('ready',flush=True)
    while not (book/'release').exists(): time.sleep(.01)
elif mode in ('sleep','tree'):
    save('001.png'); print('ready',flush=True); time.sleep(60)
elif mode=='incomplete':
    save('001.png')
elif mode=='mismatch':
    save('other.png'); save('unrelated.png')
elif mode=='truncated':
    (result/'001.png').write_bytes(b'\x89PNG\r\n\x1a\n')
    save('002.png')
else:
    if mode=='overwrite_wait':
        print('ready',flush=True); time.sleep(9)
    for source in sorted(book.glob('*.png')):
        if force or not (result/source.name).exists(): save(source.name)
    if mode=='complete_wait':
        print('ready',flush=True); time.sleep(9)
    if mode=='nonzero': sys.exit(7)
'''


def pid_alive(pid):
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    from ctypes import wintypes as w
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = (w.DWORD, w.BOOL, w.DWORD)
    api.OpenProcess.restype = w.HANDLE
    api.WaitForSingleObject.argtypes = (w.HANDLE, w.DWORD)
    api.CloseHandle.argtypes = (w.HANDLE,)
    handle = api.OpenProcess(0x100000, False, pid)
    if not handle:
        return False
    try:
        return api.WaitForSingleObject(handle, 0) == 258
    finally:
        api.CloseHandle(handle)


class P0Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.worker = self.root / 'fake_worker.py'
        self.worker.write_text(WORKER, encoding='utf-8')
        self.config = self.root / 'config.json'
        self.config.write_text('{}', encoding='utf-8')
        self.mode = 'normal'
        self.patches = [
            patch.object(panel.Panel, 'check_backend', lambda *a, **kw: None),
            patch.object(panel, 'QSettings', lambda *a: QSettings(str(self.root/'prefs.ini'), QSettings.Format.IniFormat)),
            patch.object(panel.batch, 'PROJECT_ROOT', self.root),
            patch.object(panel.batch, 'build_local_command', side_effect=lambda py, book, options:
                         [sys.executable, '-u', str(self.worker), str(book), self.mode, str(options.force)]),
        ]
        for item in self.patches:
            item.start()
        self.window = panel.Panel()
        self.window.timer.stop()
        self.window.notify = False
        self.window.config.setText(str(self.config))
        self.window.root.setText(str(self.root))
        self.window.stall.setValue(0)
        self.window.advanced['lane_books'] = 0  # these suites cover legacy one-process-per-book mode
        self.heartbeats = 0
        self.heartbeat = QTimer()
        self.heartbeat.timeout.connect(self.beat)
        self.heartbeat.start(10)

    def beat(self):
        self.heartbeats += 1

    def pump(self, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            self.window.tick()
            if predicate():
                return
            time.sleep(.01)
        self.fail('Timed out: ' + repr([(j['status'], j.get('tail','')[-1000:]) for j in self.window.jobs]))

    def tearDown(self):
        try:
            for job in self.window.jobs:
                if job.get('guard'):
                    job['guard'].terminate()
                if job['process'] is not None:
                    job['process'].kill()
            self.pump(lambda: not any(j['process'] is not None for j in self.window.jobs), 8)
            self.window.check_delay.stop()
            self.window.close()
            self.app.processEvents()
        finally:
            self.heartbeat.stop()
            for item in reversed(self.patches):
                item.stop()
            self.temp.cleanup()

    def book(self, name='book'):
        book = self.root/name
        book.mkdir()
        for name in ('001.png','002.png'):
            Image.new('RGB',(8,8),'white').save(book/name)
        return book

    def existing(self, book, names=('001.png','002.png')):
        (book/'result').mkdir(exist_ok=True)
        for name in names:
            Image.new('RGB',(8,8),'red').save(book/'result'/name)

    def launch(self, mode='normal', force=False):
        self.mode = mode
        self.window.scan()
        for row in range(len(self.window.jobs)):
            self.window.table.item(row,0).setCheckState(Qt.CheckState.Checked)
        self.window.force.setChecked(force)
        self.window.begin_translation()
        return self.window.jobs[0]

    def settled(self):
        self.pump(lambda: all(j['process'] is None for j in self.window.jobs))
        self.pump(lambda: not self.window.running)

    def test_C01_empty_normal(self):
        self.book(); job=self.launch(); self.settled()
        self.assertEqual(job['status'],'已完成')

    def test_C02_partial_resume_preserves_existing(self):
        book=self.book(); self.existing(book,('001.png',))
        before=(book/'result/001.png').read_bytes(); signature=output_snapshot((book/'result/001.png',))
        job=self.launch(); self.settled()
        self.assertEqual(job['status'],'已完成')
        self.assertEqual(before,(book/'result/001.png').read_bytes())
        self.assertEqual(signature,output_snapshot((book/'result/001.png',)))

    def test_C02_complete_no_overwrite_skips_worker(self):
        book=self.book(); self.existing(book)
        before=output_snapshot(expected_outputs(book))
        job=self.launch(); self.settled()
        self.assertEqual(job['status'],'已完成'); self.assertIsNone(job['start'])
        self.assertEqual(before,output_snapshot(expected_outputs(book)))

    def test_P01_stop_immediately_after_launch(self):
        self.book(); job=self.launch('sleep')
        self.window.stop(); self.settled()
        self.assertEqual(job['status'],'已停止')

    def test_timeout_is_verified_and_keeps_timeout_status(self):
        self.book(); job=self.launch('sleep'); self.pump(lambda:'ready' in job['tail'])
        job['start']=time.monotonic()-2
        self.window.advanced['timeout']=1
        self.window.tick(); self.settled()
        self.assertEqual(job['status'],'總時間逾時')
        self.assertIn('Verified direct process',job['tail'])

    def test_C03_overwrite_existing_does_not_finish_after_eight_seconds(self):
        book=self.book(); self.existing(book)
        job=self.launch('overwrite_wait',True)
        self.pump(lambda: 'ready' in job['tail'])
        self.assertEqual(job['done'],0)
        until=time.monotonic()+8.2
        self.pump(lambda: time.monotonic()>=until,10)
        self.assertEqual(job['status'],'執行中')
        self.settled(); self.assertEqual(job['status'],'已完成')

    def test_C04_mismatched_names(self):
        self.book(); job=self.launch('mismatch'); self.settled()
        self.assertEqual(job['status'],'失敗'); self.assertEqual(job['done'],0)

    def test_C05_extra_files_not_counted(self):
        book=self.book(); self.existing(book,('unrelated.png','another.png'))
        self.window.scan(); self.assertEqual(self.window.jobs[0]['done'],0)
        job=self.launch('incomplete'); self.settled(); self.assertEqual(job['status'],'失敗')

    def test_C06_zero_exit_incomplete(self):
        self.book(); job=self.launch('incomplete'); self.settled()
        self.assertEqual(job['status'],'失敗'); self.assertIn('002.png',job['error'])

    def test_C07_nonzero_complete_is_abnormal(self):
        self.book(); job=self.launch('nonzero'); self.settled()
        self.assertEqual(job['status'],'異常退出'); self.assertEqual(job['exit_code'],7)
        self.assertIn('exit=7',job['tail'])

    def test_C08_full_outputs_while_active(self):
        self.book(); job=self.launch('complete_wait')
        self.pump(lambda:'ready' in job['tail'])
        until=time.monotonic()+8.2
        self.pump(lambda:time.monotonic()>=until,10)
        self.assertEqual(job['status'],'執行中'); self.settled()
        self.assertEqual(job['status'],'已完成')

    def test_C09_P01_stop_retains_partial(self):
        book=self.book(); job=self.launch('sleep'); self.pump(lambda:'ready' in job['tail'])
        pid=job['process'].processId(); self.window.stop()
        self.assertNotEqual(job['status'],'已停止')
        self.settled(); self.assertEqual(job['status'],'已停止')
        self.assertFalse(pid_alive(pid)); self.assertTrue((book/'result/001.png').exists())

    def test_C10_truncated_output_is_invalid(self):
        self.book(); job=self.launch('truncated'); self.settled()
        self.assertEqual(job['status'],'失敗'); self.assertIn('invalid/incomplete',job['error'])

    def test_C10_changed_during_validation(self):
        book=self.book(); self.existing(book); outputs=expected_outputs(book)
        real=output_snapshot
        calls=0
        def changing(paths):
            nonlocal calls
            calls+=1
            result=real(paths)
            return {p:(s[0]+1,*s[1:]) for p,s in result.items()} if calls%2==0 else result
        with patch('batch_job_runtime.output_snapshot',side_effect=changing):
            valid,errors=validate_outputs(outputs)
        self.assertEqual(valid,0); self.assertTrue(errors)

    @unittest.skipUnless(os.name=='nt','Windows kernel Job Object integration')
    def test_P02_P03_tree_and_repeated_stop(self):
        book=self.book(); job=self.launch('tree')
        self.pump(lambda:(book/'descendants.json').exists() and 'ready' in job['tail'])
        pids=json.loads((book/'descendants.json').read_text())
        self.assertGreaterEqual(job['guard'].active_count(),4)
        self.window.stop(); cleanup=job['cleanup']; self.window.stop()
        self.assertIs(job['cleanup'],cleanup)
        self.settled(); self.assertEqual(job['status'],'已停止')
        self.assertTrue(all(not pid_alive(pid) for pid in pids))

    @unittest.skipUnless(os.name=='nt','Windows taskkill integration')
    def test_P04_taskkill_failed_to_start_falls_back(self):
        self.book(); job=self.launch('sleep'); self.pump(lambda:'ready' in job['tail'])
        with patch.object(panel.ProcessCleanup,'command_for',return_value=['not-a-real-taskkill-xyz.exe']):
            self.window.stop(); self.settled()
        self.assertEqual(job['status'],'已停止'); self.assertIn('FailedToStart',job['tail'])

    @unittest.skipUnless(os.name=='nt','Windows taskkill timeout integration')
    def test_P05_taskkill_timeout_is_bounded_and_responsive(self):
        self.book(); job=self.launch('sleep'); self.pump(lambda:'ready' in job['tail'])
        with patch.object(panel.ProcessCleanup,'COMMAND_MS',100), patch.object(
            panel.ProcessCleanup,'command_for',return_value=[sys.executable,'-c','import time;time.sleep(30)']):
            before=self.heartbeats; self.window.stop(); self.settled()
        self.assertEqual(job['status'],'已停止'); self.assertIn('timed out',job['tail'])
        self.assertGreater(self.heartbeats-before,2)

    @unittest.skipUnless(os.name=='nt','Windows taskkill nonzero integration')
    def test_P06_taskkill_nonzero_verifies_actual_exit(self):
        self.book(); job=self.launch('sleep'); self.pump(lambda:'ready' in job['tail'])
        with patch.object(panel.ProcessCleanup,'command_for',return_value=[sys.executable,'-c','raise SystemExit(9)']):
            self.window.stop(); self.settled()
        self.assertEqual(job['status'],'已停止'); self.assertIn('exit=9',job['tail'])

    def test_P07_already_exited_is_safe(self):
        self.book(); job=self.launch(); self.settled()
        self.window.terminate_job(job,'已停止'); self.assertEqual(job['status'],'已完成')

    @unittest.skipUnless(os.name=='nt','Windows parent/descendant race integration')
    def test_P08_parent_exits_before_descendants(self):
        book=self.book(); job=self.launch('orphan')
        self.settled(); self.assertEqual(job['status'],'異常退出')
        self.assertTrue(all(not pid_alive(pid) for pid in json.loads((book/'descendants.json').read_text())))

    @unittest.skipUnless(os.name=='nt','Windows stop/exit race integration')
    def test_P08_exit_during_stop_stays_stopped(self):
        book=self.book(); job=self.launch('exit_on_marker')
        self.pump(lambda:'ready' in job['tail'])
        command=[sys.executable,'-c','import time;time.sleep(.3);raise SystemExit(9)']
        with patch.object(panel.ProcessCleanup,'command_for',return_value=command):
            self.window.stop()
            (book/'release').touch()
            self.settled()
        self.assertEqual(job['status'],'已停止')
        self.assertIn('Verified direct process',job['tail'])

    @unittest.skipUnless(os.name=='nt','Windows isolated jobs integration')
    def test_P09_multiple_jobs_stop_isolation(self):
        self.book('one'); self.book('two'); self.launch('sleep')
        self.pump(lambda:all('ready' in job['tail'] for job in self.window.jobs))
        first,second=self.window.jobs
        self.window.terminate_job(first,'已停止')
        self.pump(lambda:first['process'] is None)
        self.assertEqual(second['status'],'執行中'); self.assertGreater(second['guard'].active_count(),0)
        self.window.stop(); self.settled()
        self.assertTrue(all(job['status']=='已停止' for job in self.window.jobs))

    @unittest.skipUnless(os.name=='nt','Windows cleanup failure reporting')
    def test_P10_cleanup_error_never_claims_stopped(self):
        self.book(); job=self.launch('sleep'); self.pump(lambda:'ready' in job['tail'])
        guard=job['guard']
        with patch.object(guard,'active_count',side_effect=OSError('synthetic query denied')):
            self.window.stop(); self.pump(lambda:job['status']=='清理失敗')
            self.assertIsNotNone(job['process']); self.assertIsNone(self.window.ended)
            self.assertIn('Unable to verify',job['tail'])
        self.window.stop(); self.settled(); self.assertEqual(job['status'],'已停止')

    @unittest.skipUnless(os.name=='nt','Windows actual termination failure reporting')
    def test_P10_termination_fails_until_verified_retry(self):
        self.book(); job=self.launch('sleep'); self.pump(lambda:'ready' in job['tail'])
        guard=job['guard']; process=job['process']; pid=process.processId()
        with patch.object(guard,'terminate',side_effect=OSError('synthetic terminate denied')), patch.object(
            process,'kill'), patch.object(panel.ProcessCleanup,'VERIFY_MS',100), patch.object(
            panel.ProcessCleanup,'command_for',return_value=[sys.executable,'-c','raise SystemExit(9)']):
            self.window.stop(); self.pump(lambda:job['status']=='清理失敗')
            self.assertTrue(pid_alive(pid)); self.assertIsNone(self.window.ended)
            self.assertIn('termination NOT confirmed',job['tail'])
        self.window.stop(); self.settled(); self.assertEqual(job['status'],'已停止')

    def test_overwrite_without_changed_output_is_not_pass(self):
        book=self.book(); self.existing(book)
        self.mode='incomplete'
        job=self.launch('incomplete',True); self.settled()
        self.assertEqual(job['status'],'失敗')
        self.assertIn('unchanged',job['error'])

    def test_format_collision_fails_closed(self):
        book=self.book()
        Image.new('RGB',(8,8),'white').save(book/'001.jpg')
        with self.assertRaises(ValueError):
            expected_outputs(book,'png')

    def test_queued_overwrite_progress_starts_at_zero(self):
        for name in ('one','two'):
            self.existing(self.book(name))
        self.window.concurrent.setValue(1)
        self.launch('normal',True)
        self.assertEqual([job['done'] for job in self.window.jobs],[0,0])
        self.assertEqual(self.window.jobs[1]['status'],'待處理')
        self.settled()

    def test_metadata_progress_does_not_decode_images(self):
        self.book(); job=self.launch('sleep'); self.pump(lambda:'ready' in job['tail'])
        with patch('PIL.Image.open',side_effect=AssertionError('decoded in tick')):
            self.window.tick()
        self.window.stop(); self.settled()

    def test_output_mapping_matches_upstream_without_importing_translation(self):
        source=ROOT/'manga_translator/manga_translator.py'
        module=ast.parse(source.read_text(encoding='utf-8-sig'))
        cls=next(n for n in module.body if isinstance(n,ast.ClassDef) and n.name=='MangaTranslator')
        function=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_calculate_output_path')
        namespace={'os':os}
        exec(compile(ast.Module(body=[function],type_ignores=[]),str(source),'exec'),namespace)
        book=self.book()
        for fmt in (None,'png','webp','none'):
            outputs=expected_outputs(book,fmt)
            actual=tuple(Path(namespace['_calculate_output_path'](None,str(p),{
                'save_to_source_dir':True,'source_result_dir':'result','format':fmt})) for p in sorted(book.glob('*.png')))
            self.assertEqual(outputs,actual)


if __name__=='__main__':
    unittest.main(verbosity=2)
