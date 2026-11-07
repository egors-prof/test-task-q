"""
Read input.csv and record one video per row with qscript.CaptureVideo_Magento.

    python capture_video.py                       # input.csv -> ./video
    python capture_video.py my.csv --video-folder out --headless

stdout: one line per successfully saved video (its absolute path)
stderr: progress and errors
exit code: 0 if every row produced a video, 1 if some failed, 2 on bad input
"""

import argparse
import csv
import sys

import qscript

# CSV header -> CaptureVideo_Magento parameter
COLUMN_TO_PARAM = {
    "URL": "url",
    "Company Name": "company_name",
    "Language": "language",
    "Country": "country",
    "Postcode": "postcode",
    "City": "city",
    "Street": "street",
    "HouseNo": "house_no",
}


def _utf8_console() -> None:
    # A Windows console/pipe defaults to cp1252 and crashes on "München.webm".
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv=None) -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("csv_path", nargs="?", default="input.csv", help="input CSV (default: input.csv)")
    parser.add_argument("--video-folder", help=f"override PATH_TO_VIDEO_FOLDER (default: {qscript.PATH_TO_VIDEO_FOLDER})")
    parser.add_argument("--headless", action="store_true", help="no browser window (DuckDuckGo blocks headless search)")
    args = parser.parse_args(argv)

    if args.video_folder:
        qscript.PATH_TO_VIDEO_FOLDER = args.video_folder
    if args.headless:
        qscript.HEADLESS = True

    # utf-8-sig: also accepts the BOM that Excel writes when saving "CSV UTF-8".
    # newline="": required by the csv module so quoted fields may contain line breaks.
    try:
        with open(args.csv_path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=";", quotechar='"')
            missing = [c for c in COLUMN_TO_PARAM if c not in (reader.fieldnames or [])]
            if missing:
                print(f"error: {args.csv_path}: missing column(s): {', '.join(missing)}", file=sys.stderr)
                return 2
            # Read everything first: a malformed file fails before any browser starts.
            rows = [(reader.line_num, row) for row in reader]
    except (OSError, UnicodeDecodeError, csv.Error) as err:
        print(f"error: cannot read {args.csv_path}: {err}", file=sys.stderr)
        return 2

    failed = 0
    for i, (line_no, row) in enumerate(rows, start=1):
        kwargs = {param: (row.get(column) or "").strip() for column, param in COLUMN_TO_PARAM.items()}
        print(f"[{i}/{len(rows)}] line {line_no}: {kwargs['company_name']} ({kwargs['city']})", file=sys.stderr, flush=True)

        path = qscript.CaptureVideo_Magento(**kwargs)
        if path:
            print(path, flush=True)
        else:
            failed += 1
            print(f"[{i}/{len(rows)}] FAILED: {kwargs['company_name']}", file=sys.stderr, flush=True)

    print(f"done: {len(rows) - failed} saved, {failed} failed", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
