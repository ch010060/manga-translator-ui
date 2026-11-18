"""Persistent-lane scheduling in Batch Console: real QProcess fake workers, no ML imports.

Run: python -m unittest tests/test_batch_panel_lane.py -v
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_batch_panel_p0 as base  # reuse the P0 harness (QApplication, temp library, pump helpers)

panel = base.panel

LANE_WORKER = r'''
import json, os, sys, time
from pathlib import Path
from PIL import Image
args = sys.argv[1:]
mode = args[args.index('--mode') + 1]; force = args[args.index('--force') + 1] == 'True'
books = []
for a in args[args.index('-i') + 1:]:
    if a.startswith('--'): break
    books.append(Path(os.path.abspath(a)))
root = books[0].parent
with open(root / 'launches.jsonl', 'a') as f:
    f.write(json.dumps({'books': [b.name for b in books], 'lane': '--book-lane' in args,
                        'subprocess': '--subprocess' in args}) + '\n')
run = len((root / 'launches.jsonl').read_text().splitlines())  # 1-based launch number, encoded in pixels
def save(path):
    Image.new('RGB', (8, 8), (run, 2, 3)).save(path)
def counter(name):
    path = root / name
    n = int(path.read_text()) if path.exists() else 0
    path.write_text(str(n + 1))
    return n
def event(book, **extra):
    print('[LANE_EVENT] ' + json.dumps(dict({'event': 'book_done', 'book': str(book), 'error': None, 'success': 2,
                                             'failed': 0, 'skipped': 0, 'total': 2}, **extra)), flush=True)
def lane_exit(remaining):
    print('[LANE_EVENT] ' + json.dumps({'event': 'lane_exit', 'reason': 'memory', 'remaining': remaining}),
          flush=True)
    sys.exit(3)
for n, book in enumerate(books):
    # RAM limit hit between books: `local --book-lane` stops before taking book b (b never starts)
    if book.name == 'b' and (mode == 'memory_before_b_always' or
                             (mode == 'memory_before_b_thrice' and counter('before_b') < 3)):
        lane_exit([str(b) for b in books[n:]])
    if book.name == 'b' and mode == 'memory_mid_b_once' and not (root / 'mid_hit').exists():
        (root / 'mid_hit').write_text('1')
        (book / 'result').mkdir(exist_ok=True)
        first = sorted(book.glob('*.png'))[0]
        if force or not (book / 'result' / first.name).exists():
            save(book / 'result' / first.name)  # page 1 finished and saved before the interruption
        event(book, error='interrupted: system memory 96.0% > 95', interrupted='memory', success=1)
        lane_exit([str(b) for b in books[n + 1:]])
    memory_hit = book.name == 'b' and (mode == 'memory_always' or
                                       (mode == 'memory_once' and not (root / 'memory_hit').exists()))
    if memory_hit:  # what `local --book-lane` prints when the RAM limit interrupts a book
        (root / 'memory_hit').write_text('1')
        event(book, error='interrupted: system memory 96.0% > 95', interrupted='memory', success=0)
        print('[LANE_EVENT] ' + json.dumps({'event': 'lane_exit', 'reason': 'memory',
                                            'remaining': [str(b) for b in books[n + 1:]]}), flush=True)
        sys.exit(3)
    if mode == 'crash_second_once' and n == 1 and not (root / 'crashed').exists():
        (root / 'crashed').write_text('1'); sys.exit(3)
    if mode == 'stop_second' and n == 1:
        print('ready', flush=True); time.sleep(60)
    result = book / 'result'; result.mkdir(exist_ok=True)
    if not (mode == 'lie' and n == 1):
        for s in sorted(book.glob('*.png')):
            if force or not (result / s.name).exists():
                save(result / s.name)
    if '--book-lane' in args:
        event(book)
'''


class LaneTests(unittest.TestCase):
    pass


for _name in ('setUpClass', 'beat', 'pump', 'tearDown', 'book', 'launch', 'settled'):
    setattr(LaneTests, _name, base.P0Tests.__dict__[_name])


def _setUp(self):
    base.P0Tests.__dict__['setUp'](self)
    self.patches[3].stop()  # replace the single-book fake command with a lane-aware worker
    self.lane_worker = self.root / 'lane_worker.py'
    self.lane_worker.write_text(LANE_WORKER, encoding='utf-8')
    self.patches[3] = patch.object(panel.batch, 'build_local_command', side_effect=lambda py, book, options: [
        sys.executable, '-u', str(self.lane_worker), '--mode', self.mode, '--force', str(options.force),
        '-i', str(book), '--subprocess'])  # the real GUI default passes --subprocess
    self.patches[3].start()


LaneTests.setUp = _setUp


def launches(self):
    path = self.root / 'launches.jsonl'
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


LaneTests.launches = launches


class Cases(LaneTests):
    def start(self, books, lane_books, concurrency=1, mode='normal', force=False):
        for name in books:
            self.book(name)
        self.window.advanced['lane_books'] = lane_books
        self.window.concurrent.setValue(concurrency)
        self.launch(mode, force)
        return {j['book'].name: j for j in self.window.jobs}

    def test_one_process_translates_three_books(self):
        jobs = self.start(['a', 'b', 'c'], lane_books=3)
        self.settled()
        self.assertEqual({n: j['status'] for n, j in jobs.items()}, dict.fromkeys('abc', '已完成'))
        self.assertEqual(self.launches(), [{'books': ['a', 'b', 'c'], 'lane': True, 'subprocess': False}])

    def test_two_lanes_in_parallel(self):
        jobs = self.start(['a', 'b', 'c', 'd'], lane_books=2, concurrency=2)
        self.settled()
        self.assertEqual({n: j['status'] for n, j in jobs.items()}, dict.fromkeys('abcd', '已完成'))
        self.assertEqual(sorted(l['books'] for l in self.launches()), [['a', 'b'], ['c', 'd']])

    def test_crash_requeues_unreached_books_once(self):
        jobs = self.start(['a', 'b', 'c'], lane_books=3, mode='crash_second_once')
        self.settled()
        self.assertEqual({n: j['status'] for n, j in jobs.items()}, dict.fromkeys('abc', '已完成'))
        self.assertEqual([l['books'] for l in self.launches()], [['a', 'b', 'c'], ['b', 'c']])
        self.assertEqual((jobs['a']['lane_retries'], jobs['b']['lane_retries']), (0, 1))

    def test_event_without_valid_outputs_is_not_success(self):
        jobs = self.start(['a', 'b', 'c'], lane_books=3, mode='lie')
        self.settled()
        self.assertEqual((jobs['a']['status'], jobs['b']['status'], jobs['c']['status']),
                         ('已完成', '失敗', '已完成'))

    def test_stop_keeps_finished_books(self):
        jobs = self.start(['a', 'b', 'c'], lane_books=3, mode='stop_second')
        self.pump(lambda: jobs['a']['lane_event'] is not None and 'ready' in jobs['a']['tail'])
        self.assertFalse(jobs['b']['lane_waiting'])  # b became the current book
        self.assertTrue(jobs['c']['lane_waiting'])
        self.window.stop()
        self.settled()
        self.assertEqual((jobs['a']['status'], jobs['b']['status'], jobs['c']['status']),
                         ('已完成', '已停止', '已停止'))

    def test_waiting_books_do_not_stall(self):
        jobs = self.start(['a', 'b', 'c'], lane_books=3, mode='stop_second')
        self.pump(lambda: 'ready' in jobs['a']['tail'])
        jobs['c']['last'] = jobs['c']['start'] = 0  # would trip any clock if it were checked
        self.window.stall.setValue(1)
        self.window.tick()
        self.assertEqual(jobs['c']['status'], '執行中')
        self.window.stop(); self.settled()

    def test_memory_exit_requeues_without_using_crash_retry(self):
        jobs = self.start(['a', 'b', 'c'], lane_books=3, mode='memory_once')
        self.settled()
        self.assertEqual({n: j['status'] for n, j in jobs.items()}, dict.fromkeys('abc', '已完成'))
        self.assertEqual([l['books'] for l in self.launches()], [['a', 'b', 'c'], ['b', 'c']])
        # b was running when the RAM limit hit -> charged; c never started -> not charged
        self.assertEqual({n: (j['memory_requeues'], j['lane_retries']) for n, j in jobs.items()},
                         {'a': (0, 0), 'b': (1, 0), 'c': (0, 0)})

    def test_memory_exit_is_bounded(self):
        jobs = self.start(['a', 'b'], lane_books=2, mode='memory_always')
        self.settled()
        self.assertEqual(jobs['a']['status'], '已完成')
        self.assertEqual(jobs['b']['status'], '失敗')  # interrupted every time, no valid outputs
        self.assertEqual(jobs['b']['memory_requeues'], panel.LANE_MEMORY_REQUEUES)
        self.assertEqual(len(self.launches()), 1 + panel.LANE_MEMORY_REQUEUES)
        self.assertIn('interrupted', jobs['b']['error'])

    def test_unstarted_books_are_not_charged_by_repeated_memory_exits(self):
        jobs = self.start(['a', 'b', 'c'], lane_books=3, mode='memory_before_b_thrice')
        self.settled()
        self.assertEqual({n: j['status'] for n, j in jobs.items()}, dict.fromkeys('abc', '已完成'))
        self.assertEqual([l['books'] for l in self.launches()],
                         [['a', 'b', 'c'], ['b', 'c'], ['b', 'c'], ['b', 'c']])
        self.assertEqual({n: (j['memory_requeues'], j['lane_retries']) for n, j in jobs.items()},
                         dict.fromkeys('abc', (0, 0)))

    def test_persistent_ram_pressure_stops_scheduling(self):
        jobs = self.start(['a', 'b', 'c'], lane_books=3, mode='memory_before_b_always')
        self.settled()
        self.assertEqual({n: j['status'] for n, j in jobs.items()},
                         {'a': '已完成', 'b': '已停止', 'c': '已停止'})
        self.assertEqual(len(self.launches()), panel.LANE_MEMORY_STREAK)  # no endless lane cycling
        self.assertIn('RAM limit', jobs['b']['error'])

    def resumed_page_runs(self, force):
        jobs = self.start(['a', 'b'], lane_books=2, mode='memory_mid_b_once', force=force)
        self.settled()
        self.assertEqual((jobs['a']['status'], jobs['b']['status']), ('已完成', '已完成'))
        self.assertEqual(len(self.launches()), 2)  # one interruption, one resume, no loop
        from PIL import Image
        return [Image.open(self.root / 'b' / 'result' / n).getpixel((0, 0))[0] for n in ('001.png', '002.png')]

    def test_memory_resume_keeps_saved_pages_without_overwrite(self):
        self.assertEqual(self.resumed_page_runs(force=False), [1, 2])  # page 1 from run 1 is kept

    def test_memory_resume_with_overwrite_redoes_pages_but_terminates(self):
        self.assertEqual(self.resumed_page_runs(force=True), [2, 2])  # documented trade-off: page 1 redone

    def test_gui_default_lane_uses_book_lane_without_subprocess(self):
        for name in 'abcde':
            self.book(name)
        self.window.advanced['lane_books'] = self.window.defaults['lane_books']  # harness pins 0; use GUI default
        self.assertEqual(self.window.advanced['lane_books'], 4)
        self.launch()
        self.settled()
        jobs = {j['book'].name: j for j in self.window.jobs}
        self.assertEqual({n: j['status'] for n, j in jobs.items()}, dict.fromkeys('abcde', '已完成'))
        self.assertEqual(sorted(l['books'] for l in self.launches()), [['a', 'b', 'c', 'd'], ['e']])
        self.assertTrue(all(l['lane'] and not l['subprocess'] for l in self.launches()))

    def test_lane_off_keeps_one_process_per_book(self):
        jobs = self.start(['a', 'b'], lane_books=0)
        self.settled()
        self.assertEqual({n: j['status'] for n, j in jobs.items()}, dict.fromkeys('ab', '已完成'))
        self.assertEqual(sorted(l['books'] for l in self.launches()), [['a'], ['b']])
        self.assertFalse(any(l['lane'] for l in self.launches()))
        self.assertTrue(all(l['subprocess'] for l in self.launches()))  # legacy flags untouched


del LaneTests  # only run Cases

if __name__ == '__main__':
    unittest.main(verbosity=2)
