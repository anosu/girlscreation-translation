"""Codex tools operating on an explicitly selected translation session."""

import argparse
import json
from pathlib import Path

from scripts.session import Session
from scripts.utils import read_json


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("next", "context", "submit", "propose", "status", "finalize"):
        sub = commands.add_parser(command)
        sub.add_argument("--work", type=Path, required=True)
        if command == "context":
            sub.add_argument("id")
        elif command in {"submit", "propose"}:
            sub.add_argument("file", type=Path)
    return parser


def main() -> None:
    parser = argument_parser()
    args = parser.parse_args()
    try:
        session = Session(args.work)
        if args.command == "next":
            result = session.next_group()
        elif args.command == "context":
            result = session.context(args.id)
        elif args.command == "submit":
            result = session.submit(read_json(args.file))
        elif args.command == "propose":
            result = session.propose(read_json(args.file))
        elif args.command == "status":
            result = session.status()
        else:
            result = session.finalize()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, OSError) as error:
        parser.exit(1, f"Validation failed: {error}\n")


if __name__ == "__main__":
    main()
