#!/usr/bin/env python3
"""Google Trends scraper via pytrends (unofficial Trends API) — no API key.

Trends numbers are RELATIVE interest on a 0-100 scale (100 = peak popularity
for that query/timeframe), NOT absolute search volume. Google Trends rate-
limits aggressively (HTTP 429), so calls are wrapped in exponential backoff;
if it's still refusing, the command fails with a clear "rate-limited, wait a
few minutes" message rather than a silent empty. Use sparingly.

Subcommands:
  interest <keyword>            interest over time. --timeframe, --geo
  compare <k1,k2,...>           up to 5 keywords side by side. --timeframe, --geo
  related <keyword>             top + rising related queries. --geo
  by_region <keyword>           geographic breakdown. --resolution, --geo

--timeframe examples: "today 3-m" (default), "today 12-m", "today 5-y".
--geo examples: "" (worldwide, default), "US", "IN", "GB".
--resolution: COUNTRY (default), REGION (states/provinces), CITY.
"""

import argparse
import sys

from common import backoff_retry, die, emit, warn

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from pytrends.request import TrendReq
except ImportError:
    die("The 'pytrends' package is missing. Run: pip install -r requirements.txt")


def trend_req():
    # hl/tz are cosmetic (language + timezone offset in minutes); 360 ~= IST.
    return TrendReq(hl="en-US", tz=360)


def run_pt(fn, what):
    """Run a pytrends call with backoff on 429/transient errors. Raises
    RuntimeError (via backoff_retry) only after several attempts."""
    return backoff_retry(fn, what, attempts=3, base=15.0)


def rate_limited_die(what, err):
    die(f"Google Trends is rate-limiting {what} ({err}). It throttles "
        "unauthenticated access hard — wait a few minutes and try again, or "
        "run fewer Trends queries back-to-back.")


def cmd_interest(args):
    pt = trend_req()

    def go():
        pt.build_payload([args.keyword], timeframe=args.timeframe, geo=args.geo)
        return pt.interest_over_time()

    try:
        df = run_pt(go, "interest over time")
    except RuntimeError as e:
        rate_limited_die("interest over time", e)
    if df is None or df.empty:
        die(f"No Google Trends data for '{args.keyword}' "
            f"(timeframe={args.timeframe}, geo={args.geo or 'worldwide'}). "
            "Try a broader keyword or a longer timeframe.")
    data = [{"date": idx.strftime("%Y-%m-%d"), "interest": int(row[args.keyword])}
            for idx, row in df.iterrows() if not row.get("isPartial", False)]
    emit({"keyword": args.keyword, "timeframe": args.timeframe,
          "geo": args.geo, "note": "interest is relative 0-100, not volume",
          "data": data})


def cmd_compare(args):
    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()][:5]
    if not keywords:
        die("compare needs at least one keyword (comma-separated, up to 5)")
    pt = trend_req()

    def go():
        pt.build_payload(keywords, timeframe=args.timeframe, geo=args.geo)
        return pt.interest_over_time()

    try:
        df = run_pt(go, "compare")
    except RuntimeError as e:
        rate_limited_die("compare", e)
    if df is None or df.empty:
        die(f"No Google Trends data for {keywords} "
            f"(timeframe={args.timeframe}, geo={args.geo or 'worldwide'}).")
    data = []
    for idx, row in df.iterrows():
        if row.get("isPartial", False):
            continue
        point = {"date": idx.strftime("%Y-%m-%d")}
        for k in keywords:
            point[k] = int(row[k]) if k in row else None
        data.append(point)
    emit({"keywords": keywords, "timeframe": args.timeframe, "geo": args.geo,
          "note": "interest is relative 0-100, not volume", "data": data})


def _df_records(df, value_key):
    """pytrends related-query frames have columns 'query' and 'value'."""
    if df is None or df.empty:
        return []
    return [{"query": r["query"], value_key: r["value"]}
            for r in df.to_dict("records")]


def cmd_related(args):
    pt = trend_req()

    def go():
        pt.build_payload([args.keyword], timeframe="today 12-m", geo=args.geo)
        return pt.related_queries()

    try:
        rq = run_pt(go, "related queries")
    except RuntimeError as e:
        rate_limited_die("related queries", e)
    block = (rq or {}).get(args.keyword) or {}
    top = _df_records(block.get("top"), "score")
    rising = [{"query": r["query"], "increase": f"+{r['increase']}%"}
              for r in _df_records(block.get("rising"), "increase")]
    if not top and not rising:
        die(f"No related queries for '{args.keyword}' "
            f"(geo={args.geo or 'worldwide'}). Try a broader keyword.")
    emit({"keyword": args.keyword, "geo": args.geo,
          "top_queries": top, "rising_queries": rising})


def cmd_by_region(args):
    pt = trend_req()

    def go():
        pt.build_payload([args.keyword], timeframe="today 12-m", geo=args.geo)
        return pt.interest_by_region(resolution=args.resolution, inc_low_vol=True)

    try:
        df = run_pt(go, "interest by region")
    except RuntimeError as e:
        rate_limited_die("interest by region", e)
    if df is None or df.empty or args.keyword not in df:
        die(f"No regional Google Trends data for '{args.keyword}' "
            f"(resolution={args.resolution}, geo={args.geo or 'worldwide'}).")
    rows = [{"region": region, "interest": int(val)}
            for region, val in df[args.keyword].items() if int(val) > 0]
    rows.sort(key=lambda r: r["interest"], reverse=True)
    emit({"keyword": args.keyword, "resolution": args.resolution, "geo": args.geo,
          "note": "interest is relative 0-100, not volume",
          "region_count": len(rows), "regions": rows})


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("interest", help="interest over time for a keyword")
    i.add_argument("keyword")
    i.add_argument("--timeframe", default="today 3-m")
    i.add_argument("--geo", default="")
    i.set_defaults(func=cmd_interest)

    c = sub.add_parser("compare", help="compare up to 5 keywords")
    c.add_argument("keywords", help="comma-separated keywords")
    c.add_argument("--timeframe", default="today 3-m")
    c.add_argument("--geo", default="")
    c.set_defaults(func=cmd_compare)

    r = sub.add_parser("related", help="top + rising related queries")
    r.add_argument("keyword")
    r.add_argument("--geo", default="")
    r.set_defaults(func=cmd_related)

    b = sub.add_parser("by_region", help="geographic interest breakdown")
    b.add_argument("keyword")
    b.add_argument("--resolution", default="COUNTRY",
                   choices=["COUNTRY", "REGION", "CITY"])
    b.add_argument("--geo", default="")
    b.set_defaults(func=cmd_by_region)

    args = p.parse_args()
    try:
        args.func(args)
    except RuntimeError as e:
        die(str(e))


if __name__ == "__main__":
    main()
