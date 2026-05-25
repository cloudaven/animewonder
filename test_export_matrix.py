"""
Export matrix test — reproduces the 2026-05-24 4K/1080p smoke harness.

Runs against the LIVE deployed AnimeWonder so we exercise the actual
Render free-tier 512Mi memory cap. Each test posts a story to
/start-export and polls /export-status until done (or worker dies and
the status flips to not_found, which we treat as OOM).

Usage:
  python test_export_matrix.py
  python test_export_matrix.py --base https://animewonder.onrender.com
"""
import argparse
import json
import os
import sys
import time
import urllib.parse

import requests


BASE_DEFAULT = "https://animewonder.onrender.com"
ADMIN_USER   = "admin"
ADMIN_PASS   = "juste-monarch-Shadow-Dungeon-2026-x9K2pQ"


def make_story(title, scene_count):
    """Build a deterministic Claude-style story payload."""
    settings = [
        ("Crumbling stone archway at dusk",        "tense and silent"),
        ("Underground crystal cavern",             "luminous and ominous"),
        ("Rain-slicked rooftop overlooking city",  "lonely and reflective"),
        ("Ruined throne hall with shattered glass","apocalyptic dread"),
        ("Snow-covered forest clearing at dawn",   "quiet and sacred"),
        ("Skyship deck above storm clouds",        "exhilarating defiance"),
        ("Demon-marked alley beneath neon signs",  "predatory hush"),
    ]
    scenes = []
    for i in range(scene_count):
        setting, mood = settings[i % len(settings)]
        scenes.append({
            "title":        f"Scene {i+1}",
            "setting":      setting,
            "mood":         mood,
            "text":         (
                "The hunter advanced one careful step, breath visible in the cold. "
                "Shadows pulled tight against the walls. A faint pulse of mana "
                "shivered through the floor like a heart waking."
            ),
            "image_prompt": (
                f"anime hero in {setting}, cinematic lighting, ultra detailed, "
                "Solo Leveling style, A-1 Pictures quality"
            ),
        })
    return {"title": title, "scenes": scenes, "characters": []}


def login(base):
    s = requests.Session()
    r = s.post(f"{base}/login",
               data={"email": ADMIN_USER, "password": ADMIN_PASS},
               allow_redirects=False, timeout=30)
    if r.status_code not in (302, 303):
        sys.exit(f"login failed: {r.status_code}\n{r.text[:300]}")
    print(f"  [login] OK (cookie set)")
    return s


