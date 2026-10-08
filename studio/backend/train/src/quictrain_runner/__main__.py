from __future__ import annotations

import argparse
import importlib

from .runner import Runner, load_job_spec


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--job-spec", required=True)
    parser.add_argument("--plugin", required=True, help="module:class")
    args = parser.parse_args()
    module_name, class_name = args.plugin.split(":", 1)
    plugin = getattr(importlib.import_module(module_name), class_name)()
    Runner(plugin).run(load_job_spec(args.job_spec))


if __name__ == "__main__":
    main()
