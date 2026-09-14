from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from tag_manager import app as app_module
from tag_manager import db, gallery


class DatabaseReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.sqlite3"
        db.init_db(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test连接启用忙等待和WAL(self) -> None:
        with db.connect(self.db_path) as conn:
            busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

        self.assertEqual(db.SQLITE_BUSY_TIMEOUT_MS, busy_timeout)
        self.assertEqual("wal", journal_mode.lower())

    def test事务异常时回滚(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "触发回滚"):
            with db.connect(self.db_path) as conn:
                conn.execute("INSERT INTO categories (name) VALUES (?)", ("不应提交",))
                raise RuntimeError("触发回滚")

        with db.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM categories WHERE name = ?", ("不应提交",)).fetchone()[0]
        self.assertEqual(0, count)

    def test短暂写锁释放后写入自动继续(self) -> None:
        locked = threading.Event()

        def hold_write_lock() -> None:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT INTO categories (name) VALUES (?)", ("持锁写入",))
                locked.set()
                time.sleep(0.2)
                conn.commit()
            finally:
                conn.close()

        holder = threading.Thread(target=hold_write_lock)
        holder.start()
        self.assertTrue(locked.wait(timeout=2))
        with db.connect(self.db_path) as conn:
            conn.execute("INSERT INTO categories (name) VALUES (?)", ("等待后写入",))
        holder.join(timeout=2)

        self.assertFalse(holder.is_alive())
        with db.connect(self.db_path) as conn:
            names = {row["name"] for row in conn.execute("SELECT name FROM categories")}
        self.assertIn("持锁写入", names)
        self.assertIn("等待后写入", names)


class GalleryScanReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.gallery_root = self.base / "gallery"
        self.gallery_root.mkdir()
        self.db_path = self.base / "test.sqlite3"
        db.init_db(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def scan_patches(self):
        return (
            patch.object(gallery, "GALLERY_DIR", self.gallery_root),
            patch.object(gallery, "connect", lambda: db.connect(self.db_path)),
        )

    def test衣柜节点元数据优先作为图源记录(self) -> None:
        positive, negative, _, _, _, loras, _, _, source, _ = gallery.extract_prompts({
            "night_wardrobe_positive": "klee, sunset",
            "night_wardrobe_negative": "blurry",
            "night_wardrobe_loras": "klee.safetensors:1.0",
        })
        self.assertEqual("klee, sunset", positive)
        self.assertEqual("blurry", negative)
        self.assertEqual("klee.safetensors:1.0", loras)
        self.assertEqual("夜之主衣柜", source)

    def test全量扫描清理磁盘上不存在的记录(self) -> None:
        with db.connect(self.db_path) as conn:
            conn.execute("INSERT INTO gallery_images (path, title) VALUES (?, ?)", ("梦姬图/已删除.png", "已删除"))

        gallery_dir_patch, connect_patch = self.scan_patches()
        with gallery_dir_patch, connect_patch:
            count = gallery.scan_gallery(self.gallery_root, initialize_db=False)

        self.assertEqual(0, count)
        with db.connect(self.db_path) as conn:
            remaining = conn.execute("SELECT COUNT(*) FROM gallery_images").fetchone()[0]
        self.assertEqual(0, remaining)

    def test子目录扫描不会删除其他目录记录(self) -> None:
        child = self.gallery_root / "当前目录"
        child.mkdir()
        with db.connect(self.db_path) as conn:
            conn.execute("INSERT INTO gallery_images (path, title) VALUES (?, ?)", ("其他目录/保留.png", "保留"))

        gallery_dir_patch, connect_patch = self.scan_patches()
        with gallery_dir_patch, connect_patch:
            gallery.scan_gallery(child, initialize_db=False)

        with db.connect(self.db_path) as conn:
            remaining = conn.execute("SELECT path FROM gallery_images").fetchall()
        self.assertEqual(["其他目录/保留.png"], [row["path"] for row in remaining])

    def test同一进程中的扫描会串行执行(self) -> None:
        active = 0
        max_active = 0
        state_lock = threading.Lock()
        start = threading.Barrier(3)
        failures: list[Exception] = []

        def delayed_images(_root: Path):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.1)
            with state_lock:
                active -= 1
            return iter(())

        def run_scan() -> None:
            try:
                start.wait(timeout=2)
                gallery.scan_gallery(self.gallery_root, initialize_db=False)
            except Exception as exc:
                failures.append(exc)

        gallery_dir_patch, connect_patch = self.scan_patches()
        with gallery_dir_patch, connect_patch, patch.object(gallery, "iter_images", delayed_images):
            threads = [threading.Thread(target=run_scan) for _ in range(2)]
            for thread in threads:
                thread.start()
            start.wait(timeout=2)
            for thread in threads:
                thread.join(timeout=3)

        self.assertEqual([], failures)
        self.assertEqual(1, max_active)
        self.assertTrue(all(not thread.is_alive() for thread in threads))


class GalleryRouteReliabilityTests(unittest.TestCase):
    def test手动扫描不重复初始化数据库(self) -> None:
        with patch.object(app_module, "scan_gallery") as scan_gallery:
            response = TestClient(app_module.app).post(
                "/scan-gallery",
                data={"folder": "角色"},
                follow_redirects=False,
            )

        self.assertEqual(303, response.status_code)
        scan_gallery.assert_called_once_with(initialize_db=False)

    def test持续锁冲突返回可恢复的中文提示(self) -> None:
        with patch.object(app_module, "scan_gallery", side_effect=sqlite3.OperationalError("database is locked")):
            response = TestClient(app_module.app).post(
                "/scan-gallery",
                data={"folder": "角色"},
                follow_redirects=False,
            )

        self.assertEqual(303, response.status_code)
        query = parse_qs(urlparse(response.headers["location"]).query)
        self.assertEqual(["warning"], query["message_type"])
        self.assertIn("图库数据库正被其他任务占用", query["message"][0])

    def test非锁类数据库错误仍向上报告(self) -> None:
        with patch.object(app_module, "scan_gallery", side_effect=sqlite3.OperationalError("no such table")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "no such table"):
                app_module.scan_gallery_route("")


class ReloadScopeTests(unittest.TestCase):
    def test开发热重载仅监视应用包目录(self) -> None:
        source = (Path(app_module.BASE_DIR) / "run.py").read_text(encoding="utf-8")
        self.assertIn("PACKAGE_DIR = Path(__file__).resolve().parent", source)
        self.assertIn("reload_dirs=[str(PACKAGE_DIR)]", source)


if __name__ == "__main__":
    unittest.main()
