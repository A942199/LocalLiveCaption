import os
import runpy
import unittest

os.environ["_LIVE_CAPTION_PYW_BOOTSTRAPPED"] = "1"
MOD = runpy.run_path("live-caption-ja.pyw", run_name="streaming_regression_test")
PartialStabilizer = MOD["_PartialStabilizer"]


class StreamingStabilityRegressionTests(unittest.TestCase):
    def test_conflicting_partial_does_not_append_unrelated_text_to_stable_prefix(self):
        st = PartialStabilizer(3)
        st.update("辛い話になるな。")
        st.update("辛い話になるな。大丈夫。")
        previous, stable = st.update("辛い話になるな。大丈夫。")
        self.assertTrue(stable)

        display, _ = st.update("まあさ、でも私が頼む場合はさ、")
        self.assertEqual(
            previous,
            display,
            "A conflicting hypothesis must not be concatenated after an already stable prefix.",
        )


    def test_three_consecutive_conflicts_unlock_and_correct_stale_prefix(self):
        st = PartialStabilizer(3)
        st.update("なんで今日寝ぼったかっていう。")
        st.update("なんで今日寝ぼったかっていう。ああ。")
        original, stable = st.update("なんで今日寝ぼったかっていう。昨日ね。")
        self.assertTrue(stable)

        self.assertEqual(original, st.update("なんで今日寝坊したかっていう。")[0])
        self.assertEqual(original, st.update("なんで今日寝坊したかっていう。昨日ね。")[0])

        corrected, corrected_stable = st.update(
            "なんで今日寝坊したかっていう。昨日ね、二時までハッパーズだったから。"
        )
        self.assertNotEqual(original, corrected)
        self.assertIn("寝坊した", corrected)
        self.assertTrue(corrected.startswith(corrected_stable))


if __name__ == "__main__":
    unittest.main()
