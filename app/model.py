from __future__ import annotations
from dataclasses import dataclass, replace
from typing import List, Iterable

@dataclass
class Segment:
    uid: int
    start_ms: int
    end_ms: int
    deleted: bool = False

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)

    def copy(self):
        return replace(self)

class SegmentModel:
    def __init__(self):
        self.duration_ms = 0
        self._next_uid = 1
        self.segments: List[Segment] = []
        self.selected: set[int] = set()

    def reset(self, duration_ms: int):
        self.duration_ms = max(0, int(duration_ms))
        self._next_uid = 1
        self.segments = [Segment(self._alloc_uid(), 0, self.duration_ms, False)] if self.duration_ms else []
        self.selected.clear()

    def _alloc_uid(self):
        uid = self._next_uid
        self._next_uid += 1
        return uid

    def snapshot(self):
        return ([s.copy() for s in self.segments], set(self.selected), self._next_uid)

    def restore(self, snap):
        segs, sel, nxt = snap
        self.segments = [s.copy() for s in segs]
        self.selected = set(sel)
        self._next_uid = nxt

    def split_at(self, ms: int) -> bool:
        ms = int(ms)
        for i, s in enumerate(self.segments):
            if s.deleted:
                continue
            if s.start_ms + 50 < ms < s.end_ms - 50:
                left = Segment(self._alloc_uid(), s.start_ms, ms, False)
                right = Segment(self._alloc_uid(), ms, s.end_ms, False)
                self.segments[i:i+1] = [left, right]
                self.selected = {right.uid}
                return True
        return False


    def keep_left_at(self, ms: int) -> bool:
        s = self.segment_at(int(ms))
        if not s or s.deleted or not (s.start_ms + 50 < ms < s.end_ms - 50):
            return False
        old_end = s.end_ms
        s.end_ms = int(ms)
        gap = Segment(self._alloc_uid(), int(ms), old_end, True)
        self.segments.append(gap)
        self.segments.sort(key=lambda x:(x.start_ms, x.end_ms))
        self.selected = {s.uid}
        return True

    def keep_right_at(self, ms: int) -> bool:
        s = self.segment_at(int(ms))
        if not s or s.deleted or not (s.start_ms + 50 < ms < s.end_ms - 50):
            return False
        old_start = s.start_ms
        s.start_ms = int(ms)
        gap = Segment(self._alloc_uid(), old_start, int(ms), True)
        self.segments.append(gap)
        self.segments.sort(key=lambda x:(x.start_ms, x.end_ms))
        self.selected = {s.uid}
        return True

    def delete_range(self, start_ms: int, end_ms: int) -> bool:
        a, b = sorted((max(0, int(start_ms)), min(self.duration_ms, int(end_ms))))
        if b - a < 1:
            return False
        changed = False
        rebuilt = []
        for s in self.segments:
            if s.deleted or s.end_ms <= a or s.start_ms >= b:
                rebuilt.append(s.copy())
                continue
            changed = True
            if s.start_ms < a:
                rebuilt.append(Segment(self._alloc_uid(), s.start_ms, a, False))
            mid_start, mid_end = max(s.start_ms, a), min(s.end_ms, b)
            if mid_end > mid_start:
                rebuilt.append(Segment(self._alloc_uid(), mid_start, mid_end, True))
            if s.end_ms > b:
                rebuilt.append(Segment(self._alloc_uid(), b, s.end_ms, False))
        if changed:
            self.segments = sorted(rebuilt, key=lambda x: (x.start_ms, x.end_ms))
            self.selected.clear()
        return changed

    def keep_range(self, start_ms: int, end_ms: int) -> bool:
        a, b = sorted((max(0, int(start_ms)), min(self.duration_ms, int(end_ms))))
        if b - a < 1:
            return False
        changed = False
        rebuilt = []
        for s in self.segments:
            if s.deleted:
                rebuilt.append(s.copy())
                continue
            # active portions outside [a,b] become gaps; intersection stays active
            if s.end_ms <= a or s.start_ms >= b:
                rebuilt.append(Segment(self._alloc_uid(), s.start_ms, s.end_ms, True))
                changed = True
                continue
            if s.start_ms < a:
                rebuilt.append(Segment(self._alloc_uid(), s.start_ms, a, True))
                changed = True
            mid_start, mid_end = max(s.start_ms, a), min(s.end_ms, b)
            if mid_end > mid_start:
                rebuilt.append(Segment(self._alloc_uid(), mid_start, mid_end, False))
            if s.end_ms > b:
                rebuilt.append(Segment(self._alloc_uid(), b, s.end_ms, True))
                changed = True
        if changed:
            self.segments = sorted(rebuilt, key=lambda x: (x.start_ms, x.end_ms))
            self.selected = {x.uid for x in self.segments if not x.deleted}
        return changed


    def merge_selected(self) -> bool:
        """Remove artificial cut boundaries between consecutively selected active segments."""
        picked = sorted([s for s in self.segments if not s.deleted and s.uid in self.selected], key=lambda x: x.start_ms)
        if len(picked) < 2:
            return False
        # Must be one continuous source-time run; never merge across a deleted gap.
        if any(a.end_ms != b.start_ms for a, b in zip(picked, picked[1:])):
            return False
        uid = picked[0].uid
        merged = Segment(uid, picked[0].start_ms, picked[-1].end_ms, False)
        ids = {x.uid for x in picked}
        rebuilt = [x.copy() for x in self.segments if x.uid not in ids]
        rebuilt.append(merged)
        self.segments = sorted(rebuilt, key=lambda x: (x.start_ms, x.end_ms))
        self.selected = {uid}
        return True

    def remove_cut_at(self, ms: int, tolerance_ms: int = 120) -> bool:
        """Remove the nearest artificial boundary between two contiguous active segments."""
        active = sorted([s for s in self.segments if not s.deleted], key=lambda x: x.start_ms)
        best = None
        for a, b in zip(active, active[1:]):
            if a.end_ms == b.start_ms and abs(a.end_ms - int(ms)) <= tolerance_ms:
                d = abs(a.end_ms - int(ms))
                if best is None or d < best[0]:
                    best = (d, a, b)
        if best is None:
            return False
        _, a, b = best
        merged = Segment(a.uid, a.start_ms, b.end_ms, False)
        ids = {a.uid, b.uid}
        rebuilt = [x.copy() for x in self.segments if x.uid not in ids]
        rebuilt.append(merged)
        self.segments = sorted(rebuilt, key=lambda x: (x.start_ms, x.end_ms))
        self.selected = {merged.uid}
        return True

    def restore_deleted_at(self, ms: int) -> bool:
        """Restore a deleted source-time region without changing chronology."""
        target = next((s for s in self.segments if s.deleted and s.start_ms <= int(ms) <= s.end_ms), None)
        if not target:
            return False
        target.deleted = False
        self.selected = {target.uid}
        return True

    def delete_selected(self) -> bool:
        changed = False
        for s in self.segments:
            if s.uid in self.selected and not s.deleted:
                s.deleted = True
                changed = True
        if changed:
            self.selected.clear()
        return changed

    def select_uid(self, uid: int, mode: str = 'replace'):
        if mode == 'replace':
            self.selected = {uid}
        elif mode == 'toggle':
            if uid in self.selected:
                self.selected.remove(uid)
            else:
                self.selected.add(uid)
        elif mode == 'range':
            ordered = [s.uid for s in self.segments if not s.deleted]
            if not ordered:
                return
            anchor = next(iter(self.selected), uid)
            if anchor not in ordered or uid not in ordered:
                self.selected = {uid}
                return
            a, b = sorted((ordered.index(anchor), ordered.index(uid)))
            self.selected = set(ordered[a:b+1])

    def select_many(self, uids, mode: str = 'replace'):
        valid = {s.uid for s in self.segments if not s.deleted}
        picked = set(uids) & valid
        if mode == 'add':
            self.selected |= picked
        elif mode == 'toggle':
            for uid in picked:
                if uid in self.selected:
                    self.selected.remove(uid)
                else:
                    self.selected.add(uid)
        else:
            self.selected = picked

    def select_all(self):
        self.selected = {s.uid for s in self.segments if not s.deleted}

    def clear_selection(self):
        self.selected.clear()

    def trim(self, uid: int, edge: str, ms: int) -> bool:
        ms = int(ms)
        idx = next((i for i,s in enumerate(self.segments) if s.uid == uid), None)
        if idx is None:
            return False
        s = self.segments[idx]
        if s.deleted:
            return False
        active_left = [x for x in self.segments if not x.deleted and x.uid != uid and x.end_ms <= s.start_ms]
        active_right = [x for x in self.segments if not x.deleted and x.uid != uid and x.start_ms >= s.end_ms]
        left_limit = max((x.end_ms for x in active_left), default=0)
        right_limit = min((x.start_ms for x in active_right), default=self.duration_ms)
        if edge == 'left':
            new_start = max(left_limit, min(ms, s.end_ms - 80))
            if new_start == s.start_ms: return False
            s.start_ms = new_start
        elif edge == 'right':
            new_end = min(right_limit, max(ms, s.start_ms + 80))
            if new_end == s.end_ms: return False
            s.end_ms = new_end
        else:
            return False
        self.segments.sort(key=lambda x:(x.start_ms, x.end_ms))
        return True

    def active(self) -> List[Segment]:
        return [s for s in self.segments if not s.deleted and s.duration_ms > 0]

    def selected_active(self) -> List[Segment]:
        return [s for s in self.active() if s.uid in self.selected]

    def segment_at(self, ms: int):
        for s in self.segments:
            if s.start_ms <= ms <= s.end_ms:
                return s
        return None
