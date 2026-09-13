from __future__ import annotations

import asyncio
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from tag_manager import app as app_module
from tag_manager import db
from tag_manager import manga_routes as routes
from tag_manager.manga_composer import (
    MAX_APNG_FRAMES,
    MangaComposeError,
    compose_apng,
    compose_pdf,
    list_image_files,
)
from tag_manager.manga_service import (
    MangaService,
    MangaServiceError,
    load_config,
    parse_apng_params,
    parse_jmid,
    sanitize_filename,
    save_config,
)


def make_image(path: Path, size=(40, 60), color=(200, 30, 30)) -> Path:
    Image.new("RGB", size, color).save(path)
    return path


def fake_download_images_factory():
    def _download(jmid: str, work_dir: Path, config: dict):
        photo_dir = work_dir / "1"
        photo_dir.mkdir(parents=True, exist_ok=True)
        for index in range(3):
            make_image(photo_dir / f"{index + 1:05d}.jpg", color=(30 + index * 70, 120, 200))
        return f"测试漫画{jmid}", 3

    return _download


def wait_for_status(service: MangaService, job_id: int, expected: set[str], timeout: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = service.get_job(job_id)
        if job and job["status"] in expected:
            return job
        time.sleep(0.02)
    raise AssertionError(f"任务 {job_id} 未在限定时间进入状态：{sorted(expected)}")


class MangaComposerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test合成PDF输出有效文件(self) -> None:
        frames = [make_image(self.root / f"{i:03d}.jpg") for i in range(3)]
        output = compose_pdf(frames, self.root / "out" / "book.pdf")
        self.assertTrue(output.is_file())
        self.assertEqual(b"%PDF", output.read_bytes()[:4])

    def test合成PDF空列表报错(self) -> None:
        with self.assertRaisesRegex(MangaComposeError, "没有可用于合成"):
            compose_pdf([], self.root / "x.pdf")

    def test合成APNG首帧加帧序列且参数生效(self) -> None:
        cover = make_image(self.root / "cover.png", size=(40, 60), color=(10, 200, 10))
        frames = [make_image(self.root / f"{i:03d}.png", size=(40, 60), color=(30 + i * 100, 200 - i * 100, 60)) for i in range(2)]
        output = compose_apng(cover, frames, self.root / "out" / "ani.png", duration_ms=120, loop=2)
        with Image.open(output) as img:
            self.assertTrue(getattr(img, "is_animated", False))
            self.assertEqual(3, img.n_frames)
            self.assertEqual((40, 60), img.size)
            self.assertEqual(120, img.info["duration"])
            self.assertEqual(2, img.info["loop"])

    def test合成APNG首帧独立停留时长(self) -> None:
        cover = make_image(self.root / "cover.png", size=(40, 60), color=(10, 200, 10))
        frames = [make_image(self.root / f"{i:03d}.png", size=(40, 60), color=(30 + i * 100, 200 - i * 100, 60)) for i in range(2)]
        output = compose_apng(cover, frames, self.root / "out" / "ani.png", duration_ms=120, cover_duration_ms=2000)
        with Image.open(output) as img:
            durations = []
            for index in range(img.n_frames):
                img.seek(index)
                durations.append(img.info["duration"])
            self.assertEqual([2000, 120, 120], durations)

    def test合成APNG尺寸归一化三策略(self) -> None:
        cover = make_image(self.root / "cover.png", size=(40, 60))
        odd = make_image(self.root / "odd.jpg", size=(80, 30))
        for strategy in ("pad", "stretch", "crop"):
            with self.subTest(strategy=strategy):
                output = compose_apng(cover, [odd], self.root / f"{strategy}.png", resize=strategy)
                with Image.open(output) as img:
                    img.seek(1)
                    self.assertEqual((40, 60), img.size)

    def test合成APNG缺首帧与空帧与超上限(self) -> None:
        cover = make_image(self.root / "cover.png")
        frame = make_image(self.root / "f.png")
        with self.assertRaisesRegex(MangaComposeError, "首帧"):
            compose_apng(self.root / "missing.png", [frame], self.root / "a.png")
        with self.assertRaisesRegex(MangaComposeError, "没有可用于合成"):
            compose_apng(cover, [], self.root / "b.png")
        with self.assertRaisesRegex(MangaComposeError, "上限"):
            compose_apng(cover, [frame] * (MAX_APNG_FRAMES + 1), self.root / "c.png")
        with self.assertRaisesRegex(MangaComposeError, "策略"):
            compose_apng(cover, [frame], self.root / "d.png", resize="bogus")

    def test目录图片收集按文件名排序且过滤非图片(self) -> None:
        make_image(self.root / "00002.jpg")
        make_image(self.root / "00001.png")
        (self.root / "note.txt").write_text("x", encoding="utf-8")
        names = [p.name for p in list_image_files(self.root)]
        self.assertEqual(["00001.png", "00002.jpg"], names)


class MangaServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "test.sqlite3"
        db.init_db(self.db_path)
        self.config_path = self.root / "manga_config.json"
        save_config({"output_dir": str(self.root / "downloads")}, self.config_path)
        self.service = MangaService(
            storage_root=self.root / "storage",
            config_path=self.config_path,
            connect_factory=lambda: db.connect(self.db_path),
            downloader=fake_download_images_factory(),
        )

    def tearDown(self) -> None:
        self.service.shutdown()
        self.temp_dir.cleanup()

    def test下载任务合成PDF全流程(self) -> None:
        job = asyncio.run(self.service.create_download_job("jm422866", "pdf"))
        final = wait_for_status(self.service, job["id"], {"succeeded"})
        self.assertEqual("测试漫画422866", final["title"])
        self.assertTrue(final["can_download"])
        path, filename = self.service.get_download(job["id"])
        self.assertTrue(filename.endswith(".pdf"))
        self.assertEqual("pdf", path.parent.name)
        self.assertEqual(b"%PDF", path.read_bytes()[:4])
        self.assertFalse((self.root / "storage" / "inbox" / f"job_{job['id']}").exists())

    def test下载APNG需要首图(self) -> None:
        with self.assertRaisesRegex(MangaServiceError, "首图"):
            asyncio.run(self.service.create_download_job("123", "apng"))

    def test下载任务合成APNG含上传首帧(self) -> None:
        from starlette.datastructures import UploadFile

        buffer = io.BytesIO()
        Image.new("RGB", (40, 60), (250, 220, 0)).save(buffer, format="PNG")
        cover = UploadFile(file=io.BytesIO(buffer.getvalue()), filename="cover.png")
        job = asyncio.run(self.service.create_download_job("123", "apng", 300, 2000, 0, "pad", cover))
        final = wait_for_status(self.service, job["id"], {"succeeded"})
        path, filename = self.service.get_download(job["id"])
        self.assertTrue(filename.endswith(".png"))
        with Image.open(path) as img:
            self.assertEqual(4, img.n_frames)
            durations = []
            for index in range(img.n_frames):
                img.seek(index)
                durations.append(img.info["duration"])
            self.assertEqual([2000, 300, 300, 300], durations)

    def test合成任务仅上传图片(self) -> None:
        from starlette.datastructures import UploadFile

        def png_upload(name: str, color) -> UploadFile:
            buffer = io.BytesIO()
            Image.new("RGB", (32, 32), color).save(buffer, format="PNG")
            return UploadFile(file=io.BytesIO(buffer.getvalue()), filename=name)

        cover = png_upload("cover.png", (1, 1, 1))
        frames = [png_upload(f"f{i}.png", (i * 60, 0, 0)) for i in range(2)]
        job = asyncio.run(self.service.create_compose_job(cover, frames, 200, None, 1, "stretch"))
        final = wait_for_status(self.service, job["id"], {"succeeded"})
        self.assertEqual("compose", final["kind"])
        path, _ = self.service.get_download(job["id"])
        with Image.open(path) as img:
            self.assertEqual(3, img.n_frames)
            durations = []
            for index in range(img.n_frames):
                img.seek(index)
                durations.append(img.info["duration"])
            # 未传 cover_duration_ms 时回退配置默认首帧 2000ms
            self.assertEqual([2000, 200, 200], durations)

    def test合成任务缺帧报错(self) -> None:
        from starlette.datastructures import UploadFile

        cover = UploadFile(file=io.BytesIO(b"png"), filename="cover.png")
        with self.assertRaisesRegex(MangaServiceError, "帧序列"):
            asyncio.run(self.service.create_compose_job(cover, []))

    def test下载失败标记failed且清理工作目录(self) -> None:
        def broken(jmid, work_dir, config):
            raise MangaServiceError("download_failed", "网络不通", 502)

        self.service.downloader = broken
        job = asyncio.run(self.service.create_download_job("999", "pdf"))
        final = wait_for_status(self.service, job["id"], {"failed"})
        self.assertEqual("download_failed", final["error_code"])
        self.assertIn("网络不通", final["error_message"])

    def test启动时中断未完成任务(self) -> None:
        with db.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO manga_jobs (kind, jmid, format, work_dir, status) VALUES ('download', '1', 'pdf', 'inbox/job_x', 'running')"
            )
        count = self.service.startup()
        self.assertGreaterEqual(count, 1)
        jobs = self.service.list_jobs()
        self.assertTrue(any(j["status"] == "interrupted" for j in jobs))

    def test删除任务清理产物(self) -> None:
        job = asyncio.run(self.service.create_download_job("777", "pdf"))
        final = wait_for_status(self.service, job["id"], {"succeeded"})
        path, _ = self.service.get_download(job["id"])
        self.service.delete_job(job["id"])
        self.assertIsNone(self.service.get_job(job["id"]))
        self.assertFalse(path.exists())

    def test配置读写与参数钳制(self) -> None:
        config = save_config(
            {"output_dir": str(self.root / "out2"), "proxy": "http://127.0.0.1:7890", "domains": "a.com b.com", "duration_ms": 5, "cover_duration_ms": 999999, "loop": -3, "resize": "crop"},
            self.config_path,
        )
        self.assertEqual("http://127.0.0.1:7890", config["proxy"])
        self.assertEqual(["a.com", "b.com"], config["domains"])
        self.assertEqual(10, config["duration_ms"])
        self.assertEqual(60000, config["cover_duration_ms"])
        self.assertEqual(0, config["loop"])
        self.assertEqual("crop", config["resize"])
        loaded = load_config(self.config_path)
        self.assertEqual(config["output_dir"], loaded["output_dir"])

    def test工具函数(self) -> None:
        self.assertEqual("422866", parse_jmid("JM 422866!"))
        with self.assertRaises(MangaServiceError):
            parse_jmid("没有数字")
        self.assertEqual("a_b", sanitize_filename("a/b", "fb"))
        self.assertEqual("fb", sanitize_filename("???", "fb"))
        params = parse_apng_params("", "", "", "", load_config(self.config_path))
        self.assertEqual(500, params["duration_ms"])
        self.assertEqual(2000, params["cover_duration_ms"])
        params = parse_apng_params("5", "999999", "", "", load_config(self.config_path))
        self.assertEqual(10, params["duration_ms"])
        self.assertEqual(60000, params["cover_duration_ms"])
        with self.assertRaises(MangaServiceError):
            parse_apng_params(500, "", 0, "bogus", load_config(self.config_path))


class MangaRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "test.sqlite3"
        db.init_db(self.db_path)
        self.config_path = self.root / "manga_config.json"
        save_config({"output_dir": str(self.root / "downloads")}, self.config_path)
        self.service = MangaService(
            storage_root=self.root / "storage",
            config_path=self.config_path,
            connect_factory=lambda: db.connect(self.db_path),
            downloader=fake_download_images_factory(),
        )
        self.client = TestClient(app_module.app)
        self.patches = [
            patch.object(routes, "manga_service", self.service),
            patch.object(app_module, "manga_service", self.service),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.service.shutdown()
        self.temp_dir.cleanup()

    def png_bytes(self, color=(9, 9, 9)) -> bytes:
        buffer = io.BytesIO()
        Image.new("RGB", (32, 32), color).save(buffer, format="PNG")
        return buffer.getvalue()

    def test页面渲染包含两个表单与任务区(self) -> None:
        response = self.client.get("/manga")
        self.assertEqual(200, response.status_code)
        self.assertIn("mangaDownloadForm", response.text)
        self.assertIn("mangaComposeForm", response.text)
        self.assertIn("manga-jobs", response.text)

    def test下载任务接口202与状态查询(self) -> None:
        response = self.client.post("/manga/jobs/download", data={"jmid": "422866", "format": "pdf"})
        self.assertEqual(202, response.status_code)
        job_id = response.json()["id"]
        final = wait_for_status(self.service, job_id, {"succeeded"})
        status = self.client.get(f"/api/manga/jobs/{job_id}")
        self.assertEqual(200, status.status_code)
        self.assertEqual("succeeded", status.json()["status"])
        download = self.client.get(f"/manga/jobs/{job_id}/download")
        self.assertEqual(200, download.status_code)
        self.assertEqual(b"%PDF", download.content[:4])
        delete = self.client.post(f"/manga/jobs/{job_id}/delete", follow_redirects=False)
        self.assertEqual(303, delete.status_code)
        self.assertIsNone(self.service.get_job(job_id))

    def test合成任务接口202(self) -> None:
        response = self.client.post(
            "/manga/jobs/compose",
            files=[
                ("cover", ("cover.png", self.png_bytes(), "image/png")),
                ("frames", ("a.png", self.png_bytes((255, 0, 0)), "image/png")),
                ("frames", ("b.png", self.png_bytes((0, 0, 255)), "image/png")),
            ],
            data={"duration_ms": "150", "cover_duration_ms": "900", "loop": "0", "resize": "pad"},
        )
        self.assertEqual(202, response.status_code)
        job_id = response.json()["id"]
        wait_for_status(self.service, job_id, {"succeeded"})
        download = self.client.get(f"/manga/jobs/{job_id}/download")
        self.assertEqual(200, download.status_code)
        self.assertEqual("image/png", download.headers["content-type"])
        with Image.open(io.BytesIO(download.content)) as img:
            durations = []
            for index in range(img.n_frames):
                img.seek(index)
                durations.append(img.info["duration"])
            self.assertEqual([900, 150, 150], durations)

    def test非法输入返回结构化错误(self) -> None:
        bad_jmid = self.client.post("/manga/jobs/download", data={"jmid": "abc", "format": "pdf"})
        self.assertEqual(400, bad_jmid.status_code)
        self.assertEqual("invalid_jmid", bad_jmid.json()["error"]["code"])
        missing_cover = self.client.post("/manga/jobs/download", data={"jmid": "123", "format": "apng"})
        self.assertEqual(400, missing_cover.status_code)
        self.assertEqual("cover_required", missing_cover.json()["error"]["code"])
        missing = self.client.get("/api/manga/jobs/99999")
        self.assertEqual(404, missing.status_code)

    def test配置保存后页面可见(self) -> None:
        response = self.client.post(
            "/manga/config",
            data={"output_dir": str(self.root / "新输出"), "proxy": "http://127.0.0.1:1080", "domains": "a.com", "duration_ms": 300, "cover_duration_ms": 1500, "loop": 1, "resize": "crop"},
            follow_redirects=False,
        )
        self.assertEqual(303, response.status_code)
        config = self.service.get_config()
        self.assertEqual("http://127.0.0.1:1080", config["proxy"])
        self.assertEqual(300, config["duration_ms"])
        self.assertEqual(1500, config["cover_duration_ms"])


class MangaStaticContractTests(unittest.TestCase):
    def test侧边栏导航与版本号(self) -> None:
        base = (Path(app_module.BASE_DIR) / "templates" / "base.html").read_text(encoding="utf-8")
        self.assertIn('href="/manga"', base)
        self.assertIn("漫画下载", base)
        self.assertIn("v1.24.1", base)
        self.assertIn("style.css?v=88", base)
        self.assertEqual("1.24.1", app_module.app.version)

    def test模板包含APNG联动与轮询脚本(self) -> None:
        html = (Path(app_module.BASE_DIR) / "templates" / "manga.html").read_text(encoding="utf-8")
        for snippet in ("mangaDownloadForm", "mangaComposeForm", "/api/manga/jobs/", "__wardrobePageCleanup", "downloadApngFields"):
            self.assertIn(snippet, html)
        self.assertEqual(3, html.count('name="cover_duration_ms"'))


if __name__ == "__main__":
    unittest.main()
