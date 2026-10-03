"""Engine that needs no ML packages — produces placeholder cues. Used by the test-suite
and by Tools → "Test pipeline" to check ffmpeg/translation without downloading a model."""
from __future__ import annotations

from ..subtitles import Cue
from . import Engine

SAMPLE = ["こんにちは、田中さん。", "今日はいい天気ですね。", "そうですね、散歩に行きましょうか。",
          "ちょっと待って！財布を忘れた。", "大丈夫、私が払うよ。"]


class DummyEngine(Engine):
    name = "dummy"

    def load(self) -> None:
        import os
        import time
        hang = float(os.environ.get("YAKUSURU_TEST_HANG", "0") or 0)   # tests: simulate a stuck load
        if hang:
            time.sleep(hang)
        self.device_used = "none (test engine)"

    def transcribe(self, audio, wav_path, duration, task, progress, cancelled):
        dur = duration or 15.0
        n = max(1, min(len(SAMPLE), int(dur // 3)))
        step = dur / n
        cues = []
        for i in range(n):
            c = Cue(i * step + 0.2, i * step + step - 0.2)
            if task == "translate":
                c.tgt = f"[test line {i + 1}]"
            else:
                c.src = SAMPLE[i]
            cues.append(c)
            progress((i + 1) / n)
        return cues
