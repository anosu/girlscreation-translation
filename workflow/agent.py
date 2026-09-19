"""Bounded reading and packet-bound submissions for a translation agent."""

import argparse
import json
from pathlib import Path

from workflow.session import Session
from workflow.utils import read_json


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in (
        "next",
        "read",
        "search",
        "submit",
        "revise",
        "propose",
        "finish",
        "status",
        "finalize",
    ):
        sub = commands.add_parser(command)
        sub.add_argument("--work", type=Path, required=True)
        if command in {"submit", "revise", "propose", "finish"}:
            sub.add_argument("--packet", required=True)
        if command in {"submit", "revise", "propose"}:
            sub.add_argument("file", type=Path)
        if command == "read":
            sub.add_argument("resource")
            sub.add_argument("--packet")
            sub.add_argument("--offset", type=int, default=0)
            sub.add_argument("--limit", type=int, default=12000)
        if command == "search":
            sub.add_argument("query")
            sub.add_argument("--offset", type=int, default=0)
    return parser


def main():
    parser = argument_parser()
    args = parser.parse_args()
    try:
        session = Session(args.work)
        if args.command == "next":
            result = session.next_group()
        elif args.command == "read":
            result = session.read_resource(
                args.resource, packet=args.packet, offset=args.offset, limit=args.limit
            )
        elif args.command == "search":
            result = session.search(args.query, args.offset)
        elif args.command == "submit":
            result = session.submit_packet(args.packet, read_json(args.file))
        elif args.command == "revise":
            result = session.revise_packet(args.packet, read_json(args.file))
        elif args.command == "propose":
            result = session.propose_packet(args.packet, read_json(args.file))
        elif args.command == "finish":
            result = session.finish_packet(args.packet)
        elif args.command == "status":
            result = session.status()
        else:
            result = session.finalize()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, OSError) as error:
        parser.exit(1, f"Validation failed: {error}\n")


if __name__ == "__main__":
    main()
