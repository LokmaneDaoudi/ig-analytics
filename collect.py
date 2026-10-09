#!/usr/bin/env python3
"""Pull Instagram analytics into CSV files under ./data.

Usage:
    IG_TOKEN=... python collect.py            # real collection
    python collect.py --demo                  # write synthetic data for previewing the dashboard
    IG_TOKEN=... python collect.py --refresh-token   # print a refreshed long-lived token
"""
import argparse
import csv
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

API_VERSION = os.environ.get("IG_API_VERSION", "v21.0")
BASE = f"https://graph.instagram.com/{API_VERSION}"
DATA = Path(__file__).parent / "data"

ACCOUNT_FIELDS = ["date", "followers", "media_count", "reach", "views",
                  "profile_views", "accounts_engaged", "reposts",
                  "reach_followers", "reach_non_followers", "views_followers", "views_non_followers",
                  "reach_reels", "reach_posts", "reach_stories",
                  "follower_count", "fu_total", "fu_follower", "fu_non_follower", "fu_unknown", "fetched_at"]
AUDIENCE_FIELDS = ["date", "breakdown", "key", "value"]
MEDIA_FIELDS = ["id", "timestamp", "media_type", "media_product_type", "permalink", "caption"]
METRIC_FIELDS = ["id", "fetched_at", "likes", "comments", "reach", "views",
                 "saves", "shares", "reposts", "total_interactions"]

ACCOUNT_METRICS = ["reach", "views", "profile_views", "accounts_engaged", "reposts"]
MEDIA_METRICS = {"reach": "reach", "views": "views", "saved": "saves",
                 "shares": "shares", "reposts": "reposts",
                 "total_interactions": "total_interactions"}

RECENT_DAYS = 14   # posts newer than this are re-snapshotted every run
MAX_POSTS = 50     # how many latest posts to track


# ---------- helpers ----------

def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def read_csv(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path, fields, rows):
    DATA.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def api_get(path, token, **params):
    params["access_token"] = token
    url = path if path.startswith("http") else f"{BASE}/{path.lstrip('/')}"
    for attempt in range(4):
        r = requests.get(url, params=params, timeout=30)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 ** attempt * 2)
            continue
        if not r.ok:
            try:
                msg = r.json().get("error", {}).get("message", r.text)
            except ValueError:
                msg = r.text
            raise RuntimeError(f"{r.status_code} on {path}: {msg}")
        return r.json()
    raise RuntimeError(f"Gave up on {path} after retries")


# ---------- account metrics ----------

def account_metric_for_day(token, metric, day):
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    data = api_get("me/insights", token, metric=metric, period="day",
                   metric_type="total_value",
                   since=int(start.timestamp()),
                   until=int((start + timedelta(days=1)).timestamp()))
    return data["data"][0]["total_value"]["value"]


# (metric, candidate breakdown names, API value -> column)
BREAKDOWNS = [
    ("reach", ["follow_type"], {"FOLLOWER": "reach_followers", "NON_FOLLOWER": "reach_non_followers"}),
    ("views", ["follow_type", "follower_type"], {"FOLLOWER": "views_followers", "NON_FOLLOWER": "views_non_followers"}),
    ("reach", ["media_product_type"], {"REEL": "reach_reels", "FEED": "reach_posts", "POST": "reach_posts",
                                       "CAROUSEL_CONTAINER": "reach_posts", "STORY": "reach_stories"}),
]


def day_window(day):
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def breakdown_for_day(token, metric, names, day):
    since, until = day_window(day)
    last = None
    for name in names:  # Meta's docs are inconsistent about some breakdown names, so try each
        try:
            d = api_get("me/insights", token, metric=metric, period="day", metric_type="total_value",
                        breakdown=name, since=since, until=until)
            res = d["data"][0]["total_value"].get("breakdowns", [{}])[0].get("results", [])
            return {r["dimension_values"][0]: r.get("value", 0) for r in res}
        except Exception as e:
            last = e
    raise last