def run_case(s, base, label, story, quality, animate=False, anim_model=None, timeout_s=900):
    print(f"\n=== {label} ===")
    print(f"  scenes={len(story['scenes'])} quality={quality} animate={animate} anim_model={anim_model or '(default)'}")
    payload = {
        "story":      story,
        "mode":       "episode",
        "quality":    quality,
        "animate":    animate,
        "style":      "solo_leveling",
    }
    if anim_model:
        payload["anim_model"] = anim_model

    t0 = time.time()
    try:
        r = s.post(f"{base}/start-export", json=payload, timeout=60)
    except requests.RequestException as e:
        print(f"  [start] REQUEST FAILED: {e}")
        return {"label": label, "status": "request_failed", "elapsed": 0}
    if r.status_code != 200:
        print(f"  [start] HTTP {r.status_code}: {r.text[:300]}")
        return {"label": label, "status": "start_http_" + str(r.status_code), "elapsed": 0}
    info = r.json()
    job_id  = info["job_id"]
    server_q = info.get("quality")
    server_anim = info.get("anim_model")
    if server_q != quality:
        print(f"  [start] quality silently capped: requested={quality} got={server_q}")
    print(f"  [start] job_id={job_id} server_quality={server_q} server_anim={server_anim}")

    last_msg = ""
    not_found_streak = 0
    saw_progress = False  # once we've seen ANY non-not_found, treat fresh not_found as OOM
    while time.time() - t0 < timeout_s:
        try:
            r = s.get(f"{base}/export-status/{job_id}", timeout=30)
        except requests.RequestException as e:
            print(f"  [poll] transient: {e}")
            time.sleep(3)
            continue
        if r.status_code != 200:
            print(f"  [poll] HTTP {r.status_code}: {r.text[:200]}")
            time.sleep(3)
            continue
        st = r.json()
        status = st.get("status", "?")
        msg    = st.get("message", "")
        if msg != last_msg:
            print(f"  [{int(time.time()-t0):4d}s] {status}: {msg}")
            last_msg = msg
        # Server's terminal states: "complete" = success, "error" = failure.
        # "not_found" can mean: never started OR worker was OOM-killed mid-run
        # OR (post-success) Render's free tier put the idle worker to sleep
        # and wiped export_jobs. We MUST exit before the worker idles or we
        # mistake "asleep" for "OOM-killed".
        if status == "complete":
            elapsed = time.time() - t0
            size_b = st.get("file_size") or 0
            print(f"  [done] complete in {elapsed:.0f}s  file={st.get('file_name')}")
            return {"label": label, "status": "complete", "elapsed": elapsed,
                    "requested_q": quality, "server_q": server_q,
                    "file_name": st.get("file_name"), "file_size": size_b}
        if status == "error":
            elapsed = time.time() - t0
            print(f"  [done] ERROR after {elapsed:.0f}s: {msg}")
            return {"label": label, "status": "error", "elapsed": elapsed,
                    "requested_q": quality, "server_q": server_q, "msg": msg}
        if status == "not_found":
            not_found_streak += 1
            if saw_progress and not_found_streak >= 3:
                # Saw running state, now job is gone — worker died mid-export.
                elapsed = time.time() - t0
                print(f"  [done] OOM (worker restarted mid-run, job lost) after {elapsed:.0f}s")
                return {"label": label, "status": "oom", "elapsed": elapsed,
                        "requested_q": quality, "server_q": server_q}
        else:
            not_found_streak = 0
            saw_progress = True
        time.sleep(2)
    print(f"  [done] TIMEOUT after {timeout_s}s")
    return {"label": label, "status": "timeout", "elapsed": timeout_s}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE_DEFAULT)
    ap.add_argument("--only", default="", help="comma list of case labels to run; default all")
    args = ap.parse_args()

    print(f"target: {args.base}")
    print(f"date:   {time.strftime('%Y-%m-%d %H:%M:%S')}")
    s = login(args.base)

    cases = [
        # The 4K cap matrix: small/large stories at 1080p and 4k.
        ("smoke-2sc-1080p",    make_story("Smoke 2sc 1080p",    2), "1080p", False, None),
        ("smoke-2sc-4k-capped",make_story("Smoke 2sc 4k",        2), "4k",    False, None),
        ("real-5sc-1080p",     make_story("Real 5sc 1080p",      5), "1080p", False, None),
        ("real-5sc-4k-capped", make_story("Real 5sc 4k",         5), "4k",    False, None),
    ]
    if args.only:
        want = set(x.strip() for x in args.only.split(","))
        cases = [c for c in cases if c[0] in want]

    results = []
    for label, story, q, animate, am in cases:
        results.append(run_case(s, args.base, label, story, q, animate, am))

    print("\n" + "=" * 78)
    print(f"{'CASE':<25} {'STATUS':<10} {'REQ':<6} {'GOT':<6} {'TIME':>7}  FILE")
    print("=" * 78)
    for r in results:
        print(f"{r['label']:<25} {r['status']:<10} "
              f"{r.get('requested_q','-'):<6} {r.get('server_q','-') or '-':<6} "
              f"{r.get('elapsed',0):>6.0f}s  {r.get('file_name','')}")
    print("=" * 78)
    fails = [r for r in results if r["status"] not in ("done",)]
    if fails:
        print(f"\n{len(fails)} non-done outcomes — see above")
    else:
        print("\nall green")


if __name__ == "__main__":
    main()
