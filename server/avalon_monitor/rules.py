"""Hub-side derived alerts. Each rule turns a host's history into extra `checks`."""
from __future__ import annotations

import time
from typing import Optional

from .models import Sample


class GpuStallRule:
    """A job is active (any `job` check with value > 0) but the GPU has stayed idle.

    Fires once the job has been continuously active for `minutes` and every
    GPU-utilisation sample in that window is <= `percent`. Tracks job-active
    start times per host in memory (the agent only reports the current state).
    """

    def __init__(self, minutes: int = 10, percent: float = 5.0):
        self.window_sec = minutes * 60
        self.percent = percent
        self.active_since: dict[int, float] = {}

    @staticmethod
    def job_active(sample: Sample) -> bool:
        return any(c.kind == "job" and (c.value or 0) > 0 for c in sample.checks)

    def evaluate(self, host_id: int, sample: Sample, history: list[tuple[float, Optional[float]]]) -> Optional[dict]:
        """history: [(ts, gpu_util)] for the last window. Returns a check dict or None."""
        if not self.job_active(sample) or not sample.gpus:
            self.active_since.pop(host_id, None)
            return None
        since = self.active_since.setdefault(host_id, sample.ts)
        active_for = sample.ts - since
        current = [g.util_percent for g in sample.gpus if g.util_percent is not None]
        current_max = max(current) if current else None
        window = [(t, v) for t, v in history if t >= since and v is not None]
        if current_max is not None:
            window.append((sample.ts, current_max))
        peak = max((v for _, v in window), default=None)
        if active_for < self.window_sec:
            return {"name": "gpu vs job", "kind": "rule", "ok": True, "level": "info", "value": active_for / 60,
                    "detail": "job active %dm, gpu peak %s%% so far" % (active_for // 60, "?" if peak is None else round(peak))}
        if peak is None or peak > self.percent:
            return {"name": "gpu vs job", "kind": "rule", "ok": True, "level": "info", "value": active_for / 60,
                    "detail": "job active %dm, gpu peak %d%% in last %dm" % (active_for // 60, round(peak), self.window_sec // 60)}
        return {"name": "gpu stalled", "kind": "rule", "ok": False, "value": active_for / 60,
                "detail": "job active for %dm but GPU never above %g%% in the last %dm - data-loader bound or hung?"
                          % (active_for // 60, self.percent, self.window_sec // 60)}
