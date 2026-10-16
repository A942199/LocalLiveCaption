"""Original upstream segment semantics, adapted to reliable Final queue."""
import os,runpy,unittest
from unittest.mock import patch
import numpy as np

os.environ["_LIVE_CAPTION_PYW_BOOTSTRAPPED"]="1"
MOD=runpy.run_path("live-caption-ja.pyw",run_name="asr_original_seg_test")
SR=MOD["SR"]


class OriginalAuthorSegmentationTests(unittest.TestCase):
    def test_silence_can_reuse_complete_partial_only(self):
        choose=MOD["_select_reusable_partial"]
        done=(2*SR, "東京へ行きます")
        self.assertEqual("東京へ行きます",choose(done,"silence",2*SR))
        self.assertIsNone(choose(done,"silence",2*SR+1))
        self.assertIsNone(choose(done,"maxlen",2*SR))
        self.assertIsNone(choose(done,"overflow",2*SR))
        self.assertIsNone(choose(done,"shutdown",2*SR))
        self.assertIsNone(choose(None,"silence",2*SR))

    def test_hard_cut_next_audio_is_independent_with_zero_overlap(self):
        edge=runpy.run_path("tests/test_asr_edge_cases.py",run_name="upstream_hardcut_test")
        fake=edge["FakeBackend"]
        fake.last=None
        runner=edge["run_injected"]
        with patch.dict(runner.__globals__,{"args":lambda:edge["args"](max_seg=2.0,min_seg=.5)}):
            sink,_=runner([np.ones(int(2.6*SR),dtype=np.float32)*.2],stop_immediately=True)
        self.assertEqual(2,len(sink.finals))
        self.assertEqual(2,len(fake.last.calls))
        self.assertGreaterEqual(fake.last.calls[0],int(1.9*SR))
        # Old/new segments must not share prior audio: only 0.6s can remain.
        self.assertLessEqual(fake.last.calls[1],int(.6*SR))

    def test_maxlen_always_uses_fresh_asr_not_partial(self):
        self.assertIsNone(MOD["_select_reusable_partial"]((999999,"旧临时文本"),"maxlen",0))

    def test_no_boundary_reconciliation_functions_exist(self):
        self.assertNotIn("_reconcile_boundary_text",MOD)
        self.assertNotIn("_should_soft_maxseg_cut",MOD)



    def test_natural_silence_reuses_completed_partial_in_full_pipeline(self):
        import io
        import time
        import threading
        from types import SimpleNamespace
        edge=runpy.run_path("tests/test_asr_edge_cases.py",run_name="upstream_natural_reuse")
        backend_base=edge["FakeBackend"]
        g=edge["MOD"]["run_asr"].__globals__
        records=[]
        class TestBackend(backend_base):
            last=None
            def __init__(self,*a):
                super().__init__(*a)
                TestBackend.last=self
        class CaptureStream:
            def stop_stream(self): pass
            def close(self): pass
        class Recorder:
            def __init__(self,*a):
                self.f=io.StringIO()
                self.t0=time.time()
            def write(self,rec,audio=None):
                records.append(dict(rec))
        class Output:
            def __init__(self): self.finals=[]
            def final(self,text,*a):self.finals.append(text)
            def partial(self,text):pass
            def partial_stable(self,text,stable):pass
        stop=threading.Event()
        sink=Output()
        def capture(pa,dev,q,signal):
            def feed():
                for i in range(12):
                    q.put(np.ones(int(.2*SR),dtype=np.float32)*.2)
                    time.sleep(.045)
                time.sleep(.15)
                for i in range(5):
                    q.put(np.zeros(int(.2*SR),dtype=np.float32))
                    time.sleep(.045)
                signal.set()
            threading.Thread(target=feed,daemon=True).start()
            return CaptureStream()
        a=edge["args"](max_seg=15.0,min_seg=.5,no_partial=False,
                        partial_every=.1,silence=.6,punct_silence=.6,
                        hesitation_silence=.6,debug="capture")
        with patch.dict(g,{"pyaudio":type("A",(),{"PyAudio":edge["FakeAudio"]}),
                           "get_loopback_device":lambda *unused:{
                                "name":"synthetic","defaultSampleRate":16000,
                                "maxInputChannels":1},
                           "LlamaBackend":TestBackend,
                           "start_capture":capture,
                           "DebugLog":Recorder}),patch("faster_whisper.vad.get_vad_model",
                                return_value=SimpleNamespace(session=edge["FakeVAD"]())):
            edge["MOD"]["run_asr"](a,sink,stop)
        final=[r for r in records if r.get("kind")=="final"]
        self.assertEqual(1,len(final),records)
        self.assertEqual("silence",final[0]["reason"])
        self.assertTrue(final[0]["reused"],records)
        self.assertEqual(1,len(sink.finals))

if __name__=="__main__":
    unittest.main()
