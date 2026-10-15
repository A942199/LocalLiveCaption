"""Cancellation parity: real TCP I/O, task queue ownership, and timeouts."""
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from translation.api import (_request, CancellationToken, TranslationCancelled,
                             TranslationSettings, TranslationAPI)
from translation.engine import GoogleTranslateSink
from test_translation_live_resilience import isolated_sink


class CancellationTransportTests(unittest.TestCase):
    def test_socket_cancel_unblocks_inflight_response(self):
        started=threading.Event()
        release=threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):
                pass
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type","application/json")
                self.send_header("Content-Length","2048")
                self.end_headers()
                self.wfile.write(b'{"translated":"')
                self.wfile.flush()
                started.set()
                release.wait(4)
                try:
                    self.wfile.write(b'late"}')
                except (OSError,ConnectionResetError,BrokenPipeError):
                    pass

        server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        server.daemon_threads=True
        loop=threading.Thread(target=server.serve_forever,daemon=True)
        loop.start()
        token=CancellationToken()
        finished=threading.Event()
        errors=[]

        def worker():
            try:
                _request("http://127.0.0.1:%d/slow"%server.server_port,
                         cancel_token=token,timeout=4)
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        thread=threading.Thread(target=worker,daemon=True)
        thread.start()
        try:
            self.assertTrue(started.wait(2),"did not receive request; errors="+repr(errors))
            start=time.perf_counter()
            token.cancel()
            self.assertTrue(finished.wait(.7),"blocked response read did not stop promptly")
            self.assertLess(time.perf_counter()-start,.7)
            self.assertTrue(errors)
            self.assertIsInstance(errors[0],TranslationCancelled)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_newer_completed_cancels_older_not_on_enqueue(self):
        t=isolated_sink()
        older={"id":1,"text":"旧","cancelled":False,
               "cancel_token":CancellationToken()}
        newer={"id":2,"text":"新","cancelled":False,
               "cancel_token":CancellationToken()}
        t.tasks=[older,newer]
        self.assertFalse(older["cancel_token"].cancelled)
        self.assertTrue(t._on_task_completed(newer,"新译文",True))
        self.assertTrue(older["cancel_token"].cancelled)
        self.assertTrue(older["cancelled"])
        self.assertEqual(("新","新译文",True),t.output)
        t.history.log.assert_called_once()

    def test_hot_settings_abort_inflight_token(self):
        t=isolated_sink()
        token=CancellationToken()
        older={"id":1,"text":"旧","cancelled":False,
               "cancel_token":token}
        t.tasks=[older]
        t.update_settings(TranslationSettings(engine="Google2"))
        self.assertTrue(token.cancelled)
        self.assertTrue(older["cancelled"])

    def test_precancel_does_not_send_request(self):
        token=CancellationToken()
        token.cancel()
        with self.assertRaises(TranslationCancelled):
            _request("http://127.0.0.1:1/",timeout=1,cancel_token=token)

    def test_cancel_idempotent_and_fast(self):
        token=CancellationToken()
        token.cancel()
        token.cancel()
        self.assertTrue(token.cancelled)

    def test_request_timeout_is_total_budget(self):
        started=threading.Event()
        release=threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length","512")
                self.end_headers()
                self.wfile.write(b'{"ok":')
                self.wfile.flush()
                started.set()
                release.wait(2)
        server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        server.daemon_threads=True
        threading.Thread(target=server.serve_forever,daemon=True).start()
        t=time.perf_counter()
        try:
            with self.assertRaises(TimeoutError):
                _request("http://127.0.0.1:%d/wait"%server.server_port,
                         timeout=.25,cancel_token=CancellationToken())
            self.assertLess(time.perf_counter()-t,.9)
            self.assertTrue(started.is_set())
        finally:
            release.set()
            server.shutdown()
            server.server_close()

    def test_cancel_before_response_headers(self):
        arrived=threading.Event()
        release=threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                arrived.set()
                release.wait(2)
                try:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{}')
                except OSError:
                    pass
        server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        server.daemon_threads=True
        threading.Thread(target=server.serve_forever,daemon=True).start()
        token=CancellationToken()
        done=threading.Event()
        errors=[]
        def req():
            try:
                _request("http://127.0.0.1:%d/headers"%server.server_port,
                         timeout=3,cancel_token=token)
            except Exception as ex:
                errors.append(ex)
            finally:
                done.set()
        worker=threading.Thread(target=req,daemon=True)
        worker.start()
        try:
            self.assertTrue(arrived.wait(2))
            token.cancel()
            self.assertTrue(done.wait(.7))
            self.assertIsInstance(errors[0],TranslationCancelled)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_chunked_http_compatibility(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.send_response(200)
                self.send_header("Transfer-Encoding","chunked")
                self.end_headers()
                for data in (b'{"value":',b'42}'):
                    self.wfile.write(("%X\r\n"%len(data)).encode()+data+b'\r\n')
                self.wfile.write(b'0\r\n\r\n')
                self.wfile.flush()
        server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        server.daemon_threads=True
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            got=_request("http://127.0.0.1:%d/chunked"%server.server_port,
                         timeout=2,cancel_token=CancellationToken())
            self.assertEqual({"value":42},got)
        finally:
            server.shutdown()
            server.server_close()


    def test_full_task_queue_cancels_obsolete_http_request(self):
        import json
        slow_started=threading.Event()
        release=threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                length=int(self.headers.get("Content-Length","0"))
                data=json.loads(self.rfile.read(length))
                slow=data["text"]=="古い遅い文"
                if slow:
                    slow_started.set()
                    self.send_response(200)
                    self.send_header("Content-Length","1024")
                    self.end_headers()
                    self.wfile.write(b'{"result":"')
                    self.wfile.flush()
                    release.wait(3)
                    try:self.wfile.write(b'late"}')
                    except OSError:pass
                else:
                    content=json.dumps({"result":"新的中文译文"}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length",str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
        server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        server.daemon_threads=True
        threading.Thread(target=server.serve_forever,daemon=True).start()
        t=isolated_sink()
        settings=TranslationSettings(engine="MTranServer",context_aware=False,
            configs={"MTranServer":{"api_url":
                "http://127.0.0.1:%d/translate"%server.server_port}})
        t.settings=settings
        t.api=TranslationAPI(settings)
        older={"id":1,"text":"古い遅い文","cancelled":False,
               "cancel_token":CancellationToken()}
        newer={"id":2,"text":"新しい文","cancelled":False,
               "cancel_token":CancellationToken()}
        t.tasks=[older,newer]
        previous=threading.Thread(target=t._translation_worker,args=(older,),daemon=True)
        previous.start()
        try:
            self.assertTrue(slow_started.wait(2))
            self.assertFalse(older["cancel_token"].cancelled)
            latest=threading.Thread(target=t._translation_worker,args=(newer,),daemon=True)
            latest.start()
            latest.join(3)
            self.assertFalse(latest.is_alive())
            self.assertTrue(older["cancel_token"].cancelled)
            previous.join(.7)
            self.assertFalse(previous.is_alive(),"old HTTP connection did not abort")
            self.assertEqual(("新しい文","新的中文译文",False),t.output)
            t.history.log.assert_called_once()
            self.assertEqual("新しい文",t.history.log.call_args.args[0])
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            previous.join(2)

if __name__ == "__main__":
    unittest.main()