def follower_estimates(token, current):
    """Rebuild past daily follower totals from the API's daily net-change series (last 30 days)."""
    end = now_utc()
    try:
        d = api_get("me/insights", token, metric="follower_count", period="day",
                    since=int((end - timedelta(days=29)).timestamp()), until=int(end.timestamp()))
        values = d["data"][0]["values"]
    except Exception as e:
        print(f"  warn: follower_count history unavailable: {e}", file=sys.stderr)
        return {}, {}
    est, raw, running = {}, {}, current
    for v in sorted(values, key=lambda v: v["end_time"], reverse=True):
        day = (parse_ts(v["end_time"]) - timedelta(days=1)).date().isoformat()  # end_time is the day's close
        est[day] = running
        raw[day] = v.get("value", 0)
        running -= v.get("value", 0)
    return est, raw


def follows_for_day(token, day):
    """Raw follows-and-unfollows numbers. Meta does not document how the follow_type split maps to
    follows versus unfollows, so store the raw values and let the data show what they mean."""
    since, until = day_window(day)
    out = {}
    d = api_get("me/insights", token, metric="follows_and_unfollows", period="day",
                metric_type="total_value", since=since, until=until)
    out["fu_total"] = d["data"][0]["total_value"].get("value", "")
    try:
        res = breakdown_for_day(token, "follows_and_unfollows", ["follow_type"], day)
        out["fu_follower"] = res.get("FOLLOWER", 0)
        out["fu_non_follower"] = res.get("NON_FOLLOWER", 0)
        out["fu_unknown"] = res.get("UNKNOWN", 0)
    except Exception as e:
        print(f"  warn: follows_and_unfollows breakdown for {day}: {e}", file=sys.stderr)
    return out


def collect_account(token, backfill=0):
    me = api_get("me", token, fields="username,followers_count,media_count")
    existing = {r["date"]: r for r in read_csv(DATA / "account_daily.csv")}
    fetched = iso(now_utc())
    today = now_utc().date()
    days = [today - timedelta(days=i) for i in range(max(backfill, 2) - 1, -1, -1)]
    est, raw = follower_estimates(token, me.get("followers_count", 0)) if backfill > 2 else ({}, {})
    for day in days:
        key = day.isoformat()
        is_recent = day >= today - timedelta(days=1)
        row = existing.get(key, {"date": key})
        if is_recent:
            row["followers"] = me.get("followers_count", row.get("followers", ""))
            row["media_count"] = me.get("media_count", row.get("media_count", ""))
        elif not row.get("followers") and key in est:
            row["followers"] = est[key]
        if key in raw:
            row["follower_count"] = raw[key]
        if is_recent or row.get("fu_total") in (None, ""):
            try:
                fu = follows_for_day(token, day)
                row.update(fu)
                if day == today:
                    print(f"  follows_and_unfollows sample for {key}: {fu}")
            except Exception as e:
                print(f"  warn: follows_and_unfollows for {key}: {e}", file=sys.stderr)
        for m in ACCOUNT_METRICS:
            if not is_recent and row.get(m) not in (None, ""):
                continue  # backfill only fills gaps
            try:
                row[m] = account_metric_for_day(token, m, day)
            except Exception as e:  # one unsupported metric must not kill the run
                print(f"  warn: account metric {m} for {key}: {e}", file=sys.stderr)
                row.setdefault(m, "")
        for metric, names, mapping in BREAKDOWNS:
            cols = sorted(set(mapping.values()))
            if not is_recent and row.get(cols[0]) not in (None, ""):
                continue
            try:
                res = breakdown_for_day(token, metric, names, day)
                if "FEED" in res:  # avoid double counting if both FEED and its subtypes come back
                    res.pop("POST", None)
                    res.pop("CAROUSEL_CONTAINER", None)
                for c in cols:
                    row[c] = 0
                for k, v in res.items():
                    if k in mapping:
                        row[mapping[k]] += v
            except Exception as e:
                print(f"  warn: {metric} breakdown {names[0]} for {key}: {e}", file=sys.stderr)
                for c in cols:
                    row.setdefault(c, "")
        row["fetched_at"] = fetched
        existing[key] = row
    write_csv(DATA / "account_daily.csv", ACCOUNT_FIELDS, sorted(existing.values(), key=lambda r: r["date"]))
    print(f"account: @{me.get('username')} followers={me.get('followers_count')} days={len(days)}")


