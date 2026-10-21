"""Simulated device service. See DESIGN.md §13.

Milestone 1 is the whole stack running with no instruments attached: bus,
writers, alarms and UI, on a laptop. This is the service that makes that
possible, and it is also how the stack is developed without occupying the
lab PC — which is still running LabVIEW.

The synthetic values are plausible, not physical. They exist so that a plot
has something on it and a writer has something to store. Nobody should read
physics out of them, so every simulated channel is deliberately given a
visible wobble rather than a flat line.
"""

from __future__ import annotations

import math
import random
import time

from ..config import Channel, Config
from ..model import Measurement, Quality, utcnow
from ..service import BaseService

# Plausible resting points per kind, so a simulated dashboard looks like a
# dashboard rather than a row of zeros.
BASELINE = {
    "rtd": (175.0, 2.0),          # K-ish cryostat temperatures, in C here
    "temperature": (180.0, 1.0),
    "voltage": (1.5, 0.05),
    "current": (0.01, 0.001),
    "hv_vmon": (0.0, 0.0),        # set from the channel's limits below
    "hv_imon": (0.5, 0.05),
    "status": (0.0, 0.0),
}


class SimService(BaseService):
    """Publishes synthetic measurements for every enabled channel.

    `name` is 'sim' so its heartbeat and state appear on the bus exactly as a
    real service's would — the status page and the watchdog must not need to
    know whether they are watching simulated data.
    """

    name = "sim"

    def __init__(self, config: Config, bus, **kw):
        super().__init__(config, bus, simulate=True, **kw)
        self._t0 = time.monotonic()
        self._channels = [c for c in config.enabled_channels() if c.device != "derived"]

    def verify_identity(self) -> bool:
        # Nothing to misidentify. Real services ask the instrument (§6.2).
        return True

    def _value_for(self, ch: Channel, elapsed: float) -> float:
        if ch.kind == "hv_vmon" and ch.limits:
            # Sit at ~80% of the configured software range, with the correct
            # sign, so the sign convention is visible in a plot from day one.
            span = ch.limits["max"] if ch.sign > 0 else ch.limits["min"]
            base, noise = span * 0.8, abs(span) * 0.001
        elif ch.name.startswith("turbo_"):
            return 1500.0 if ch.name == "turbo_speed" else 0.0
        elif ch.device == "scale":
            gross = 280.0 + math.sin(2 * math.pi * elapsed / 600.0) * 0.2
            return {"ln2_gross": gross, "ln2_mass": gross - ch.offset,
                    "ln2_scale_mains": 1.0}.get(ch.name, 0.0)
        elif ch.name.startswith("ups_"):
            return {"ups_on_battery": 0.0, "ups_battery_pct": 100.0,
                    "ups_runtime_min": 42.0}.get(ch.name, 0.0)
        else:
            base, noise = BASELINE.get(ch.kind, (1.0, 0.1))

        # A slow drift plus noise: enough structure that a broken writer is
        # obvious (a flat line) and a working one is obviously working.
        period = 300.0 + (hash(ch.name) % 120)
        drift = math.sin(2 * math.pi * elapsed / period) * noise * 5
        return base + drift + random.gauss(0, noise)

    def read(self) -> list[Measurement]:
        now = utcnow()
        elapsed = time.monotonic() - self._t0
        out = []
        for ch in self._channels:
            raw = self._value_for(ch, elapsed)
            out.append(
                Measurement(
                    t=now, channel=ch.name, value=raw, unit=ch.unit,
                    raw=raw, quality=Quality.OK, src="sim",
                )
            )
        return out
