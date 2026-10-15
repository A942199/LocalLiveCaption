"""ASR backend recovery tests without starting llama.cpp or contacting the network."""
import json
import os
import runpy
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch, Mock

import numpy as np

os.environ["_LIVE_CAPTION_PYW_BOOTSTRAPPED"] = "1"
MOD = runpy.run_path("live-caption-ja.pyw", run_name="asr_backend_regression")
Backend = MOD["LlamaBackend"]


class MockResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({"choices": [{"message": {"content": "language Japanese<asr_text>こんにちは"}}]}).encode()


def backend_stub():
    backend = Backend.__new__(Backend)
    backend.urllib = urllib.request
    backend.url = "http://127.0.0.1:8765"
    backend.lang = "Japanese"
    backend.hot = ""
    backend.history = []
    backend.use_context = False
    backend.max_tokens = 128
    backend.temperature = 0.0
    backend._server_generation = 1
    backend._server_cmd = ["fake-llama-server.exe"]
    backend._closed = False
    backend._restart_lock = threading.Lock()
    backend._retry_after = 0.0
    backend.proc = None
    backend.job = None
    return backend


class ASRBackendRecoveryTests(unittest.TestCase):
    def test_refused_request_reconnects_and_retries_once(self):
        backend = backend_stub()
        with patch.object(backend, "_recover_server", return_value=True) as restart:
            with patch.object(backend.urllib, "urlopen", side_effect=[
                urllib.error.URLError(ConnectionRefusedError(10061, "refused")),
                MockResponse(),
            ]) as network:
                text = backend.transcribe(np.zeros(1600, dtype=np.float32))
        self.assertEqual("こんにちは", text)
        restart.assert_called_once_with(1)
        self.assertEqual(2, network.call_count)
        request = network.call_args_list[0].args[0]
        self.assertEqual("application/json", request.get_header("Content-type"))

    def test_client_error_does_not_restart_or_retry(self):
        backend = backend_stub()
        error = urllib.error.HTTPError(backend.url, 400, "bad request", None, None)
        with patch.object(backend, "_recover_server") as restart:
            with patch.object(backend.urllib, "urlopen", side_effect=error) as network:
                with self.assertRaises(urllib.error.HTTPError):
                    backend.transcribe(np.zeros(1600, dtype=np.float32))
        restart.assert_not_called()
        self.assertEqual(1, network.call_count)

    def test_external_server_is_never_restarted(self):
        backend = backend_stub()
        backend._server_cmd = None
        self.assertFalse(backend._recover_server(1))

    def test_dead_owned_server_is_restarted_only_once(self):
        backend = backend_stub()
        dead_server = Mock()
        dead_server.poll.return_value = 2
        backend.proc = dead_server
        new_server = Mock()
        with patch.object(backend, "_wait_health", side_effect=[RuntimeError("down"), None]) as health:
            with patch.object(backend, "_stop_owned_server") as stop:
                with patch.object(backend, "_bind_job", return_value=77):
                    with patch("subprocess.Popen", return_value=new_server) as spawn:
                        self.assertTrue(backend._recover_server(1))
                        self.assertTrue(backend._recover_server(1))
        self.assertEqual(2, backend._server_generation)
        stop.assert_called_once()
        spawn.assert_called_once()
        self.assertEqual(2, health.call_count)

    def test_failed_restart_has_cooldown(self):
        backend = backend_stub()
        with patch.object(backend, "_wait_health", side_effect=RuntimeError("offline")):
            with patch.object(backend, "_stop_owned_server"):
                with patch("subprocess.Popen", side_effect=OSError("startup failed")) as spawn:
                    with self.assertRaises(OSError):
                        backend._recover_server(1)
                    self.assertFalse(backend._recover_server(1))
        self.assertEqual(1, spawn.call_count)


if __name__ == "__main__":
    unittest.main()
