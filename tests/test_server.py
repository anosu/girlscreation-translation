"""Serve translations with Node alone, without a translation project or Python."""

import http.client
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from workflow.config import ROOT
from workflow.utils import write_json


class ServerTests(unittest.TestCase):
    def test_static_service_needs_only_node_and_preserves_language_routes(self):
        node = shutil.which("node")
        self.assertIsNotNone(node)
        (ROOT / ".cache").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            dir=ROOT / ".cache", prefix="static-test-"
        ) as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / "app.ts", root / "app.ts")
            write_json(root / "translations/es/ui.json", {"start": "Iniciar"})
            write_json(root / "translations/zh-Hans/ui.json", {"start": "开始"})
            write_json(root / "outside.json", {"private": "Do not serve"})
            write_json(root / "translations/.hidden.json", {"private": "Do not serve"})
            obsolete = subprocess.run(
                [node, str(root / "app.ts"), "--config", "missing.toml"],
                cwd=root,
                env={**os.environ, "PATH": ""},
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(obsolete.returncode, 0)
            self.assertIn("takes no command-line options", obsolete.stderr)
            log = root / "server.log"
            with log.open("w", encoding="utf-8") as output:
                process = subprocess.Popen(
                    [node, str(root / "app.ts")],
                    cwd=root,
                    env={
                        **os.environ,
                        "PORT": "0",
                        "PATH": "",
                    },
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )
                try:
                    deadline = time.monotonic() + 20
                    match = None
                    while time.monotonic() < deadline and process.poll() is None:
                        match = re.search(
                            r"localhost:(\d+)", log.read_text(encoding="utf-8")
                        )
                        if match:
                            break
                        time.sleep(0.1)
                    self.assertIsNotNone(match, log.read_text(encoding="utf-8"))
                    connection = http.client.HTTPConnection(
                        "localhost", int(match[1]), timeout=5
                    )
                    try:
                        for code, expected in (("es", "Iniciar"), ("zh-Hans", "开始")):
                            connection.request("GET", f"/translations/{code}/ui.json")
                            response = connection.getresponse()
                            self.assertEqual(response.status, 200)
                            self.assertIn(expected, response.read().decode("utf-8"))
                        for path in (
                            "/translations/fr/ui.json",
                            "/translations/es/",
                            "/translations/.hidden.json",
                            "/translations/../outside.json",
                            "/translations/%2e%2e/outside.json",
                        ):
                            connection.request("GET", path)
                            response = connection.getresponse()
                            self.assertEqual(response.status, 404, path)
                            response.read()
                        connection.request("HEAD", "/translations/es/ui.json")
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.read(), b"")
                        connection.request("POST", "/translations/es/ui.json")
                        response = connection.getresponse()
                        self.assertEqual(response.status, 404)
                        response.read()
                    finally:
                        connection.close()
                finally:
                    process.terminate()
                    process.wait(timeout=10)
