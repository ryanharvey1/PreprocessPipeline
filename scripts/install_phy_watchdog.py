"""Install Phy's cooperative GPU recovery plugin; disabled unless --enable."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.phy_watchdog import install_watchdog as install


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, default=Path.home() / ".phy")
    state = parser.add_mutually_exclusive_group()
    state.add_argument("--enable", action="store_true")
    state.add_argument("--disable", action="store_true")
    args = parser.parse_args()
    result = install(args.config_dir, enabled=True if args.enable else False if args.disable else None)
    print(json.dumps(result, indent=2))
    print("Restart Phy and the pipeline/GUI to load the plugin and GPU-wait publisher.")


if __name__ == "__main__":
    main()
