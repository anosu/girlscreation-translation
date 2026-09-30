"""Agent-facing batch submission and progress checks."""

import argparse
import json
import sys
from pathlib import Path

from workflow.session import Session
from workflow.utils import read_json, unique_object


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("submit", "status"):
        sub = commands.add_parser(command)
        sub.add_argument("--work", type=Path, required=True)
        if command == "submit":
            sub.add_argument("file", type=Path, help="JSON file or - for stdin")
    return parser


def main():
    parser = argument_parser()
    args = parser.parse_args()
    try:
        session = Session(args.work)
        if args.command == "submit":
            payload = (
                json.loads(sys.stdin.read(), object_pairs_hook=unique_object)
                if str(args.file) == "-"
                else read_json(args.file)
            )
            result = session.submit_resources(payload)
        else:
            result = session.status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, OSError) as error:
        parser.exit(1, f"Validation failed: {error}\n")


if __name__ == "__main__":
    main()
