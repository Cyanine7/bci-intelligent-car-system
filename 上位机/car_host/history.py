"""与绘图组件无关的有界遥测历史。"""

from collections import deque

from .models import TelemetrySample


class TelemetryHistory:
    def __init__(self, window_seconds: float = 60.0, capacity: int = 6000):
        self.window_seconds = window_seconds
        self.capacity = capacity
        self.samples: deque[TelemetrySample] = deque(maxlen=capacity)

    def append(self, sample: TelemetrySample) -> None:
        self.samples.append(sample)
        self.prune(sample.received_monotonic)

    def prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self.samples and self.samples[0].received_monotonic < cutoff:
            self.samples.popleft()

    def clear(self) -> None:
        self.samples.clear()
