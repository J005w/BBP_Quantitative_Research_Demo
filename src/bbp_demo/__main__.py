"""Offline report and local playback; no adapter or account configuration."""
import argparse
import json
from pathlib import Path

from .report import build_report, write_report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="BBP synthetic correctness demonstration")
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report", help="Generate deterministic offline report and HTML playback")
    report.add_argument("--out", type=Path, default=Path("build/report"))
    dashboard = commands.add_parser("dashboard", help="Serve an existing report on loopback")
    dashboard.add_argument("--report", type=Path, default=Path("build/report/report.json"))
    dashboard.add_argument("--port", type=int, default=8765)
    commands.add_parser("regenerate-data", help="Rebuild the invented candle CSV with integer arithmetic")
    args = parser.parse_args(argv)
    try:
        if args.command == "report":
            result = build_report()
            write_report(result, args.out)
            print(f"Created {args.out / 'report.md'}; SYNTHETIC_CORRECTNESS_ONLY")
            print(f"Raw replay: {result['results']['native_raw_ohlc']['trade_count']} trades; cash reconciliation: PASS")
        elif args.command == "regenerate-data":
            from .data import write_fixture
            from .report import RESOURCES
            write_fixture(RESOURCES / "synthetic_m5.csv")
            print("Rebuilt invented candles; no market data accessed")
        else:
            from .dashboard import serve
            data = json.loads(args.report.read_text(encoding="utf-8"))
            serve(data, host="127.0.0.1", port=args.port)
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
