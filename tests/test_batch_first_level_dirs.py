import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from scripts.headless_batch_by_first_level_dirs import (
    BatchOptions,
    build_local_command,
    count_images,
    discover_book_dirs,
    main,
    preflight_checks,
    resolve_default_config_path,
    resolve_sakura_api_base,
    is_book_complete,
    sanitize_log_name,
)


class BatchFirstLevelDirsTests(unittest.TestCase):
    def test_discover_book_dirs_only_returns_first_level_dirs_with_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "book10").mkdir()
            (root / "book2").mkdir()
            (root / "empty").mkdir()
            (root / "book2" / "001.png").write_bytes(b"fake")
            (root / "book10" / "nested").mkdir()
            (root / "book10" / "nested" / "001.jpg").write_bytes(b"fake")
            (root / "cover.png").write_bytes(b"ignored")

            self.assertEqual(
                [p.name for p in discover_book_dirs(root)],
                ["book2"],
            )

    def test_count_images_is_recursive_and_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.PNG").write_bytes(b"fake")
            (root / "b.txt").write_text("ignored", encoding="utf-8")
            (root / "nested").mkdir()
            (root / "nested" / "c.WebP").write_bytes(b"fake")

            self.assertEqual(count_images(root), 2)

    def test_is_book_complete_compares_source_and_result_image_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp) / "book"
            result = book / "result"
            result.mkdir(parents=True)
            (book / "001.png").write_bytes(b"fake")
            (book / "002.jpg").write_bytes(b"fake")
            (result / "001.png").write_bytes(b"fake")

            self.assertFalse(is_book_complete(book, "result"))
            (result / "002.jpg").write_bytes(b"fake")
            self.assertTrue(is_book_complete(book, "result"))

    def test_sanitize_log_name_removes_windows_illegal_characters(self):
        self.assertEqual(sanitize_log_name('a:b*c?d/e\\f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")

    def test_build_local_command_uses_source_result_dir_and_overrides(self):
        options = BatchOptions(
            config=Path("examples/config.json"),
            result_dir_name="result",
            force=True,
            verbose=True,
            use_gpu=True,
            disable_onnx_gpu=True,
            output_format="png",
            batch_size=4,
            attempts=2,
            use_subprocess=True,
            memory_limit=8000,
            memory_percent=80,
            batch_per_restart=25,
        )

        command = build_local_command(
            python_executable=Path(sys.executable),
            book_dir=Path("D:/manga/book"),
            options=options,
        )

        self.assertIn("-m", command)
        self.assertIn("manga_translator", command)
        self.assertIn("--save-to-source-dir", command)
        self.assertIn("--source-result-dir", command)
        self.assertIn("result", command)
        self.assertIn("--overwrite", command)
        self.assertIn("--no-recursive", command)
        self.assertIn("--subprocess", command)
        self.assertIn("--batch-size", command)
        self.assertIn("4", command)

    def test_default_config_path_matches_gui_user_config(self):
        default_config = resolve_default_config_path()

        self.assertEqual(default_config.name, "config.json")
        self.assertEqual(default_config.parent.name, "examples")
        self.assertTrue(default_config.exists())

    def test_build_local_command_always_passes_effective_config_path(self):
        options = BatchOptions(
            config=Path("examples/config.json"),
            result_dir_name="result",
        )

        command = build_local_command(
            python_executable=Path(sys.executable),
            book_dir=Path("D:/manga/book"),
            options=options,
        )

        self.assertIn("--config", command)
        config_arg = command[command.index("--config") + 1]
        self.assertEqual(Path(config_arg), Path("examples/config.json"))
        self.assertIn("--no-recursive", command)
        self.assertIn("--no-overwrite", command)

    def test_source_image_count_ignores_nested_work_and_result_dirs(self):
        from scripts.headless_batch_by_first_level_dirs import count_source_images

        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp) / "book"
            (book / "nested").mkdir(parents=True)
            (book / "result").mkdir()
            (book / "manga_translator_work" / "result").mkdir(parents=True)
            (book / "001.png").write_bytes(b"fake")
            (book / "nested" / "002.png").write_bytes(b"fake")
            (book / "result" / "001.png").write_bytes(b"fake")
            (book / "manga_translator_work" / "result" / "001.png").write_bytes(b"fake")

            self.assertEqual(count_source_images(book, "result"), 1)

    def test_sakura_endpoint_resolves_from_gui_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text('SAKURA_API_BASE="http://127.0.0.1:18080/v1"\n', encoding="utf-8")

            self.assertEqual(
                resolve_sakura_api_base(env_path=env_path),
                "http://127.0.0.1:18080/v1",
            )

    def test_preflight_fails_for_closed_sakura_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text('{"translator": {"translator": "sakura"}}', encoding="utf-8")
            env_path = Path(tmp) / ".env"
            env_path.write_text('SAKURA_API_BASE="http://127.0.0.1:18080/v1"\n', encoding="utf-8")

            with patch("scripts.headless_batch_by_first_level_dirs.urlopen") as urlopen:
                urlopen.side_effect = OSError("connection refused")

                ok, message = preflight_checks(config, env_path=env_path, timeout_seconds=0.01)

            self.assertFalse(ok)
            self.assertIn("Sakura API endpoint is not reachable", message)
            self.assertIn("127.0.0.1:18080", message)

    def test_main_exits_before_submitting_jobs_when_preflight_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            book = root / "book"
            book.mkdir(parents=True)
            (book / "001.png").write_bytes(b"fake")
            config = Path(tmp) / "config.json"
            config.write_text('{"translator": {"translator": "sakura"}}', encoding="utf-8")

            with patch("scripts.headless_batch_by_first_level_dirs.urlopen") as urlopen:
                urlopen.side_effect = OSError("connection refused")
                with patch("scripts.headless_batch_by_first_level_dirs.run_job") as run_job:
                    exit_code = main(["--root", str(root), "--config", str(config)])

            self.assertEqual(exit_code, 2)
            run_job.assert_not_called()

    def test_skip_preflight_allows_dry_run_without_endpoint_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            book = root / "book"
            book.mkdir(parents=True)
            (book / "001.png").write_bytes(b"fake")
            config = Path(tmp) / "config.json"
            config.write_text('{"translator": {"translator": "sakura"}}', encoding="utf-8")

            with patch("scripts.headless_batch_by_first_level_dirs.urlopen") as urlopen:
                exit_code = main([
                    "--root",
                    str(root),
                    "--config",
                    str(config),
                    "--skip-preflight",
                    "--dry-run",
                ])

            self.assertEqual(exit_code, 0)
            urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
