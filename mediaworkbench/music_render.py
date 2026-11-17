"""Bounded single-source decoding and sample-exact frozen music assembly.

Each distinct original referenced by the plan is decoded exactly once, in one
sequential pass, to a private disk spool covering the union of the phrase
windows that catalogue sections select from it.  Repeated uses and different
sections of the same recording seek inside that one spool; the slow original
is never read again per cut.  Memory stays bounded by one output block.  Join
weights are a function of absolute programme position, so an audition slice is
bit-identical to the same samples of a full export and the block size changes
only I/O granularity.
"""
from array import array
import math
import os
from pathlib import Path
import sys
import tempfile
from . import audio_signal, media_sources, music_plan
from .assets import fingerprint
from .tools import run

RATE = 48           # programme samples per millisecond
SAMPLE_RATE = 48000
CHANNELS = 2


def _ramp(overlap):
    """Raised-cosine join gain by distance from the phrase edge, in samples.

    Reused for the incoming and outgoing side; lookup keeps block boundaries
    irrelevant and avoids recomputing trigonometry per sample.
    """
    if not overlap:
        return None
    return [math.sin((index / overlap) * math.pi / 2) for index in range(overlap + 1)]


def _decode(asset, low_ms, high_ms, path):
    """Sequentially decode one original's needed range into a private spool.

    A little extra is requested past the window so the decoder's frame
    granularity never shortens it, then the spool is cut back to the exact
    phrase clock.  A larger shortfall is a real source/window error and is
    refused rather than filled with invented audio.
    """
    frames = (high_ms - low_ms) * RATE
    run('ffmpeg', [
        '-v', 'error', '-y', '-ss', f'{low_ms / 1000:.9f}',
        '-i', asset['path'], '-t', f'{frames / SAMPLE_RATE + 0.1:.9f}',
        '-map', '0:a:0', '-vn', '-ac', str(CHANNELS), '-ar', str(SAMPLE_RATE),
        '-c:a', 'pcm_f32le', '-f', 'f32le', str(path),
    ])
    if fingerprint(asset['path']) != asset['fingerprint']:
        raise ValueError('source changed during music decoding')
    expected = frames * CHANNELS * 4
    size = path.stat().st_size
    if size % (CHANNELS * 4):
        raise ValueError('decoder returned an incomplete music frame')
    # A decoder may return up to one codec frame fewer than asked for when the
    # window ends exactly on the declared source end.  That tiny shortfall is
    # silence-padded to keep the phrase clock exact; anything larger means the
    # window does not really exist, and inventing audio for it is not allowed.
    if size < expected - 1024 * CHANNELS * 4:
        raise ValueError('music source spool is shorter than its phrase window')
    if size != expected:
        with path.open('r+b') as handle:
            handle.truncate(expected)


