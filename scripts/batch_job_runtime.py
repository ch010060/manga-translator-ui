"""Output validation and Windows containment for the GUI (no translation imports)."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import uuid
from pathlib import Path

IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.bmp', '.webp', '.avif'}


def expected_outputs(book: Path, output_format=None) -> tuple[Path, ...]:
    """Mirror MangaTranslator._calculate_output_path, save_to_source_dir=True."""
    sources = sorted(p for p in book.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS)
    fmt = output_format
    if not fmt or fmt == '不指定' or not fmt.strip() or fmt.lower() == 'none':
        fmt = None
    outputs = tuple(book / 'result' / (f'{p.stem}.{fmt}' if fmt else p.name) for p in sources)
    if len({os.path.normcase(str(p)) for p in outputs}) != len(outputs):
        raise ValueError('Multiple source images map to the same output filename')
    return outputs


def output_snapshot(outputs):
    """Cheap metadata only; no recursive counting or decoding on progress ticks."""
    result = {}
    for path in outputs:
        try:
            stat = path.stat()
            if path.is_file() and stat.st_size:
                result[path] = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        except OSError:
            pass
    return result


def validate_outputs(outputs, baseline=None):
    """Validate mapped images at terminal/skip boundaries, not during polling.

    Compatibility basis: local._should_skip_existing_cli_file uses the path from
    MangaTranslator._calculate_output_path; the upstream save path provides no
    per-page completion receipt. A zero exit code and a pre-existing valid image
    therefore cannot establish that an overwrite happened in this run.

    Fail-safe policy: when a launch baseline is supplied, require a changed
    (size, mtime_ns, ctime_ns) signature AND successful decoding. A writer that
    deliberately preserves all metadata can cause a false failure; we report
    that uncertainty instead of accepting old output or deleting user files.
    This is not an early-exit shortcut: the GUI calls terminal validation only
    after process exit and Windows containment verification.

    Regression evidence: test_overwrite_without_changed_output_is_not_pass,
    test_C03_overwrite_existing_does_not_finish_after_eight_seconds, and
    test_C10_changed_during_validation in tests/test_batch_panel_p0.py.
    """
    from PIL import Image
    valid = 0
    errors = []
    for path in outputs:
        before = output_snapshot((path,)).get(path)
        if before is None:
            errors.append(f'{path.name}: missing or empty')
            continue
        if baseline is not None and before == baseline.get(path):
            errors.append(f'{path.name}: unchanged from before overwrite run')
            continue
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                image.load()
            if output_snapshot((path,)).get(path) != before:
                raise ValueError('file changed during validation')
            valid += 1
        except Exception as exc:
            errors.append(f'{path.name}: invalid/incomplete image ({exc})')
    return valid, errors


class WindowsJob:
    """One named kernel job per GUI worker; all descendants inherit containment.

    The bootstrap assigns itself BEFORE starting the translator. No PID-based
    fallback targets are guessed, and breakaway is not enabled.
    """
    def __init__(self):
        from ctypes import wintypes as w
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.argtypes = (ctypes.c_void_p, w.LPCWSTR)
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.OpenJobObjectW.argtypes = (w.DWORD, w.BOOL, w.LPCWSTR)
        self.api.OpenJobObjectW.restype = w.HANDLE
        self.api.SetInformationJobObject.argtypes = (w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD)
        self.api.QueryInformationJobObject.argtypes = (w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p)
        self.api.TerminateJobObject.argtypes = (w.HANDLE, w.UINT)
        self.api.AssignProcessToJobObject.argtypes = (w.HANDLE, w.HANDLE)
        self.api.GetCurrentProcess.restype = w.HANDLE
        self.api.CloseHandle.argtypes = (w.HANDLE,)
        self.name = 'Local\\MangaBatch-' + uuid.uuid4().hex
        self.handle = self.api.CreateJobObjectW(None, self.name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

        class BasicLimits(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                        ('flags', w.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
                        ('active_limit', w.DWORD), ('affinity', ctypes.c_size_t),
                        ('priority', w.DWORD), ('scheduling', w.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ('read_ops', 'write_ops', 'other_ops',
                                                               'read_bytes', 'write_bytes', 'other_bytes')]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('basic', BasicLimits), ('io', IoCounters), ('process_memory', ctypes.c_size_t),
                        ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t),
                        ('peak_job', ctypes.c_size_t)]
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def active_count(self):
        from ctypes import wintypes as w
        class Accounting(ctypes.Structure):
            _fields_ = [('user', ctypes.c_longlong), ('kernel', ctypes.c_longlong),
                        ('period_user', ctypes.c_longlong), ('period_kernel', ctypes.c_longlong),
                        ('faults', w.DWORD), ('total', w.DWORD), ('active', w.DWORD), ('terminated', w.DWORD)]
        info = Accounting()
        if not self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.active

    def terminate(self):
        if not self.api.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def bootstrap(name, command):
    """Fail closed: a translator is never launched outside its assigned job."""
    from ctypes import wintypes as w
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenJobObjectW.argtypes = (w.DWORD, w.BOOL, w.LPCWSTR)
    api.OpenJobObjectW.restype = w.HANDLE
    api.AssignProcessToJobObject.argtypes = (w.HANDLE, w.HANDLE)
    api.GetCurrentProcess.restype = w.HANDLE
    api.CloseHandle.argtypes = (w.HANDLE,)
    handle = api.OpenJobObjectW(1, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not api.AssignProcessToJobObject(handle, api.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        api.CloseHandle(handle)
    return subprocess.call(command)


if __name__ == '__main__':
    try:
        sys.exit(bootstrap(sys.argv[1], sys.argv[2:]))
    except Exception as exc:
        print(f'[Batch containment failed] {exc}', file=sys.stderr, flush=True)
        sys.exit(125)