# ---------- media metrics ----------

def parse_ts(ts):
    return datetime.strptime(ts.replace("+0000", "Z"), "%Y-%m-%dT%H:%M:%S%z")


def collect_media(token):
    data = api_get("me/media", token, limit=MAX_POSTS,
                   fields="id,caption,media_type,media_product_type,permalink,timestamp,like_count,comments_count")
    posts = data.get("data", [])[:MAX_POSTS]
    media = {r["id"]: r for r in read_csv(DATA / "media.csv")}
    metrics = read_csv(DATA / "media_metrics.csv")
    today = now_utc().date().isoformat()
    snapped_today = {r["id"] for r in metrics if r["fetched_at"].startswith(today)}
    fetched = iso(now_utc())
    cutoff = now_utc() - timedelta(days=RECENT_DAYS)
    new_rows = 0

    for p in posts:
        media[p["id"]] = {k: (p.get(k) or "").replace("\n", " ") if k == "caption" else p.get(k, "")
                          for k in MEDIA_FIELDS}
        if parse_ts(p["timestamp"]) < cutoff and p["id"] in snapped_today:
            continue  # older posts: one snapshot per day is enough
        row = {"id": p["id"], "fetched_at": fetched,
               "likes": p.get("like_count", ""), "comments": p.get("comments_count", "")}
        for api_name, col in MEDIA_METRICS.items():
            try:
                d = api_get(f"{p['id']}/insights", token, metric=api_name)
                row[col] = d["data"][0]["values"][0]["value"]
            except Exception as e:
                print(f"  warn: {api_name} for {p['id']}: {e}", file=sys.stderr)
                row[col] = ""
        metrics.append(row)
        new_rows += 1

    write_csv(DATA / "media.csv", MEDIA_FIELDS, sorted(media.values(), key=lambda r: r["timestamp"]))
    write_csv(DATA / "media_metrics.csv", METRIC_FIELDS, metrics)
    print(f"media: {len(posts)} posts, {new_rows} snapshots added")


# ---------- audience demographics ----------

def collect_audience(token):
    path = DATA / "audience.csv"
    rows = read_csv(path)
    today = now_utc().date().isoformat()
    if any(r["date"] == today for r in rows):
        return  # one snapshot per day is plenty
    new = []
    for b in ("country", "age", "gender"):
        try:
            d = api_get("me/insights", token, metric="follower_demographics", period="lifetime",
                        metric_type="total_value", breakdown=b, timeframe="this_month")
            res = d["data"][0]["total_value"]["breakdowns"][0]["results"]
            for r in sorted(res, key=lambda r: r.get("value", 0), reverse=True)[:15]:
                new.append({"date": today, "breakdown": b, "key": r["dimension_values"][0], "value": r.get("value", 0)})
        except Exception as e:
            print(f"  warn: follower demographics ({b}): {e}", file=sys.stderr)
    if new:
        write_csv(path, AUDIENCE_FIELDS, rows + new)
        print(f"audience: {len(new)} rows")


# ---------- token refresh ----------

def refresh_token(token):
    d = api_get("https://graph.instagram.com/refresh_access_token", token,
                grant_type="ig_refresh_token")
    return d["access_token"]


# ---------- demo data ----------