class Compiler:
    """Resolve each catalogue phrase once and decode each original once."""

    def __init__(self, conn, plan, private):
        self.conn = conn
        self.plan = plan
        self.private = Path(private)
        self.validated = set()
        self.clips = {}
        self.spools = {}
        self.files = {}
        self.slots = []

    def clip(self, phrase):
        identity = (phrase['name'], phrase['asset_id'], phrase['start_ms'],
                    phrase['end_ms'], phrase['fingerprint'])
        if identity not in self.clips:
            # A saved plan is authoritative about its own cut, gain and order;
            # the asset lookup only resolves the current original location and
            # rejects a changed original before any decode starts.
            self.clips[identity] = media_sources.select(
                self.conn, dict(phrase, has_audio=True), 'final',
                validated=self.validated)
        return self.clips[identity]

    def verify(self):
        validated = set()
        for clip in self.clips.values():
            media_sources.select(self.conn, clip, 'final', validated=validated)

    def close(self):
        for handle in self.files.values():
            try:
                handle.close()
            except ValueError:
                pass
        self.files.clear()

    def __enter__(self):
        resolved = [self.clip(phrase) for phrase in self.plan['sequence']]
        windows = {}
        for clip in resolved:
            key = (clip['asset_id'], clip['fingerprint'])
            low, high = windows.get(key, (clip['start_ms'], clip['end_ms']))
            windows[key] = (min(low, clip['start_ms']), max(high, clip['end_ms']))
        # One sequential decode per original, ordered by first use, covering
        # every window any selected phrase takes from it.  Repeated uses and
        # sibling sections then seek within this spool.
        for clip in resolved:
            key = (clip['asset_id'], clip['fingerprint'])
            if key in self.spools:
                continue
            path = self.private / f'source-{len(self.spools)}.f32'
            low, high = windows[key]
            _decode(clip, low, high, path)
            self.spools[key] = (path, low)
        for key, (path, _) in self.spools.items():
            self.files[key] = path.open('rb')
        for clip in resolved:
            key = (clip['asset_id'], clip['fingerprint'])
            self.slots.append((self.files[key],
                               (clip['start_ms'] - self.spools[key][1]) * RATE))
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def blocks(handles, plan, start, stop, block_frames):
    """Yield exact stereo float blocks of the frozen programme in [start, stop)."""
    sequence = plan['sequence']
    overlap = plan['overlap_ms'] * RATE
    ramp = _ramp(overlap)
    count = len(sequence)
    for cursor in range(start, stop, block_frames):
        frames = min(block_frames, stop - cursor)
        output = audio_signal.silence(frames)
        for index, phrase in enumerate(sequence):
            at = phrase['at_ms'] * RATE
            length = phrase['duration_ms'] * RATE
            left = max(cursor, at)
            right = min(cursor + frames, at + length)
            if left >= right:
                continue
            handle, origin = handles[index]
            handle.seek((origin + left - at) * CHANNELS * 4)
            raw = handle.read((right - left) * CHANNELS * 4)
            if len(raw) != (right - left) * CHANNELS * 4:
                raise ValueError('music source spool is incomplete')
            pcm = array('f')
            pcm.frombytes(raw)
            if sys.byteorder != 'little':
                pcm.byteswap()
            if any(not math.isfinite(value) for value in pcm):
                raise ValueError('nonfinite decoded music')
            gain = phrase['gain']
            first = left - at
            base = (left - cursor) * CHANNELS
            incoming_until = overlap if ramp is not None and index > 0 else 0
            outgoing_from = length - overlap if ramp is not None and index < count - 1 else length
            for local in range(right - left):
                position = first + local
                factor = gain
                if position < incoming_until:
                    factor *= ramp[position]
                if position >= outgoing_from:
                    remaining = length - position
                    factor *= ramp[remaining if remaining > 0 else 0]
                offset = base + local * CHANNELS
                output[offset] += pcm[local * CHANNELS] * factor
                output[offset + 1] += pcm[local * CHANNELS + 1] * factor
        yield output


def render(conn, identity, destination, at_sample=0, samples=None, block_frames=4096):
    plan = music_plan.get(conn, identity)
    total = plan['duration_ms'] * RATE
    samples = total - at_sample if samples is None else samples
    if (type(at_sample) is not int or type(samples) is not int or at_sample < 0 or samples < 1
            or at_sample + samples > total or type(block_frames) is not int
            or not 1 <= block_frames <= 16384):
        raise ValueError('invalid music audition range or block size')
    target = Path(destination).expanduser().absolute()
    target = target.parent.resolve() / target.name
    if os.path.lexists(target) or not target.parent.is_dir():
        raise ValueError('music destination exists or directory is missing')
    with tempfile.TemporaryDirectory(prefix='.music-', dir=target.parent) as private:
        with Compiler(conn, plan, private) as compiler:
            raw = Path(private) / 'music.f32'
            output = Path(private) / 'music.wav'
            with raw.open('wb') as handle:
                for pcm in blocks(compiler.slots, plan, at_sample,
                                  at_sample + samples, block_frames):
                    if sys.byteorder != 'little':
                        pcm.byteswap()
                    handle.write(pcm.tobytes())
            if raw.stat().st_size != samples * CHANNELS * 4:
                raise ValueError('incomplete music assembly')
            run('ffmpeg', ['-v', 'error', '-y', '-f', 'f32le', '-ar', str(SAMPLE_RATE),
                           '-ac', '2', '-i', str(raw), '-c:a', 'pcm_f32le', str(output)])
            compiler.verify()
            checksum = fingerprint(output)
        with output.open('rb') as handle:
            os.fsync(handle.fileno())
        os.link(output, target)
    return dict(plan_id=identity, path=str(target), sha256=checksum,
                samples=samples, score=plan['score'])
