#!/usr/bin/env python3
"""Preview or change the Codextab Codex hook configuration."""
import argparse
import importlib.util
import pathlib
import sys

SCRIPT = pathlib.Path(__file__).with_name("codextab.py")
spec = importlib.util.spec_from_file_location("codextab_runtime", SCRIPT)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="write merged hooks.json")
    group.add_argument("--remove", action="store_true", help="remove only Codextab hooks")
    args = parser.parse_args()
    try:
        runtime.install_hooks(apply=args.apply, remove=args.remove)
    except RuntimeError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
