from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ExportItem:
    source_uid: int
    start_ms: int
    end_ms: int
    output_path: str
    requested_start_ms: int | None = None
    requested_end_ms: int | None = None


class ExportEngine:
    """FFmpeg export runner.

    mode='lossless': packet stream-copy using already-resolved safe bounds.
    mode='precise': frame-accurate export by re-encoding the selected segment.

    The UI keeps the user's edit points unchanged; only lossless output bounds are
    resolved to codec-safe positions before ExportItem objects are created.
    """

    def __init__(self, ffmpeg: str, mode: str = 'lossless', source_info: dict | None = None):
        self.ffmpeg = ffmpeg
        self.mode = mode
        self.source_info = source_info or {}

    def _video_codec(self):
        for s in self.source_info.get('streams', []):
            if s.get('codec_type') == 'video':
                return (s.get('codec_name') or '').lower()
        return ''

    def _audio_codec(self):
        for s in self.source_info.get('streams', []):
            if s.get('codec_type') == 'audio':
                return (s.get('codec_name') or '').lower()
        return ''

    def _precise_codecs(self, output_path: str):
        ext = Path(output_path).suffix.lower()
        src_v = self._video_codec()
        # Prefer source-family codecs when practical, but always choose encoders
        # that are broadly available in standard FFmpeg Windows builds.
        if ext == '.webm':
            return ['-c:v', 'libvpx-vp9', '-crf', '22', '-b:v', '0', '-c:a', 'libopus', '-b:a', '160k']
        if src_v in {'hevc', 'h265'}:
            return ['-c:v', 'libx265', '-preset', 'medium', '-crf', '18', '-c:a', 'aac', '-b:a', '192k']
        return ['-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-c:a', 'aac', '-b:a', '192k']

    def command(self, src: str, item: ExportItem):
        start = item.start_ms / 1000.0
        dur = max(0.001, (item.end_ms - item.start_ms) / 1000.0)
        common = [self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-y']
        if self.mode == 'precise':
            # Accurate seek happens after input opening. Reset timestamps so each
            # independently exported clip starts from zero.
            return (
                common
                + ['-i', src, '-ss', f'{start:.3f}', '-t', f'{dur:.3f}', '-map', '0:v:0?', '-map', '0:a:0?']
                + self._precise_codecs(item.output_path)
                + ['-movflags', '+faststart', '-avoid_negative_ts', 'make_zero', item.output_path]
            )
        return common + [
            '-ss', f'{start:.3f}', '-i', src, '-t', f'{dur:.3f}',
            '-map', '0', '-c', 'copy', '-avoid_negative_ts', 'make_zero', item.output_path
        ]

    def export_one(self, src: str, item: ExportItem, cancel_cb=None):
        kw = {}
        if os.name == 'nt':
            kw['creationflags'] = 0x08000000
        p = subprocess.Popen(
            self.command(src, item),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            **kw,
        )
        while p.poll() is None:
            if cancel_cb and cancel_cb():
                try:
                    p.terminate()
                    p.wait(timeout=2)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
                return 130, 'Canceled by user'
            time.sleep(0.05)
        err = (p.stderr.read() if p.stderr else b'').decode('utf-8', 'ignore')[-2000:]
        return p.returncode, err


def unique_folder(parent: Path, name: str):
    candidate = parent / name
    if not candidate.exists():
        return candidate
    i = 2
    while True:
        c = parent / f'{name} ({i})'
        if not c.exists():
            return c
        i += 1


def unique_file(path: Path):
    if not path.exists():
        return path
    i = 2
    while True:
        c = path.with_name(f'{path.stem}_{i}{path.suffix}')
        if not c.exists():
            return c
        i += 1
