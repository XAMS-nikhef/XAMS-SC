"""Entry point for device services: python -m xams_sc.devices <name>

A thin __main__ that constructs a device class and runs BaseService. No
business logic here (DESIGN.md §2).
"""

from __future__ import annotations

import argparse
import sys

from ..bus import Bus
from ..config import ConfigError, load
from ..service import setup_logging

SERVICES = {
    "sim": ("xams_sc.devices.sim", "SimService"),
    "cdaq": ("xams_sc.devices.cdaq", "CdaqService"),
    "caen": ("xams_sc.devices.caen", "CaenService"),
    "lakeshore": ("xams_sc.devices.lakeshore", "LakeShoreService"),
    "ups": ("xams_sc.devices.ups", "UpsService"),
    "turbo": ("xams_sc.devices.turbo", "TurboService"),
    "rga": ("xams_sc.devices.rga", "RgaService"),
    "derived": ("xams_sc.devices.derived", "DerivedService"),
}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m xams_sc.devices")
    p.add_argument("service", choices=sorted(SERVICES))
    p.add_argument("--broker", default="127.0.0.1")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--interval", type=float, default=1.0, help="hardware read interval (s)")
    p.add_argument("--log-interval", type=float, default=10.0, help="publish interval (s)")
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--simulate", action="store_true",
                   help="synthetic data; do not touch hardware (§13)")
    args = p.parse_args(argv)

    setup_logging(args.service, args.log_level)

    try:
        config = load()
    except ConfigError as exc:
        print(f"FATAL: configuration is invalid: {exc}", file=sys.stderr)
        return 2

    module_name, class_name = SERVICES[args.service]
    module = __import__(module_name, fromlist=[class_name])
    cls = getattr(module, class_name)

    bus = Bus(client_id=args.service, host=args.broker, port=args.port,
              publish_interval_s=args.interval)
    kwargs = {"interval_s": args.interval, "log_interval_s": args.log_interval}
    if args.simulate and args.service != "sim":
        kwargs["simulate"] = True
    service = cls(config, bus, **kwargs)
    return service.run()


if __name__ == "__main__":
    raise SystemExit(main())
