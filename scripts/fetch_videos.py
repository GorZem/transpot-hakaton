"""Download free-licensed pedestrian crossing videos from Wikimedia Commons
and transcode them to 720p H.264 mp4 for the demo / tests.

Usage: python scripts/fetch_videos.py [--out data/videos]
"""
import argparse
import json
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

UA = {"User-Agent": "transport-hackathon/1.0 (educational)"}

# (commons title, local name)
VIDEOS = [
    ("File:Piedira pasejo en strato Respubliko (Tjumeno).webm", "tyumen_respubliki.mp4"),
    ("File:Piedirejo en strato Herzen (Tjumeno).webm", "tyumen_herzen.mp4"),
    ("File:Piedirantoj ĉe kruciĝo de stratoj Respubliko kaj Orĝonikidze (Tjumeno).webm", "tyumen_ordzhonikidze.mp4"),
    ("File:Farthinder Brunnsjögatan Hedemora 20201124.webm", "hedemora.mp4"),
]


def info(title: str) -> dict:
    q = urllib.parse.urlencode({
        "action": "query", "format": "json", "prop": "imageinfo",
        "iiprop": "url|size|extmetadata", "titles": title,
    })
    req = urllib.request.Request("https://commons.wikimedia.org/w/api.php?" + q, headers=UA)
    page = next(iter(json.load(urllib.request.urlopen(req))["query"]["pages"].values()))
    return page["imageinfo"][0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/videos")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    credits = []
    for title, name in VIDEOS:
        dst = out / name
        ii = info(title)
        meta = ii["extmetadata"]
        credits.append(f"- {name}: {title} — {meta.get('LicenseShortName', {}).get('value')} — "
                       f"https://commons.wikimedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}")
        if dst.exists():
            print("skip", name)
            continue
        src = out / (name + ".src.webm")
        print("download", title)
        req = urllib.request.Request(ii["url"], headers=UA)
        with urllib.request.urlopen(req) as r, open(src, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        # scale longest side to 1280, keep aspect; drop audio
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
            "-vf", "scale='if(gt(iw,ih),1280,-2)':'if(gt(iw,ih),-2,1280)',fps=15",
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", str(dst),
        ], check=True)
        src.unlink()
    (out / "CREDITS.md").write_text("# Видео (Wikimedia Commons)\n\n" + "\n".join(credits) + "\n")
    print("done")


if __name__ == "__main__":
    main()
