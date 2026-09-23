#!/usr/bin/env python3
"""유튜브 영상 → 요약 재료.

제미나이 키가 있으면 영상 자체를 보게 하고, 없으면 자막으로 대신한다.
둘 다 안 되면 제목·설명만 받고 그렇다고 분명히 알린다.

  python3 youtube_note.py "<URL>" [--out 자료/] [--model gemini-2.5-flash]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROMPT = """이 영상을 보고 아래를 한국어로 정리해라.

1. 한 줄 요약
2. 핵심 포인트 3~5개. 각각 앞에 [mm:ss] 타임스탬프를 붙인다.
   영상에서 실제로 확인한 시점만 적고, 모르면 타임스탬프를 비운다.
3. 이 내용이 실무에서 어디에 쓰이는지 2~3줄.
   "새로 나왔다"가 아니라 "이미 하고 있는 일 중 무엇이 쉬워지나"로 쓴다.

지어내지 마라. 영상에 없는 내용은 적지 않는다."""


def load_dotenv(start: Path) -> None:
    """.env 를 현재 폴더부터 위로 올라가며 찾아 환경변수에 넣는다(이미 있으면 유지)."""
    for folder in [start, *start.parents]:
        f = folder / ".env"
        if not f.is_file():
            continue
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))
        return


def video_id(url: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def meta(url: str) -> dict:
    """yt-dlp 로 제목·채널·길이를 받는다. 없으면 빈 값."""
    if not _has("yt-dlp"):
        return {}
    try:
        out = subprocess.run(
            ["yt-dlp", "-J", "--no-warnings", "--skip-download", url],
            capture_output=True, text=True, timeout=90,
        )
        if out.returncode != 0:
            return {}
        d = json.loads(out.stdout)
        return {
            "title": d.get("title", ""),
            "channel": d.get("uploader") or d.get("channel", ""),
            "duration": d.get("duration"),
            "upload_date": d.get("upload_date", ""),
            "description": (d.get("description") or "")[:2000],
        }
    except Exception:
        return {}


def subtitles(url: str) -> str:
    """자막을 받아 평문으로. 없으면 빈 문자열."""
    if not _has("yt-dlp"):
        return ""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            ["yt-dlp", "--skip-download", "--write-auto-subs", "--write-subs",
             "--sub-langs", "ko,en", "--sub-format", "vtt",
             "-o", f"{tmp}/s.%(ext)s", "--no-warnings", url],
            capture_output=True, text=True, timeout=180,
        )
        parts = []
        for vtt in sorted(Path(tmp).glob("*.vtt")):
            seen, cur = set(), []
            for line in vtt.read_text(encoding="utf-8", errors="replace").splitlines():
                if "-->" in line or line.startswith("WEBVTT") or not line.strip():
                    continue
                t = re.sub(r"<[^>]+>", "", line).strip()
                if t and t not in seen:
                    seen.add(t)
                    cur.append(t)
            if cur:
                parts.append(" ".join(cur))
        return "\n\n".join(parts)[:40000]


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def by_gemini(url: str, model: str) -> tuple[str, str]:
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    os.environ.pop("GOOGLE_API_KEY", None)  # 둘 다 있으면 SDK 가 경고를 뱉는다
    if not key:
        return "", "키 없음"
    try:
        import logging
        logging.getLogger("google_genai").setLevel(logging.ERROR)
        from google import genai
        from google.genai import types
    except ImportError:
        return "", "google-genai 미설치 (pip3 install google-genai)"
    try:
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model=model,
            contents=types.Content(parts=[
                types.Part(file_data=types.FileData(file_uri=url)),
                types.Part(text=PROMPT),
            ]),
        )
        return (resp.text or "").strip(), "gemini"
    except Exception as e:
        return "", f"제미나이 오류: {type(e).__name__} {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--out", default="자료/유튜브")
    ap.add_argument("--model", default="gemini-2.5-flash")
    a = ap.parse_args()

    load_dotenv(Path.cwd())

    if not video_id(a.url):
        print("유튜브 URL 로 보이지 않습니다:", a.url, file=sys.stderr)
        return 2

    m = meta(a.url)
    body, how = by_gemini(a.url, a.model)

    if not body:
        note = how
        subs = subtitles(a.url)
        desc = m.get("description", "")
        if subs:
            how = f"자막 (제미나이 못 씀: {note})"
            body = subs
        elif desc:
            how = f"제목·설명만 — 영상 내용 아님 (제미나이 못 씀: {note} / 자막 없음)"
            body = desc
        else:
            missing = []
            if "키 없음" in note or "미설치" in note:
                missing.append("제미나이 키 또는 google-genai")
            if not _has("yt-dlp"):
                missing.append("yt-dlp")
            print(json.dumps({
                "source": "가져온 것 없음",
                "error": f"영상에서 아무것도 못 가져왔습니다 ({note}).",
                "missing": missing,
                "hint": "요약하지 마세요. 준비물을 갖춘 뒤 다시 돌려야 합니다.",
                "meta": m,
            }, ensure_ascii=False, indent=2))
            return 3

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    print(json.dumps({
        "source": how,
        "meta": m,
        "content": body,
        "out_dir": str(out),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