def make_demo():
    rnd = random.Random(7)
    today = now_utc().date()
    followers = 8200
    acct = []
    for i in range(90, -1, -1):
        d = today - timedelta(days=i)
        followers += rnd.randint(-8, 45)
        acct.append({"date": d.isoformat(), "followers": followers, "media_count": 120 + (90 - i) // 3,
                     "reach": rnd.randint(2500, 9000), "views": rnd.randint(6000, 24000),
                     "profile_views": rnd.randint(80, 420), "accounts_engaged": rnd.randint(180, 900),
                     "fetched_at": iso(now_utc())})
        a = acct[-1]
        a["reach_followers"] = int(a["reach"] * rnd.uniform(0.25, 0.5))
        a["reach_non_followers"] = a["reach"] - a["reach_followers"]
        a["views_followers"] = int(a["views"] * rnd.uniform(0.25, 0.5))
        a["views_non_followers"] = a["views"] - a["views_followers"]
        a["reach_reels"] = int(a["reach"] * 0.6)
        a["reach_posts"] = int(a["reach"] * 0.3)
        a["reach_stories"] = a["reach"] - a["reach_reels"] - a["reach_posts"]
        a["reposts"] = rnd.randint(0, 30)
    write_csv(DATA / "account_daily.csv", ACCOUNT_FIELDS, acct)
    aud = []
    for b, items in {"country": [("US", 31), ("GB", 18), ("DZ", 14), ("FR", 9), ("CA", 7)],
                     "age": [("18-24", 28), ("25-34", 41), ("35-44", 19), ("45-54", 8)],
                     "gender": [("F", 58), ("M", 40), ("U", 2)]}.items():
        aud += [{"date": today.isoformat(), "breakdown": b, "key": k, "value": v} for k, v in items]
    write_csv(DATA / "audience.csv", AUDIENCE_FIELDS, aud)

    kinds = [("REELS", "VIDEO", 2.4), ("FEED", "IMAGE", 1.0), ("FEED", "CAROUSEL_ALBUM", 1.5)]
    captions = ["Behind the scenes of today's shoot", "3 editing tricks nobody tells you",
                "Q&A: your questions answered", "My morning routine", "Gear I actually use",
                "Big announcement!", "How I plan a week of content", "Mistakes I made as a beginner"]
    media, metrics = [], []
    for n in range(40):
        ts = now_utc() - timedelta(days=n * 2.2, hours=rnd.randint(0, 23))
        prod, mtype, boost = rnd.choice(kinds)
        hour_boost = 1.4 if ts.hour in (18, 19, 20) else 1.0
        reach = int(rnd.randint(1500, 7000) * boost * hour_boost)
        pid = f"demo{n:03d}"
        media.append({"id": pid, "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S+0000"),
                      "media_type": mtype, "media_product_type": prod,
                      "permalink": "https://www.instagram.com/", "caption": rnd.choice(captions)})
        age_now = (now_utc() - ts).total_seconds() / 86400
        for age in [g for g in (0.25, 0.5, 1, 2, 3, 5, 7, 10, 14) if g < age_now] + [age_now]:
            r = int(reach * (1 - 2.718 ** (-age / 1.6)))
            metrics.append({"id": pid, "fetched_at": iso(ts + timedelta(days=age)),
                            "likes": int(r * 0.06), "comments": int(r * 0.004), "reach": r,
                            "views": int(r * 1.6), "saves": int(r * 0.012), "shares": int(r * 0.01),
                            "reposts": int(r * 0.004), "total_interactions": int(r * 0.09)})
    write_csv(DATA / "media.csv", MEDIA_FIELDS, sorted(media, key=lambda r: r["timestamp"]))
    write_csv(DATA / "media_metrics.csv", METRIC_FIELDS, metrics)
    print("demo data written to ./data")


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--refresh-token", action="store_true")
    ap.add_argument("--backfill", type=int, default=0, help="also fill account stats for the last N days")
    args = ap.parse_args()

    if args.demo:
        make_demo()
        return
    token = os.environ.get("IG_TOKEN")
    if not token:
        sys.exit("IG_TOKEN is not set")
    if args.refresh_token:
        print(refresh_token(token))
        return
    collect_account(token, args.backfill)
    collect_media(token)
    collect_audience(token)


if __name__ == "__main__":
    main()
