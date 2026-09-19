"""Run the real CLI through a tool call against a local Responses test server."""

import json
import os
import shlex
import shutil
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import tomli_w

from workflow.config import ROOT, load_project
from workflow.prepare import prepare_tasks
from workflow.snapshot import sync_sources
from workflow.translate import codex_command, execute_codex, translate_plan
from workflow.utils import read_json, write_json


@contextmanager
def responses_server(reply):
    """Capture real CLI requests and return deterministic Responses events."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            item = reply(body, len(requests))
            response = {
                "id": f"resp_{len(requests)}",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": body["model"],
                "output": [item],
                "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
            }
            events = [
                {
                    "type": "response.created",
                    "response": {**response, "status": "in_progress", "output": []},
                },
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {**item, "status": "in_progress"},
                },
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ]
            content = "".join(
                f"event: {e['type']}\ndata: {json.dumps({**e, 'sequence_number': i})}\n\n"
                for i, e in enumerate(events)
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def final_message(text):
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


@unittest.skipUnless(
    (ROOT / "node_modules/@openai/codex/bin/codex.js").exists(), "Run npm ci first"
)
class CodexIntegrationTests(unittest.TestCase):
    def test_cli_uses_explicit_settings_without_a_model_catalog(self):
        project = load_project()
        with (
            tempfile.TemporaryDirectory() as temporary,
            responses_server(
                lambda _body, _count: final_message("Local connection works.")
            ) as (endpoint, requests),
        ):
            work = Path(temporary)
            home = work / "codex-home"
            home.mkdir()
            backend = replace(
                project.backend(project.targets["zh-Hans"]),
                base_url=endpoint,
                api_key_env="MODEL_TEST_KEY",
                model="test-model",
                effort="high",
            )
            command = codex_command(backend, work)
            self.assertFalse(any("model_catalog_json" in arg for arg in command))
            with (
                patch.dict(
                    os.environ,
                    {"MODEL_TEST_KEY": "local-test-only", "CODEX_HOME": str(home)},
                ),
                (work / "agent.log").open("w", encoding="utf-8") as log,
            ):
                execute_codex(command, "Respond with the test message.", log, 30)
            self.assertEqual(len(requests), 1)
            request = requests[0]
            self.assertEqual(request["model"], backend.model)
            self.assertEqual(request["reasoning"]["effort"], "high")
            self.assertEqual(request["reasoning"].get("summary", "none"), "none")
            self.assertEqual(
                request["instructions"].strip().splitlines(),
                (ROOT / "workflow/prompts/agent-system.md")
                .read_text(encoding="utf-8")
                .strip()
                .splitlines(),
            )
            self.assertTrue(
                any(
                    tool.get("name") in {"exec_command", "shell_command", "shell"}
                    for tool in request["tools"]
                )
            )
            self.assertEqual(
                (work / "agent-report.md").read_text(encoding="utf-8").strip(),
                "Local connection works.",
            )

    def test_agent_executes_context_and_submit_tools(self):
        (ROOT / ".cache").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="agent-test-", dir=ROOT / ".cache"
        ) as temporary:
            root = Path(temporary)
            translations, work = (
                root / "zh-Hans",
                root / "work",
            )
            write_json(translations / "master.json", {})
            task = {
                "id": "item-1",
                "kind": "text",
                "output": "master.json",
                "blocks": [{"texts": ["テスト{0}"]}],
                "context": {
                    "table": "mItems",
                    "field": "ml_description",
                    "record_ids": [1],
                },
                "rules": {"protected_patterns": [r"\{[^{}]+\}"]},
            }
            write_json(root / "input.json", [task])
            config = {
                "schema_version": 1,
                "project": {
                    "id": "integration",
                    "source_language": "ja",
                    "adapter": "adapters.json_file",
                    "backend": "test",
                    "sources": "sources",
                },
                "adapter": {"input": "input.json"},
                "backends": {
                    "test": {
                        "base_url": "http://127.0.0.1",
                        "api_key_env": "MODEL_TEST_KEY",
                        "model": "test-model",
                        "codex": {"effort": "high", "context_window": 1_000_000},
                    }
                },
                "targets": {"zh-Hans": {"translations": "zh-Hans", "work": "work"}},
            }
            (root / "translation.toml").write_text(
                tomli_w.dumps(config), encoding="utf-8"
            )
            project = load_project(root / "translation.toml")
            target = project.targets["zh-Hans"]
            sync_sources(project)
            prepare_tasks(project, target)
            helper = work / "exercise_tools.py"
            helper.write_text(
                "import sys,json\nfrom pathlib import Path\nsys.path.insert(0,"
                + repr(str(ROOT))
                + ")\n"
                "from workflow.session import Session\ns=Session(Path(sys.argv[1]))\npacket=s.next_group()\n"
                "context=s.read_resource(packet['resource'],packet=packet['packet'])\n(s.work/'context-used.json').write_text(json.dumps(context),encoding='utf-8')\n"
                "print(s.submit_packet(packet['packet'],{'1':'测试译文{0}'}))\n",
                encoding="utf-8",
            )
            argv = [sys.executable, str(helper), str(work)]
            command = (
                "& " + " ".join("'" + a.replace("'", "''") + "'" for a in argv)
                if os.name == "nt"
                else shlex.join(argv)
            )
            windows_shell = shutil.which("powershell") if os.name == "nt" else None

            def reply(body, count):
                if count != 1:
                    return final_message("已查阅记录并通过提交校验。")
                tool = next(
                    t
                    for t in body["tools"]
                    if t.get("name") in {"exec_command", "shell_command", "shell"}
                )
                name = tool["name"]
                arguments = (
                    {"cmd": command}
                    if name == "exec_command"
                    else {"command": argv if name == "shell" else command}
                )
                if windows_shell and "shell" in tool.get("parameters", {}).get(
                    "properties", {}
                ):
                    arguments["shell"] = windows_shell
                return {
                    "id": "fc_test",
                    "type": "function_call",
                    "call_id": "call_test",
                    "name": name,
                    "arguments": json.dumps(arguments),
                    "status": "completed",
                }

            with responses_server(reply) as (endpoint, requests):
                with patch.dict(
                    os.environ,
                    {
                        "MODEL_TEST_KEY": "local-test-only",
                    },
                ):
                    try:
                        translate_plan(
                            work,
                            replace(
                                project.backend(target),
                                base_url=endpoint,
                            ),
                            90,
                        )
                    except ValueError:
                        log = (work / "agent.log").read_text(encoding="utf-8")
                        if os.name == "nt" and any(
                            error in log
                            for error in (
                                "CreateProcessAsUserW failed",
                                "CreateFileMapping",
                            )
                        ):
                            self.assertFalse((work / "results.json").exists())
                            self.skipTest(
                                "Host Windows sandbox cannot launch its shells; real tool roundtrip runs on Ubuntu CI"
                            )
                        print(log)
                        raise
                self.assertGreaterEqual(len(requests), 2)
                self.assertTrue(
                    any(
                        item.get("type") == "function_call_output"
                        for item in requests[1]["input"]
                    )
                )
                self.assertEqual(
                    read_json(work / "context-used.json")["resource"], "item-1"
                )
                self.assertEqual(
                    read_json(work / "results.json")["translations"][0]["translation"],
                    "测试译文{0}",
                )


if __name__ == "__main__":
    unittest.main()
