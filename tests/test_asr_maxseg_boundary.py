"""Boundary behavior regressions (no GPU)."""
import os,runpy,unittest
os.environ["_LIVE_CAPTION_PYW_BOOTSTRAPPED"]="1"
M=runpy.run_path("live-caption-ja.pyw",run_name="boundary_tests")
SR=M["SR"]
class BoundaryTests(unittest.TestCase):
 def test_soft_cut(self):
  f=M["_should_soft_maxseg_cut"]
  self.assertTrue(f(14,.2,16,2.5,.16))
  self.assertFalse(f(12,.2,16,2.5,.16))
  self.assertFalse(f(14,.05,16,2.5,.16))
  self.assertFalse(f(15,.2,16,0,.16))
 def test_overlap_exact(self):
  f=M["_reconcile_boundary_text"]
  self.assertEqual(("について話します",4),f("新しいプロジェクト","ジェクトについて話します",int(.4*SR),SR))
 def test_bridge_weak_match(self):
  f=M["_reconcile_boundary_text"]
  self.assertEqual(("ジェクトについて",0),f("新しいプロジェ","ジェクトについて",int(.4*SR),SR))
  self.assertEqual(("クトについて",2),f("新しいプロジェ","ジェクトについて",int(.4*SR),SR,"プロジェクトについて"))
 def test_no_false_positive(self):
  f=M["_reconcile_boundary_text"]
  self.assertEqual(("はい、わかりました",0),f("はい","はい、わかりました",int(.4*SR),SR))
  self.assertEqual(("ありがとうございます",0),f("ありがとうございます","ありがとうございます",int(.4*SR),SR))
  self.assertEqual(("次の話",0),f("前の話","次の話",0,SR))

 def test_forced_boundary_carries_audio_into_next_segment(self):
  import numpy as np
  from unittest.mock import patch
  edge=runpy.run_path("tests/test_asr_edge_cases.py",run_name="max_seg_integration")
  runner=edge["run_injected"]
  with patch.dict(runner.__globals__,{"args":lambda:edge["args"](max_seg=2.0,min_seg=.5)}):
   sink,_=runner([np.ones(int(2.4*SR),dtype=np.float32)*.2],stop_immediately=True)
  self.assertEqual(2,len(sink.finals))
  self.assertGreaterEqual(edge["FakeBackend"].last.calls[-1],int(.7*SR))

 def test_soft_pause_splits_before_hard_limit(self):
  import numpy as np
  from unittest.mock import patch
  edge=runpy.run_path("tests/test_asr_edge_cases.py",run_name="soft_seg_integration")
  runner=edge["run_injected"]
  chunks=[np.ones(int(2.0*SR),dtype=np.float32)*.2,
          np.zeros(int(.9*SR),dtype=np.float32),
          np.ones(int(.8*SR),dtype=np.float32)*.2]
  with patch.dict(runner.__globals__,{"args":lambda:edge["args"](
      max_seg=3.0,min_seg=.5,silence=2.0,punct_silence=2.0,hesitation_silence=2.0)}):
   sink,_=runner(chunks,stop_immediately=True)
  self.assertGreaterEqual(len(sink.finals),2)
  self.assertLess(edge["FakeBackend"].last.calls[0],int(3.0*SR))


 def test_bridge_repairs_two_character_word_split_once(self):
  import numpy as np
  from unittest.mock import patch
  edge=runpy.run_path("tests/test_asr_edge_cases.py",run_name="bridge_seg_integration")
  runner=edge["run_injected"]
  base=edge["FakeBackend"]
  class BoundaryBackend(base):
   def transcribe(self,audio):
    self.calls.append(len(audio))
    if len(audio)>=int(1.9*SR): return "新しいプロジェ"
    if len(audio)>=int(1.4*SR): return "プロジェクトについて"
    return "ジェクトについて"
  with patch.dict(runner.__globals__,{"FakeBackend":BoundaryBackend,
       "args":lambda:edge["args"](max_seg=2.0,min_seg=.5)}):
   sink,_=runner([np.ones(int(2.4*SR),dtype=np.float32)*.2],stop_immediately=True)
  self.assertEqual(["新しいプロジェ","クトについて"],sink.finals)
  self.assertEqual(3,len(BoundaryBackend.last.calls))

 def test_exact_overlap_skips_extra_bridge_inference(self):
  import numpy as np
  from unittest.mock import patch
  edge=runpy.run_path("tests/test_asr_edge_cases.py",run_name="direct_seg_integration")
  runner=edge["run_injected"]
  base=edge["FakeBackend"]
  class BoundaryBackend(base):
   def transcribe(self,audio):
    self.calls.append(len(audio))
    return "新しいプロジェクト" if len(audio)>=int(1.9*SR) else "ジェクトについて"
  with patch.dict(runner.__globals__,{"FakeBackend":BoundaryBackend,
       "args":lambda:edge["args"](max_seg=2.0,min_seg=.5)}):
   sink,_=runner([np.ones(int(2.4*SR),dtype=np.float32)*.2],stop_immediately=True)
  self.assertEqual(["新しいプロジェクト","について"],sink.finals)
  self.assertEqual(2,len(BoundaryBackend.last.calls))

if __name__=="__main__":unittest.main()
