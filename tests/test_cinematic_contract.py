"""Stage 6B.5 narrow contract regressions (no model, no network)."""
import importlib.util
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("cinematic_contract", ROOT / "studio" / "cinematic_contract.py")
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class ContractTests(unittest.TestCase):
    def test_crop_formula_pixels(self):
        # Landscape source, subject near the left. A percent-of-travel crop
        # would shift the subject away from the desired centre.
        width, height = 320, 180
        target_w, target_h = 90, 160
        x, y = 52, 90
        graph = (f"drawbox=x={x-5}:y={y-5}:w=11:h=11:color=white:t=fill,"
                 + c.crop_filter(x/width, y/height, target_w, target_h))
        raw = subprocess.check_output(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "color=black:s=320x180:r=1:d=1", "-vf", graph + ",format=gray", "-frames:v", "1",
                    "-threads", "1", "-f", "rawvideo", "pipe:1"])
        points = [(i % target_w, i // target_w) for i, v in enumerate(raw) if v > 200]
        self.assertTrue(points)
        self.assertLessEqual(abs(sum(p[0] for p in points)/len(points)-target_w/2), 2)
        self.assertLessEqual(abs(sum(p[1] for p in points)/len(points)-target_h/2), 2)

    def test_safe_crop_union(self):
        boxes = [{"x": x, "y": .25, "w": .08, "h": .16} for x in (.12, .17, .20)]
        center = c.safe_face_center(boxes, 1920, 1080)
        self.assertIsNotNone(center)
        half = (1080/1920)/(1920/1080)/2
        for box in boxes:
            self.assertLessEqual(center[0]-half, box["x"])
            self.assertGreaterEqual(center[0]+half, box["x"]+box["w"])
        self.assertIsNone(c.safe_face_center([boxes[0], {**boxes[0], "x": .8}], 1920, 1080))
        self.assertIsNone(c.safe_face_center([], 1920, 1080))

    def test_duration_and_missing_tail(self):
        self.assertTrue(c.duration_matches(c.stream_duration({"duration": "30.000"}), 30, 2/30))
        self.assertFalse(c.duration_matches(c.stream_duration({"duration": "27.100"}), 30, 2/30))
        self.assertFalse(c.duration_matches(None, 30, 2/30))
        self.assertAlmostEqual(c.stream_duration({"duration_ts": "1440000", "time_base": "1/48000"}), 30)

    def test_ass_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "captions.ass"
            p.write_text("[Script Info]\n[Events]\n", encoding="utf-8")
            self.assertEqual(c.ass_dialogue_count(p), 0)
            p.write_text("[Events]\nDialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,Привет\n", encoding="utf-8")
            self.assertEqual(c.ass_dialogue_count(p), 1)

    def test_merge_speech(self):
        result = c.merge_speech_windows([(0.1, 0.3), (0.45, 0.7), (1.8, 2.2)], 2.0)
        self.assertEqual(len(result), 2)
        self.assertAlmostEqual(result[0][0], .02)
        self.assertAlmostEqual(result[0][1], .78)
        self.assertAlmostEqual(result[1][1], 2.0)


    def test_mixed_sample_rate_audio_timeline(self):
        # AAC source + sparse speech windows + intentional silence + normalization.
        # The old filter graph returned 5.8 s instead of 6 s on FFmpeg 7.1.5.
        import json
        with tempfile.TemporaryDirectory() as tmp:
            src = pathlib.Path(tmp) / "source.m4a"
            out = pathlib.Path(tmp) / "result.m4a"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                            "sine=frequency=440:sample_rate=22050:duration=6", "-c:a", "aac", str(src)], check=True)
            fmt = c.canonical_audio()
            fit = c.fit_audio_segment(2)
            filters = []
            for i in range(2):
                filters.append(f"[{i}:a]{fmt},atempo=1,asetpts=N/SR/TB,atrim=start=0.27:end=0.93,"
                               f"asetpts=N/SR/TB,adelay=270:all=1[d{i}];[d{i}]{fmt},{fit}[a{i}]")
            filters.append(f"anullsrc=r=48000:cl=stereo,{fmt},{fit}[a2]")
            filters.append("[a0][a1]concat=n=2:v=0:a=1[ax1];[ax1][a2]concat=n=2:v=0:a=1[ax2]")
            filters.append(f"[ax2]{c.normalized_audio()}[aout]")
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-t", "2", "-i", str(src), "-ss", "2", "-t", "2", "-i", str(src),
                            "-filter_complex", ";".join(filters), "-map", "[aout]", "-t", "6", "-c:a", "aac", "-ac", "2", str(out)], check=True)
            probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(out)]))
            self.assertTrue(c.duration_matches(c.stream_duration(probe["streams"][0]), 6, 2/30))
            self.assertEqual(probe["streams"][0]["sample_rate"], "48000")
            self.assertEqual(probe["streams"][0]["channels"], 2)


    def test_original_audio_loudnorm_keeps_samples(self):
        # Three fully audible AAC intervals, with the same rate and duration
        # as the Original Audio fixture. No intentional silent segments and
        # no final padding. The previous graph can output 3.75s instead of 15s.
        import array
        import json
        with tempfile.TemporaryDirectory() as tmp:
            src = pathlib.Path(tmp) / "source.m4a"
            out = pathlib.Path(tmp) / "original.m4a"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                            "sine=frequency=220:sample_rate=22050:duration=16.5",
                            "-c:a", "aac", str(src)], check=True)
            cmd = ["ffmpeg", "-y", "-v", "error"]
            filters = []
            for i, start in enumerate((0, 5.5, 11)):
                cmd += ["-ss", str(start), "-t", "5", "-i", str(src)]
                filters.append(f"[{i}:a]{c.canonical_audio()},atempo=1,asetpts=N/SR/TB,"
                               f"{c.fit_audio_segment(5)}[a{i}]")
            filters += ["[a0][a1]concat=n=2:v=0:a=1[ax1]",
                        "[ax1][a2]concat=n=2:v=0:a=1[ax2]",
                        f"[ax2]{c.normalized_audio()}[aout]"]
            subprocess.run(cmd + ["-filter_complex", ";".join(filters), "-map", "[aout]",
                                  "-t", "15", "-c:a", "aac", str(out)], check=True)
            probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams",
                                                        "-of", "json", str(out)]))["streams"][0]
            self.assertTrue(c.duration_matches(c.stream_duration(probe), 15, 2/30), probe)
            self.assertEqual(probe["sample_rate"], "48000")
            # Check actual decoded content near the end, not just container duration.
            samples = array.array("f")
            samples.frombytes(subprocess.check_output(["ffmpeg", "-v", "error", "-ss", "14",
                  "-i", str(out), "-t", "0.8", "-ac", "1", "-ar", "8000", "-f", "f32le", "pipe:1"]))
            if sys.byteorder != "little":
                samples.byteswap()
            self.assertGreaterEqual(len(samples), 6280)
            self.assertGreater(sum(x*x for x in samples)/len(samples), 0.0001)


if __name__ == "__main__":
    unittest.main()
