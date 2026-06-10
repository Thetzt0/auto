import json
import os
import requests
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from bot import generate_recap_script, get_duration, run_video_recap, word_range_for_video

APP_TITLE = "AutoRecap VPS"
PORT = int(os.getenv("PORT", "7860"))
MAX_VIDEO_MB = int(os.getenv("MAX_VIDEO_MB", "2000"))
CACHE_DIR = Path(os.getenv("CACHE_DIR", "./cache")).resolve()
VIDEOS_DIR = CACHE_DIR / "videos"
JOBS_DIR = CACHE_DIR / "jobs"
TMP_DIR = CACHE_DIR / "tmp"
for d in (CACHE_DIR, VIDEOS_DIR, JOBS_DIR, TMP_DIR):
    d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title=APP_TITLE)
app.mount("/assets", StaticFiles(directory=str(CACHE_DIR), html=False), name="assets")
_LOCK = threading.Lock()


def now_iso():
    mm_tz = timezone(timedelta(hours=6, minutes=30))
    return datetime.now(mm_tz).strftime("%Y-%m-%d %I:%M:%S %p")


def safe_name(name: str) -> str:
    name = Path(name or "video.mp4").name
    name = re.sub(r"[^A-Za-z0-9._\-\u1000-\u109F ]+", "_", name).strip()
    return name or "video.mp4"


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def video_meta_path(video_id: str) -> Path:
    return VIDEOS_DIR / f"{video_id}.json"


def video_file_path(video_id: str) -> Path:
    return VIDEOS_DIR / f"{video_id}.mp4"


def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def job_status_path(job_id: str) -> Path:
    return job_dir(job_id) / "status.json"


def get_job(job_id: str):
    return read_json(job_status_path(job_id), None)


def update_job(job_id: str, **updates):
    with _LOCK:
        data = get_job(job_id) or {"id": job_id}
        data.update(updates)
        data["updated_at"] = now_iso()
        write_json(job_status_path(job_id), data)
    return data


def create_job(kind: str, title: str, **extra):
    job_id = uuid.uuid4().hex[:12]
    data = {
        "id": job_id,
        "kind": kind,
        "title": title,
        "status": "queued",
        "progress": 0,
        "message": "Queued",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        **extra,
    }
    write_json(job_status_path(job_id), data)
    return job_id



def trigger_github_render(video_url, script_text, voice_name="my-MM-ThihaNeural", voice_speed="+40%"):
    repo = os.getenv("GITHUB_REPO", "Thetzt0/auto")
    workflow = os.getenv("GITHUB_WORKFLOW", "render.yml")
    token = os.getenv("GITHUB_TOKEN", "")
    branch = os.getenv("GITHUB_REF", "main")

    if not token:
        raise RuntimeError("GITHUB_TOKEN မရှိသေးပါ။")

    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches"

    r = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={
            "ref": branch,
            "inputs": {
                "video_url": video_url,
                "script_text": script_text,
                "voice_name": voice_name,
                "voice_speed": voice_speed,
            },
        },
        timeout=30,
    )

    if r.status_code != 204:
        raise RuntimeError(f"GitHub dispatch failed: {r.status_code} {r.text}")

    return True

class ProgressReporter:
    def __init__(self, job_id):
        self.job_id = job_id

    def __call__(self, value, desc=""):
        try:
            raw = int(max(0, min(1, float(value))) * 100)
        except Exception:
            raw = 0
        if raw <= 0:
            pct = 0
        elif raw < 20:
            pct = 10
        elif raw < 35:
            pct = 25
        elif raw < 55:
            pct = 40
        elif raw < 75:
            pct = 60
        elif raw < 90:
            pct = 80
        elif raw < 100:
            pct = 90
        else:
            pct = 100
        update_job(self.job_id, progress=pct, message=desc or "Working...")


def list_videos():
    items = []
    for meta_file in sorted(VIDEOS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        meta = read_json(meta_file, None)
        if not meta:
            continue
        path = video_file_path(meta.get("id", ""))
        if not path.exists():
            continue
        meta["size_mb"] = round(path.stat().st_size / (1024 * 1024), 1)
        meta["stream_url"] = f"/api/videos/{meta['id']}/file"
        items.append(meta)
    return items


def list_jobs(limit=50):
    jobs = []
    for status_file in sorted(JOBS_DIR.glob("*/status.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        data = read_json(status_file, None)
        if not data:
            continue
        jid = data.get("id")
        if data.get("kind") == "recap" and data.get("status") == "done":
            data["video_url"] = f"/api/jobs/{jid}/video"
            data["srt_url"] = f"/api/jobs/{jid}/srt"
        jobs.append(data)
        if len(jobs) >= limit:
            break
    return jobs


def save_video_file(src: Path, original_name: str, source: dict):
    video_id = uuid.uuid4().hex[:12]
    dst = video_file_path(video_id)
    shutil.copy(str(src), str(dst))
    size_mb = dst.stat().st_size / (1024 * 1024)
    if size_mb > MAX_VIDEO_MB:
        dst.unlink(missing_ok=True)
        raise HTTPException(status_code=413, detail=f"Video ကြီးလွန်းပါတယ် ({size_mb:.1f} MB). MAX_VIDEO_MB={MAX_VIDEO_MB}")
    duration = get_duration(str(dst))
    minutes, min_words, max_words = word_range_for_video(str(dst))
    meta = {
        "id": video_id,
        "name": safe_name(original_name),
        "source": source,
        "duration": duration,
        "duration_text": f"{duration/60:.1f} min" if duration > 0 else "unknown",
        "word_range": f"{min_words}-{max_words}",
        "size_mb": round(size_mb, 1),
        "created_at": now_iso(),
    }
    write_json(video_meta_path(video_id), meta)
    return meta


def parse_hhmmss(value: str) -> float:
    value = str(value or "").strip().replace(" ", "")
    if not re.fullmatch(r"\d{6}", value):
        raise ValueError("timestamp must be HHMMSS, e.g. 000000 or 000500")
    h, m, s = int(value[:2]), int(value[2:4]), int(value[4:6])
    if m >= 60 or s >= 60:
        raise ValueError("minute/second must be under 60")
    return h * 3600 + m * 60 + s


def sec_to_hms(sec: float) -> str:
    sec = max(0, int(round(sec)))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02}:{m:02}:{s:02}"


def normalize_youtube_url(url: str) -> str:
    url = str(url or "").strip().strip(" \t\r\n\"'`“”‘’<>()[]{}")
    m = re.search(r"https?://(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/[^\s\"'`<>]+", url)
    if m:
        url = m.group(0).strip(" \t\r\n\"'`“”‘’<>()[]{}")
    url = url.replace("https://m.youtube.com/", "https://www.youtube.com/")
    url = url.replace("http://m.youtube.com/", "https://www.youtube.com/")
    url = url.replace("http://www.youtube.com/", "https://www.youtube.com/")
    url = url.replace("https://youtube.com/", "https://www.youtube.com/")
    if "youtu.be/" in url:
        m = re.search(r"youtu\.be/([A-Za-z0-9_-]{6,})", url)
        if m:
            url = f"https://www.youtube.com/watch?v={m.group(1)}"
    return url


def download_youtube_segment(job_id: str, url: str, start_raw: str, end_raw: str, mode: str = "hd"):
    work = job_dir(job_id)
    work.mkdir(parents=True, exist_ok=True)
    mode = (mode or "hd").strip().lower()
    if mode not in ("hd", "fast"):
        mode = "hd"

    def add_common_ytdlp_args(outtmpl: str):
        base = [
            "python", "-m", "yt_dlp",
            "--no-playlist",
            "--force-ipv4",
            "--sleep-requests", "1",
            "--retries", "5",
            "--fragment-retries", "5",
            "-N", os.getenv("YOUTUBE_CONCURRENT_FRAGMENTS", "8"),
            "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
            "--merge-output-format", "mp4",
            "-o", outtmpl,
        ]

        deno = os.getenv("DENO_PATH", "").strip() or shutil.which("deno") or "/usr/local/bin/deno"
        if deno and os.path.exists(deno):
            base += ["--js-runtimes", f"deno:{deno}", "--remote-components", "ejs:github"]

        cookies_path = os.getenv("YOUTUBE_COOKIES_PATH", "").strip()
        if not cookies_path:
            for candidate in [
                "/home/user/app/secrets/cookies.txt",
                "/home/user/app/cookies.txt",
                str(CACHE_DIR / "youtube_cookies.txt"),
            ]:
                if os.path.exists(candidate):
                    cookies_path = candidate
                    break
        if cookies_path and os.path.exists(cookies_path):
            base += ["--cookies", cookies_path]

        extractor_args = os.getenv("YOUTUBE_EXTRACTOR_ARGS", "").strip()
        if extractor_args:
            base += ["--extractor-args", extractor_args]

        proxy = os.getenv("YOUTUBE_PROXY", "").strip()
        if proxy:
            base += ["--proxy", proxy]

        return base

    def cleanup_fast_temp():
        for pattern in ("source*", "cut_video_part_*", "youtube_fast*"):
            for tmp in work.glob(pattern):
                try:
                    if tmp.is_file():
                        tmp.unlink()
                except Exception:
                    pass

    try:
        update_job(job_id, status="running", progress=10, message=f"Preparing YouTube {mode.upper()} download...")
        url = normalize_youtube_url(url)
        if not url:
            raise RuntimeError("YouTube link ထည့်ပါ။")

        start = parse_hhmmss(start_raw)
        end = parse_hhmmss(end_raw)
        if end <= start:
            raise RuntimeError("End Timestamp က Start ထက်ကြီးရမယ်။")

        start_hms, end_hms = sec_to_hms(start), sec_to_hms(end)
        last_log = ""

        if mode == "fast":
            update_job(job_id, progress=25, message="Fast: downloading full source.mp4...")

            source_tmpl = str(work / "source.%(ext)s")
            fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
            cmd = add_common_ytdlp_args(source_tmpl) + ["-f", fmt, url]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=int(os.getenv("YTDLP_TIMEOUT", "1800")),
            )
            last_log = (result.stdout or "") + chr(10) + (result.stderr or "")

            sources = sorted(
                [p for p in work.glob("source*") if p.is_file() and p.suffix.lower() in (".mp4", ".m4v", ".webm", ".mkv")],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not sources:
                raise RuntimeError("source.mp4 download မတွေ့ပါ။")

            source_file = sources[0]
            cut_file = work / "cut_video_part_1.mp4"

            update_job(job_id, progress=70, message=f"Fast: cutting {start_hms} → {end_hms} with no render...")
            subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-ss", start_hms,
                "-to", end_hms,
                "-i", str(source_file),
                "-c", "copy",
                "-movflags", "+faststart",
                str(cut_file),
            ], check=True)

            if not cut_file.exists() or cut_file.stat().st_size <= 0:
                raise RuntimeError("cut output mp4 မထွက်ပါ။")

            update_job(job_id, progress=90, message="Saving fast cut to video list...")
            meta = save_video_file(
                cut_file,
                "youtube.mp4",
                {"type": "youtube", "mode": "fast", "url": url, "range": f"{start_hms}-{end_hms}"},
            )

            cleanup_fast_temp()
            update_job(job_id, status="done", progress=100, message="YouTube fast cut cached", result_video_id=meta["id"], video=meta, log=last_log[-2000:])
            return

        outtmpl = str(work / "youtube_download.%(ext)s")
        section = f"*{start_hms}-{end_hms}"
        update_job(job_id, progress=10, message=f"HD downloading {start_hms} → {end_hms}...")

        base = add_common_ytdlp_args(outtmpl)
        base += ["--download-sections", section, "--force-keyframes-at-cuts"]

        attempts = ["bestvideo*+bestaudio/best", "best[ext=mp4]/best", "worst[ext=mp4]/worst"]
        for idx, fmt in enumerate(attempts, 1):
            update_job(job_id, progress=[25, 40, 60][min(idx-1, 2)], message=f"HD yt-dlp attempt {idx}/{len(attempts)}...")
            cmd = base + ["-f", fmt, url]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=int(os.getenv("YTDLP_TIMEOUT", "1800")))
                last_log = (result.stdout or "") + chr(10) + (result.stderr or "")
                break
            except subprocess.CalledProcessError as e:
                last_log = (e.stdout or "") + chr(10) + (e.stderr or "")
                print(last_log[-5000:])
                if idx == len(attempts):
                    raise RuntimeError("YouTube HD download မအောင်မြင်ပါ။ Last log: " + last_log[-900:])

        downloaded = sorted([p for p in work.glob("youtube_download*") if p.is_file()], key=lambda p: p.stat().st_mtime, reverse=True)
        if not downloaded:
            raise RuntimeError("Downloaded file မတွေ့ပါ။")

        src = downloaded[0]

        if os.getenv("YOUTUBE_REENCODE", "0").lower() in ("1", "true", "yes", "on"):
            converted = work / "youtube_segment.mp4"
            yt_fps = os.getenv("YOUTUBE_FPS", "source").strip().lower()
            vf = []
            if yt_fps not in ("", "source", "original", "orig"):
                vf = ["-vf", f"fps={yt_fps}"]
            subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                *vf,
                "-c:v", "libx264",
                "-preset", os.getenv("YOUTUBE_PRESET", "fast"),
                "-crf", os.getenv("YOUTUBE_CRF", "18"),
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", os.getenv("YOUTUBE_AUDIO_BITRATE", "192k"),
                "-movflags", "+faststart",
                str(converted),
            ], check=True)
            src = converted
        elif src.suffix.lower() != ".mp4":
            converted = work / "youtube_segment.mp4"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", str(converted)], check=True)
            src = converted

        update_job(job_id, progress=90, message="Saving HD segment to video list...")
        meta = save_video_file(
            src,
            "youtube.mp4",
            {"type": "youtube", "mode": "hd", "url": url, "range": f"{start_hms}-{end_hms}"},
        )

        for pattern in ("youtube_download*", "youtube_segment.mp4"):
            for tmp in work.glob(pattern):
                try:
                    if tmp.is_file():
                        tmp.unlink()
                except Exception:
                    pass

        update_job(job_id, status="done", progress=100, message="YouTube HD segment cached", result_video_id=meta["id"], video=meta, log=last_log[-2000:])

    except Exception as e:
        update_job(job_id, status="error", progress=100, message=str(e), traceback=traceback.format_exc())


def generate_script_job(job_id: str, video_id: str, gemini_model: str = ''):
    try:
        path = video_file_path(video_id)
        if not path.exists():
            raise RuntimeError("Selected video မတွေ့ပါ။")
        update_job(job_id, status="running", progress=10, message="Starting script generation...")
        script = generate_recap_script(str(path), progress=ProgressReporter(job_id), gemini_model=gemini_model)
        out = job_dir(job_id) / "script.txt"
        out.write_text(script, encoding="utf-8")
        update_job(job_id, status="done", progress=100, message="Script generated", script_text=script, video_id=video_id)
    except Exception as e:
        update_job(job_id, status="error", progress=100, message=str(e), traceback=traceback.format_exc())


def generate_recap_job(job_id: str, video_id: str, script_text: str, voice_name: str, voice_speed: str, gemini_model: str = ''):
    try:
        path = video_file_path(video_id)
        if not path.exists():
            raise RuntimeError("Selected video မတွေ့ပါ။")
        work = job_dir(job_id)
        work.mkdir(parents=True, exist_ok=True)
        (work / "script.txt").write_text(script_text, encoding="utf-8")
        update_job(job_id, status="running", progress=10, message="Starting recap render...", video_id=video_id)
        output_video, output_srt = run_video_recap(
            video_path=str(path),
            script_text=script_text,
            output_dir=str(work / "work"),
            voice_name=voice_name,
            voice_speed=voice_speed,
            progress=ProgressReporter(job_id),
            gemini_model=gemini_model,
        )
        final_video = work / "final_recap.mp4"
        final_srt = work / "subtitles.srt"
        shutil.copy(output_video, final_video)
        shutil.copy(output_srt, final_srt)
        update_job(job_id, status="done", progress=100, message="Recap video ready", video_id=video_id)
    except Exception as e:
        update_job(job_id, status="error", progress=100, message=str(e), traceback=traceback.format_exc())


def start_thread(target, *args):
    t = threading.Thread(target=target, args=args, daemon=True)
    t.start()


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


@app.get("/api/status")
def api_status():
    return {"videos": list_videos(), "jobs": list_jobs()}


@app.post("/api/upload")
async def api_upload(video: UploadFile = File(...)):
    if not video.filename.lower().endswith(".mp4"):
        raise HTTPException(status_code=400, detail="MP4 file ပဲတင်ပါ။")
    tmp = TMP_DIR / f"upload_{uuid.uuid4().hex}.mp4"
    with tmp.open("wb") as f:
        while True:
            chunk = await video.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
    meta = save_video_file(tmp, video.filename, {"type": "upload"})
    tmp.unlink(missing_ok=True)
    return meta


@app.post("/api/delete-video/{video_id}")
def api_delete_video(video_id: str):
    video_file_path(video_id).unlink(missing_ok=True)
    video_meta_path(video_id).unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/videos/{video_id}/file")
def api_video_file(video_id: str):
    path = video_file_path(video_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Video not found")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.post("/api/youtube")
def api_youtube(url: str = Form(...), start: str = Form(...), end: str = Form(...), mode: str = Form("hd")):
    mode = (mode or "hd").strip().lower()
    if mode not in ("hd", "fast"):
        mode = "hd"
    job_id = create_job("youtube", f"YouTube {mode.upper()} download", url=normalize_youtube_url(url), start=start, end=end, mode=mode)
    start_thread(download_youtube_segment, job_id, url, start, end, mode)
    return {"job_id": job_id}

@app.post("/api/script")
def api_script(video_id: str = Form(...), gemini_model: str = Form("models/gemini-2.5-flash")):
    job_id = create_job("script", "Script generation", video_id=video_id, gemini_model=gemini_model)
    start_thread(generate_script_job, job_id, video_id, gemini_model)
    return {"job_id": job_id}


@app.post("/api/generate")
def api_generate(video_id: str = Form(...), script_text: str = Form(...), voice_name: str = Form("my-MM-ThihaNeural"), voice_speed: str = Form("+40%"), gemini_model: str = Form("models/gemini-2.5-flash")):
    if not script_text.strip():
        raise HTTPException(status_code=400, detail="Script text မရှိသေးပါ။")
    job_id = create_job("recap", "Final recap render", video_id=video_id, voice_name=voice_name, voice_speed=voice_speed, gemini_model=gemini_model)
    base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if not base_url:
        raise HTTPException(status_code=500, detail="PUBLIC_BASE_URL မသတ်မှတ်ရသေးပါ။")

    video_url = f"{base_url}/api/videos/{video_id}/file"
    trigger_github_render(video_url, script_text, voice_name, voice_speed)
    update_job(job_id, status="running", progress=10, message="GitHub Actions render started", github_render=True)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    if data.get("kind") == "recap" and data.get("status") == "done":
        data["video_url"] = f"/api/jobs/{job_id}/video"
        data["srt_url"] = f"/api/jobs/{job_id}/srt"
    return data


@app.get("/api/jobs/{job_id}/video")
def api_job_video(job_id: str):
    path = job_dir(job_id) / "final_recap.mp4"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Final video not found")
    return FileResponse(path, media_type="video/mp4", filename=f"recap_{job_id}.mp4")


@app.get("/api/jobs/{job_id}/srt")
def api_job_srt(job_id: str):
    path = job_dir(job_id) / "subtitles.srt"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Subtitles not found")
    return FileResponse(path, media_type="text/plain", filename=f"subtitles_{job_id}.srt")


@app.post("/api/delete-job/{job_id}")
def api_delete_job(job_id: str):
    shutil.rmtree(job_dir(job_id), ignore_errors=True)
    return {"ok": True}



# =========================
# YouTube Trending Channels
# =========================
TRENDING_DIR = CACHE_DIR / "trending"
TRENDING_LOGO_DIR = TRENDING_DIR / "logos"
TRENDING_VIDEO_DIR = TRENDING_DIR / "videos"
TRENDING_CHANNELS_FILE = TRENDING_DIR / "channels.json"
for d in (TRENDING_DIR, TRENDING_LOGO_DIR, TRENDING_VIDEO_DIR):
    d.mkdir(parents=True, exist_ok=True)

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"


def _yt_api(path: str, params: dict):
    key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="YOUTUBE_API_KEY မရှိသေးပါ။ .env ထဲထည့်ပါ။")
    params = dict(params)
    params["key"] = key
    r = requests.get(f"{YOUTUBE_API_BASE}/{path}", params=params, timeout=25)
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", r.text[:500])
        except Exception:
            msg = r.text[:500]
        raise HTTPException(status_code=400, detail="YouTube API error: " + msg)
    return r.json()


def _parse_yt_time(value: str):
    value = (value or "").replace("Z", "+00:00")
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _read_channels():
    data = read_json(TRENDING_CHANNELS_FILE, [])
    return data if isinstance(data, list) else []


def _write_channels(channels):
    clean = []
    seen = set()
    for c in channels:
        cid = c.get("id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        clean.append(c)
    write_json(TRENDING_CHANNELS_FILE, clean)
    return clean


def _channel_logo_asset(channel_id: str):
    for ext in ("jpg", "png", "webp"):
        f = TRENDING_LOGO_DIR / f"{channel_id}.{ext}"
        if f.exists():
            return f"/assets/trending/logos/{channel_id}.{ext}"
    return ""


def _save_channel_logo(channel_id: str, logo_url: str):
    if not logo_url:
        return ""
    try:
        r = requests.get(logo_url, timeout=25)
        r.raise_for_status()
        ctype = (r.headers.get("content-type") or "").lower()
        ext = "png" if "png" in ctype else ("webp" if "webp" in ctype else "jpg")
        out = TRENDING_LOGO_DIR / f"{channel_id}.{ext}"
        out.write_bytes(r.content)
        return f"/assets/trending/logos/{channel_id}.{ext}"
    except Exception:
        return _channel_logo_asset(channel_id) or logo_url


def _resolve_channel_id(channel_input: str):
    text = (channel_input or "").strip()
    m = re.search(r"(UC[A-Za-z0-9_-]{20,})", text)
    if m:
        return m.group(1)

    handle = None
    m = re.search(r"youtube\.com/@([A-Za-z0-9._-]+)", text)
    if not m:
        m = re.search(r"^@([A-Za-z0-9._-]+)$", text)
    if m:
        handle = "@" + m.group(1).lstrip("@")

    if handle:
        data = _yt_api("channels", {"part": "snippet,contentDetails,statistics", "forHandle": handle})
        items = data.get("items") or []
        if items:
            return items[0]["id"]

    if text.startswith("http"):
        try:
            html = requests.get(text, timeout=25, headers={"User-Agent": "Mozilla/5.0"}).text
            m = re.search(r'"channelId":"(UC[A-Za-z0-9_-]{20,})"', html) or re.search(r'"browseId":"(UC[A-Za-z0-9_-]{20,})"', html)
            if m:
                return m.group(1)
        except Exception:
            pass

    raise HTTPException(status_code=400, detail="Channel link/ID မတွေ့ပါ။ youtube.com/@handle သို့ channel/UC... link ထည့်ပါ။")


def _get_channel_info(channel_id: str):
    data = _yt_api("channels", {"part": "snippet,contentDetails,statistics", "id": channel_id})
    items = data.get("items") or []
    if not items:
        raise HTTPException(status_code=404, detail="Channel မတွေ့ပါ။")
    item = items[0]
    sn = item.get("snippet", {})
    thumbs = sn.get("thumbnails", {})
    logo_url = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
    uploads = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")
    return {
        "id": item["id"],
        "title": sn.get("title") or item["id"],
        "description": sn.get("description", ""),
        "url": f"https://www.youtube.com/channel/{item['id']}",
        "uploads_playlist": uploads,
        "logo_url": logo_url,
        "logo_asset": _save_channel_logo(item["id"], logo_url),
        "subscribers": int(item.get("statistics", {}).get("subscriberCount", 0) or 0),
        "added_at": now_iso(),
        "last_refreshed": "",
    }


def _cutoff_for_period(period: str):
    period = (period or "3d").lower()
    now = datetime.now(timezone.utc)
    hours_map = {
        "12h": 12,
        "24h": 24,
        "2d": 48,
        "3d": 72,
        "5d": 120,
        "7d": 168,
    }
    return now - timedelta(hours=hours_map.get(period, 72))


def _trending_periods():
    return ["12h", "24h", "2d", "3d", "5d", "7d"]


def _video_cache_path(channel_id: str, period: str):
    safe_period = re.sub(r"[^A-Za-z0-9_-]", "", period or "3d") or "3d"
    return TRENDING_VIDEO_DIR / f"{channel_id}_{safe_period}.json"


def _master_video_cache_path(channel_id: str):
    return TRENDING_VIDEO_DIR / f"{channel_id}_master_7d.json"


def _get_recent_video_ids(uploads_playlist: str, cutoff_time):
    video_ids = []
    page_token = None
    pages = 0
    max_pages = int(os.getenv("TRENDING_MAX_PAGES", "50"))
    max_ids = int(os.getenv("TRENDING_MAX_IDS", "2500"))

    while True:
        pages += 1
        params = {
            "part": "snippet,contentDetails",
            "playlistId": uploads_playlist,
            "maxResults": 50,
        }
        if page_token:
            params["pageToken"] = page_token

        data = _yt_api("playlistItems", params)
        should_stop = False

        for item in data.get("items", []):
            published = item.get("contentDetails", {}).get("videoPublishedAt") or item.get("snippet", {}).get("publishedAt")
            if not published:
                continue

            try:
                published_at = _parse_yt_time(published)
            except Exception:
                continue

            if published_at >= cutoff_time:
                vid = item.get("contentDetails", {}).get("videoId")
                if vid:
                    video_ids.append(vid)
            else:
                should_stop = True
                break

            if len(video_ids) >= max_ids:
                should_stop = True
                break

        if should_stop or pages >= max_pages:
            break

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return list(dict.fromkeys(video_ids))


def _get_video_details(video_ids):
    videos = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        if not batch:
            continue

        data = _yt_api("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(batch)})
        for item in data.get("items", []):
            sn = item.get("snippet", {})
            st = item.get("statistics", {})
            thumbs = sn.get("thumbnails", {})
            thumb = (thumbs.get("maxres") or thumbs.get("standard") or thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")

            videos.append({
                "id": item["id"],
                "title": sn.get("title", ""),
                "views": int(st.get("viewCount", 0) or 0),
                "likes": int(st.get("likeCount", 0) or 0),
                "published_at": sn.get("publishedAt", ""),
                "thumbnail": thumb,
                "url": f"https://www.youtube.com/watch?v={item['id']}",
            })

    videos.sort(key=lambda x: int(x.get("views", 0) or 0), reverse=True)
    return videos


def _filter_sort_videos_for_period(videos, period: str, limit: int = 10):
    cutoff = _cutoff_for_period(period)
    out = []

    for v in videos or []:
        try:
            published = _parse_yt_time(v.get("published_at", ""))
        except Exception:
            continue

        if published >= cutoff:
            out.append(v)

    out.sort(key=lambda x: int(x.get("views", 0) or 0), reverse=True)
    return out[:max(1, min(int(limit or 10), 50))]


def _refresh_channel_master(channel_id: str):
    channels = _read_channels()
    ch = next((c for c in channels if c.get("id") == channel_id), None)
    if not ch:
        ch = _get_channel_info(channel_id)
        channels.append(ch)

    uploads = ch.get("uploads_playlist")
    if not uploads:
        ch2 = _get_channel_info(channel_id)
        uploads = ch2.get("uploads_playlist")
        ch.update(ch2)

    ids = _get_recent_video_ids(uploads, _cutoff_for_period("7d"))
    videos = _get_video_details(ids)
    videos.sort(key=lambda x: int(x.get("views", 0) or 0), reverse=True)

    refreshed_at = now_iso()
    master = {
        "channel_id": channel_id,
        "period": "master_7d",
        "refreshed_at": refreshed_at,
        "total": len(videos),
        "videos": videos,
    }
    write_json(_master_video_cache_path(channel_id), master)

    for period in _trending_periods():
        pv = _filter_sort_videos_for_period(videos, period, 50)
        write_json(_video_cache_path(channel_id, period), {
            "channel_id": channel_id,
            "period": period,
            "refreshed_at": refreshed_at,
            "total": len(pv),
            "videos": pv,
        })

    for c in channels:
        if c.get("id") == channel_id:
            c["last_refreshed"] = refreshed_at
    _write_channels(channels)

    return master


def _get_period_payload(channel_id: str, period: str, limit: int = 10, refresh: bool = False):
    period = period if period in _trending_periods() else "3d"
    limit = max(1, min(int(limit or 10), 50))
    master_path = _master_video_cache_path(channel_id)

    if refresh or not master_path.exists():
        master = _refresh_channel_master(channel_id)
    else:
        master = read_json(master_path, None)
        if not master:
            master = _refresh_channel_master(channel_id)

    videos = _filter_sort_videos_for_period(master.get("videos") or [], period, limit)
    total = len(_filter_sort_videos_for_period(master.get("videos") or [], period, 50))

    return {
        "channel_id": channel_id,
        "period": period,
        "limit": limit,
        "total": total,
        "refreshed_at": master.get("refreshed_at", ""),
        "videos": videos,
    }


def _refresh_channel_all_periods(channel_id: str, limit: int = 10):
    _refresh_channel_master(channel_id)
    return {period: _get_period_payload(channel_id, period, limit, refresh=False) for period in _trending_periods()}


def _refresh_channel_videos(channel_id: str, period: str, limit: int = 20):
    return _get_period_payload(channel_id, period, limit, refresh=True)


@app.get("/trending", response_class=HTMLResponse)
def trending_page():
    return TRENDING_HTML


@app.get("/api/trending/channels")
def api_trending_channels():
    return {"channels": _read_channels()}


@app.post("/api/trending/add")
def api_trending_add(channel: str = Form(...)):
    channel_id = _resolve_channel_id(channel)
    info = _get_channel_info(channel_id)
    channels = _read_channels()
    channels = [c for c in channels if c.get("id") != channel_id]
    channels.insert(0, info)
    _write_channels(channels)
    return {"ok": True, "channel": info, "channels": channels}


@app.get("/api/trending/videos")
def api_trending_videos(channel_id: str, period: str = "3d", limit: int = 10, refresh: int = 0):
    period = period if period in _trending_periods() else "3d"
    limit = max(1, min(int(limit or 10), 50))
    return _get_period_payload(channel_id, period, limit, refresh=bool(refresh))

@app.post("/api/trending/refresh")
def api_trending_refresh(limit: int = Form(10)):
    out = []
    for ch in _read_channels():
        try:
            out.append({
                "channel_id": ch.get("id"),
                "ok": True,
                "periods": _refresh_channel_all_periods(ch["id"], limit),
            })
        except Exception as e:
            out.append({"channel_id": ch.get("id"), "ok": False, "error": str(e)})

    return {"ok": True, "results": out}

TRENDING_HTML = r"""
<!doctype html>
<html lang="my">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>YouTube Trending</title>
<style>
:root{--bg:#0b1020;--panel:#121a2e;--text:#e8eefc;--muted:#93a4c4;--accent:#27d5ff;--accent2:#7c5cff;--danger:#ff5d6c;--border:#24304d}*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#08111f,#111b35);font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;color:var(--text)}header{position:sticky;top:0;z-index:9;background:rgba(8,12,25,.9);backdrop-filter:blur(14px);border-bottom:1px solid var(--border);padding:13px 16px;display:flex;gap:12px;align-items:center}.brand{font-weight:900;font-size:19px}.back{color:#07101c;background:var(--accent);border-radius:999px;padding:8px 12px;text-decoration:none;font-weight:800}.wrap{max-width:1180px;margin:0 auto;padding:16px}.card{background:rgba(18,26,46,.92);border:1px solid var(--border);border-radius:18px;padding:16px;margin-bottom:16px}label{display:block;font-size:12px;color:var(--muted);margin:8px 0 6px}input,select{width:100%;background:#0b1224;border:1px solid #2b3858;color:var(--text);border-radius:12px;padding:11px;outline:none}button{border:0;border-radius:12px;padding:11px 14px;font-weight:800;cursor:pointer;color:#07101c;background:var(--accent)}button.secondary{background:#263554;color:var(--text)}.row{display:flex;gap:10px;align-items:end}.row>*{flex:1}@media(max-width:760px){.row{display:block}.row button{width:100%;margin-top:8px}}.channels{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}@media(max-width:760px){.channels{grid-template-columns:repeat(3,1fr);gap:8px}}.channel{background:#0d1629;border:1px solid var(--border);border-radius:16px;padding:12px;text-align:center;cursor:pointer;transition:.15s}.channel:hover,.channel.active{border-color:var(--accent);box-shadow:0 0 0 2px rgba(39,213,255,.15);transform:translateY(-1px)}.avatar{width:76px;height:76px;border-radius:50%;object-fit:cover;border:2px solid #2b3858;background:#050914}.chname{font-size:13px;font-weight:800;margin-top:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.small{font-size:12px;color:var(--muted)}.filters{display:flex;gap:10px;align-items:end;margin-bottom:12px}.filters>*{flex:1}.videos{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}.video{background:#0d1629;border:1px solid var(--border);border-radius:16px;overflow:hidden}.thumb{width:100%;aspect-ratio:16/9;object-fit:cover;background:#050914;display:block}.vbody{padding:10px}.title{font-size:13px;font-weight:800;line-height:1.35}.meta{font-size:12px;color:var(--muted);margin-top:6px}.toast{position:fixed;right:16px;bottom:16px;background:#101a31;border:1px solid var(--border);padding:12px 14px;border-radius:14px;max-width:390px;z-index:99}.hidden{display:none}.loading{opacity:.65;pointer-events:none}
</style>
</head>
<body>
<header><a class="back" href="/">← AutoRecap</a><div class="brand">🔥 YouTube Trending</div></header>
<div class="wrap">
  <section class="card">
    <div class="row">
      <div><label>YouTube Channel Link / @handle / Channel ID</label><input id="channelInput" placeholder="https://www.youtube.com/@channel or UC..." /></div>
      <button onclick="addChannel()">Add</button>
      <button class="secondary" onclick="refreshAllChannels()">Refresh</button>
    </div>
    <div class="small" style="margin-top:8px">Refresh မနှိပ်ရင် cache ထဲက အရင် trending result ကိုပဲပြမယ်။ Logo တွေကို VPS မှာသိမ်းထားပါတယ်။</div>
  </section>

  <section class="card">
    <div class="row" style="align-items:center;margin-bottom:12px"><h3 style="margin:0">Channels</h3><div class="small" id="channelCount"></div></div>
    <div id="channels" class="channels"></div>
  </section>

  <section class="card">
    <div class="filters">
      <div><label>Time</label><select id="period" onchange="loadVideos(false)"><option value="12h">12 hr</option><option value="24h">24 hr</option><option value="2d">2 day</option><option value="3d" selected>3 day</option><option value="5d">5 day</option><option value="7d">7 day</option></select></div>
      <div><label>Max videos</label><select id="limit" onchange="loadVideos(false)"><option selected>10</option><option>20</option></select></div>
      <button onclick="loadVideos(true)">Refresh</button>
    </div>
    <div id="selected" class="small">Channel တစ်ခုနှိပ်ပါ။</div>
    <div id="videos" class="videos" style="margin-top:12px"></div>
  </section>
</div>
<div id="toast" class="toast hidden"></div>
<script>
let channels=[]; let selectedId=localStorage.getItem('trendChannelId')||'';
function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.remove('hidden');setTimeout(()=>t.classList.add('hidden'),4500)}
async function api(path, opts={}){
  const r = await fetch(path, opts);
  const txt = await r.text();
  let data = {};
  try {
    data = txt ? JSON.parse(txt) : {};
  } catch (_) {
    data = {detail: txt || 'Request failed'};
  }
  if (!r.ok) {
    throw new Error(data.detail || data.error || 'Request failed');
  }
  return data;
}
function fmtViews(n){n=Number(n||0); if(n>=1000000)return (n/1000000).toFixed(1)+'M views'; if(n>=1000)return (n/1000).toFixed(1)+'K views'; return n+' views'}
function ago(t){const d=new Date(t); if(isNaN(d))return ''; const s=Math.max(0,Math.floor((Date.now()-d.getTime())/1000)); if(s<60)return s+' sec ago'; const m=Math.floor(s/60); if(m<60)return m+' min ago'; const h=Math.floor(m/60); if(h<24)return h+' hr ago'; const day=Math.floor(h/24); if(day<30)return day+(day===1?' day ago':' days ago'); const mo=Math.floor(day/30); if(mo<12)return mo+(mo===1?' month ago':' months ago'); const y=Math.floor(mo/12); return y+(y===1?' year ago':' years ago')}
function renderChannels(){const box=document.getElementById('channels'); box.innerHTML=''; document.getElementById('channelCount').textContent=channels.length+' channels'; if(!channels.length){box.innerHTML='<div class="small">Channel မရှိသေးပါ။ Add လုပ်ပါ။</div>';return} if(!selectedId)selectedId=channels[0].id; channels.forEach(c=>{const d=document.createElement('div'); d.className='channel '+(c.id===selectedId?'active':''); d.onclick=()=>{selectedId=c.id;localStorage.setItem('trendChannelId',selectedId);renderChannels();loadVideos(false)}; d.innerHTML=`<img class="avatar" src="${c.logo_asset||c.logo_url||''}" onerror="this.style.display='none'"><div class="chname">${c.title}</div>`; box.appendChild(d)});}
async function loadChannels(){try{const d=await api('/api/trending/channels'); channels=d.channels||[]; renderChannels(); if(selectedId)loadVideos(false)}catch(e){toast(e.message)}}
async function addChannel(){const input=document.getElementById('channelInput'); const v=input.value.trim(); if(!v){toast('Channel link ထည့်ပါ');return} try{document.body.classList.add('loading'); const fd=new FormData(); fd.append('channel',v); const d=await api('/api/trending/add',{method:'POST',body:fd}); channels=d.channels||[]; selectedId=d.channel.id; localStorage.setItem('trendChannelId',selectedId); input.value=''; renderChannels(); await loadVideos(true); toast('Channel added')}catch(e){toast(e.message)}finally{document.body.classList.remove('loading')}}
async function refreshAllChannels(){try{document.body.classList.add('loading'); const fd=new FormData(); fd.append('limit',document.getElementById('limit').value); await api('/api/trending/refresh',{method:'POST',body:fd}); await loadChannels(); if(selectedId)await loadVideos(false); toast('All channels refreshed')}catch(e){toast(e.message)}finally{document.body.classList.remove('loading')}}
async function loadVideos(refresh){if(!selectedId){return} const ch=channels.find(x=>x.id===selectedId); document.getElementById('selected').textContent=ch?('Selected: '+ch.title):selectedId; const p=document.getElementById('period').value; const l=document.getElementById('limit').value; try{document.body.classList.add('loading'); const d=await api(`/api/trending/videos?channel_id=${encodeURIComponent(selectedId)}&period=${encodeURIComponent(p)}&limit=${l}&refresh=${refresh?1:0}`); renderVideos(d.videos||[]); if(refresh) await loadChannels();}catch(e){toast(e.message); renderVideos([])}finally{document.body.classList.remove('loading')}}
function renderVideos(videos){const box=document.getElementById('videos'); box.innerHTML=''; if(!videos.length){box.innerHTML='<div class="small">ဒီ time range ထဲ video မတွေ့သေးပါ။ Refresh နှိပ်ကြည့်ပါ။</div>';return} videos.forEach((v,i)=>{const d=document.createElement('div'); d.className='video'; d.innerHTML=`<a href="${v.url}" target="_blank"><img class="thumb" src="${v.thumbnail||''}"></a><div class="vbody"><div class="title">${i+1}. ${v.title}</div><div class="meta">${fmtViews(v.views)} · ${ago(v.published_at)}</div></div>`; box.appendChild(d)})}
loadChannels();
</script>
</body>
</html>
"""

HTML = r"""
<!doctype html>
<html lang="my">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>AutoRecap VPS</title>
<style>
:root{--bg:#0b1020;--panel:#121a2e;--panel2:#17233d;--text:#e8eefc;--muted:#93a4c4;--accent:#27d5ff;--accent2:#7c5cff;--danger:#ff5d6c;--ok:#29d98d;--warn:#ffd166;--border:#24304d}*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#08111f,#0d1022 45%,#111b35);font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;color:var(--text)}header{position:sticky;top:0;z-index:10;background:rgba(8,12,25,.85);backdrop-filter:blur(14px);border-bottom:1px solid var(--border);padding:14px 18px;display:flex;gap:12px;align-items:center;justify-content:space-between}.brand{font-weight:800;font-size:20px;letter-spacing:.3px}.badge{font-size:12px;color:#00131a;background:var(--accent);padding:5px 9px;border-radius:999px}.wrap{max-width:1220px;margin:0 auto;padding:18px}.grid{display:grid;grid-template-columns:360px 1fr;gap:16px}@media(max-width:900px){.grid{grid-template-columns:1fr}}.card{background:rgba(18,26,46,.92);border:1px solid var(--border);border-radius:18px;padding:16px;box-shadow:0 12px 28px rgba(0,0,0,.22)}h2{font-size:16px;margin:0 0 12px}label{display:block;font-size:12px;color:var(--muted);margin:10px 0 6px}input,textarea,select{width:100%;background:#0b1224;border:1px solid #2b3858;color:var(--text);border-radius:12px;padding:11px;outline:none}textarea{min-height:290px;resize:vertical;line-height:1.6}button{border:0;border-radius:12px;padding:11px 14px;font-weight:700;cursor:pointer;color:#07101c;background:var(--accent);transition:.15s}button:hover{filter:brightness(1.08);transform:translateY(-1px)}button.secondary{background:#263554;color:var(--text)}button.danger{background:var(--danger);color:white}button.ok{background:var(--ok)}button.purple{background:var(--accent2);color:white}.row{display:flex;gap:10px;align-items:center}.row>*{flex:1}.video-list{display:flex;flex-direction:column;gap:10px;max-height:420px;overflow:auto}.video-item{padding:12px;border:1px solid #263655;border-radius:14px;background:#0d1629;cursor:pointer;display:grid;grid-template-columns:1fr auto;gap:10px;align-items:center}.video-item.active{border-color:var(--accent);box-shadow:0 0 0 2px rgba(39,213,255,.16)}.meta{font-size:12px;color:var(--muted);margin-top:4px}.small{font-size:12px;color:var(--muted)}.status{padding:10px 12px;border-radius:12px;background:#0b1224;border:1px solid var(--border);min-height:42px}.progress{height:8px;border-radius:999px;background:#0b1224;overflow:hidden;border:1px solid var(--border)}.bar{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));width:0%}.jobs{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}.job{background:#0d1629;border:1px solid var(--border);border-radius:14px;padding:12px}.job .title{font-weight:800}.pill{font-size:11px;border-radius:999px;padding:4px 8px;display:inline-block;background:#263554;color:var(--text)}.pill.done{background:rgba(41,217,141,.15);color:var(--ok)}.pill.error{background:rgba(255,93,108,.15);color:var(--danger)}.pill.running{background:rgba(255,209,102,.15);color:var(--warn)}a{color:var(--accent);text-decoration:none}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}video{width:100%;max-height:260px;border-radius:14px;background:#000;border:1px solid var(--border)}.tabs{display:flex;gap:8px;margin-bottom:12px}.tab{background:#202d49;color:var(--text)}.tab.active{background:var(--accent);color:#07101c}.hidden{display:none}.toast{position:fixed;right:18px;bottom:18px;background:#101a31;border:1px solid var(--border);padding:13px 16px;border-radius:14px;box-shadow:0 12px 30px rgba(0,0,0,.3);max-width:390px;z-index:99}.hint{border-left:3px solid var(--accent);padding:10px 12px;background:#0b1224;border-radius:10px;color:var(--muted);font-size:13px;margin:10px 0}.filebox{border:1px dashed #405072;border-radius:14px;padding:14px;background:#0b1224}.filebox input{border:0;padding:0;background:transparent}
.job-tabs{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px}.job-tabs .tab{width:100%;text-align:center;border-radius:14px;padding:12px 0;transition:.15s}.job-tabs .tab:hover{filter:brightness(1.08);transform:translateY(-1px)}.job-tabs .tab.active{box-shadow:0 0 0 2px rgba(39,213,255,.18)}
</style>
</head>
<body>
<header><div style="display:flex;gap:12px;align-items:center"><a href="/trending" style="color:#07101c;background:var(--accent);border-radius:999px;padding:8px 12px;text-decoration:none;font-weight:800">🔥 Trending</a><div class="brand">🎬 AutoRecap VPS</div></div></header>
<div class="wrap">
  <div class="grid">
    <section class="card">
      <h2>Video Source</h2>
      <div class="tabs"><button class="tab active" id="tabUpload">Upload</button><button class="tab" id="tabYoutube">YouTube</button></div>
      <div id="uploadPane">
        <div class="filebox"><input type="file" id="videoFile" accept="video/mp4" /></div>
        <button style="margin-top:10px;width:100%" onclick="uploadVideo()">Upload video</button>
      </div>
      <div id="youtubePane" class="hidden">
        <label>YouTube Link</label><input id="ytUrl" placeholder="https://www.youtube.com/watch?v=..." />
        <div class="row"><div><label>Start HHMMSS</label><input id="ytStart" value="000000" /></div><div><label>End HHMMSS</label><input id="ytEnd" placeholder="000500" /></div></div>
        <label>Download Mode</label><select id="ytMode"><option value="hd">HD Download</option><option value="fast">Fast Download</option></select>
        <button style="margin-top:10px;width:100%" onclick="downloadYoutube()">Download YouTube video</button>
        
      </div>
      <hr style="border-color:#22304d;margin:16px 0" />
      <h2>Video list</h2>
      <div id="videoList" class="video-list"></div>
      
    </section>

    <main class="card">
      <div class="row"><div><h2>Selected Video</h2><div id="selectedInfo" class="small">No video selected</div></div><button class="secondary" onclick="refreshAll()">Refresh</button></div>
      <video id="preview" controls></video>
            <div class="row" style="margin-top:12px">
        <div>
          <label>Gemini Model</label>
          <select id="geminiModel">
            <option value="models/gemini-2.5-flash">Gemini 2.5 Flash</option>
            <option value="models/gemini-3.5-flash">Gemini 3.5 Flash</option>
            <option value="models/gemini-3-flash">Gemini 3 Flash</option>
            <option value="models/gemini-3.1-flash-lite">Gemini 3.1 Flash Lite</option>
            <option value="models/gemini-2.5-flash-lite">Gemini 2.5 Flash Lite</option>
          </select>
        </div>
      </div>
      <div class="row" style="margin-top:12px"><button class="purple" onclick="generateScript()">Generate Script</button><button class="secondary" onclick="generateScript()">Generate Script Again</button></div>
      <label>Script Text</label><textarea id="scriptText" placeholder="Generate Script နှိပ်ရင် ဒီထဲဝင်လာမယ်။ မကြိုက်ရင်ပြင်ပြီး Generate Recap နှိပ်ပါ။"></textarea>
      <div class="row"><div><label>Voice</label><select id="voiceName"><option>my-MM-ThihaNeural</option><option>my-MM-NilarNeural</option><option>en-US-GuyNeural</option><option>it-IT-GiuseppeMultilingualNeural</option></select></div><div><label>Voice Speed</label><input id="voiceSpeed" value="+40%" /></div></div>
      <button class="ok" style="width:100%;margin-top:12px" onclick="generateRecap()">Generate Recap Video</button>
      <div style="margin-top:12px" class="status" id="liveStatus">Idle</div>
      <div class="progress" style="margin-top:8px"><div id="liveBar" class="bar"></div></div>
    </main>
  </div>

  <section class="card" style="margin-top:16px">
    <div class="row"><h2>History / Jobs</h2></div>
    <div class="tabs job-tabs"><button class="tab jobtab active" onclick="setJobFilter('youtube')">YouTube</button><button class="tab jobtab" onclick="setJobFilter('script')">Script</button><button class="tab jobtab" onclick="setJobFilter('recap')">Recap</button></div>
    <div id="jobs" class="jobs"></div>
  </section>
</div>
<div id="toast" class="toast hidden"></div>
<script>
let selectedVideoId = localStorage.getItem('selectedVideoId') || '';
let activeJobId = localStorage.getItem('activeJobId') || '';
let pollTimer = null;
let jobFilter = localStorage.getItem('jobFilter') || 'youtube';

function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.remove('hidden');setTimeout(()=>t.classList.add('hidden'),5500)}
function setStatus(msg,pct){pct=Number(pct||0);let txt=msg||'Idle';if(pct>0&&pct<100){txt += ' — '+pct+'%'}document.getElementById('liveStatus').textContent=txt;document.getElementById('liveBar').style.width=pct+'%'}
function getGeminiModel(){const el=document.getElementById('geminiModel');return el?el.value:'models/gemini-2.5-flash'}
function api(path, opts={}){return fetch(path,opts).then(async r=>{if(!r.ok){let e;try{e=await r.json()}catch(_){e={detail:await r.text()}}throw new Error(e.detail||e.message||'Request failed')}return r.json()})}
function niceVideoName(v){const t=(v.source&&v.source.type)||'upload';return t==='youtube'?'youtube.mp4':'upload.mp4'}

document.getElementById('tabUpload').onclick=()=>{tab('upload')};
document.getElementById('tabYoutube').onclick=()=>{tab('youtube')};
function tab(which){document.getElementById('uploadPane').classList.toggle('hidden',which!=='upload');document.getElementById('youtubePane').classList.toggle('hidden',which!=='youtube');document.getElementById('tabUpload').classList.toggle('active',which==='upload');document.getElementById('tabYoutube').classList.toggle('active',which==='youtube')}
function setJobFilter(which){jobFilter=which;localStorage.setItem('jobFilter',which);refreshAll()}


function anyVideoInUse(){
  return Array.from(document.querySelectorAll('video')).some(v=>{
    return v.currentTime > 0 && !v.ended;
  });
}

async function refreshAll(){
  const data=await api('/api/status');
  renderVideos(data.videos||[]); renderJobs(data.jobs||[]);
}
function renderVideos(videos){
  const box=document.getElementById('videoList'); box.innerHTML='';
  if(!videos.length){box.innerHTML='<div class="small">Cached video မရှိသေးပါ။</div>';return}
  if(!selectedVideoId || !videos.find(v=>v.id===selectedVideoId)){selectedVideoId=videos[0].id; localStorage.setItem('selectedVideoId',selectedVideoId)}
  videos.forEach(v=>{
    const div=document.createElement('div'); div.className='video-item '+(v.id===selectedVideoId?'active':'');
    div.innerHTML=`<div><b>${niceVideoName(v)}</b><div class="meta">${v.duration_text} · ${v.size_mb} MB · words ${v.word_range}<br>${v.created_at}</div></div><button class="danger">Delete</button>`;
    div.onclick=(e)=>{if(e.target.tagName==='BUTTON')return; selectVideo(v)};
    div.querySelector('button').onclick=async(e)=>{e.stopPropagation(); if(!confirm('Delete this video from VPS?'))return; await api('/api/delete-video/'+v.id,{method:'POST'}); if(selectedVideoId===v.id){selectedVideoId='';localStorage.removeItem('selectedVideoId')} const gm=document.getElementById('geminiModel'); if(gm){gm.value=localStorage.getItem('geminiModel')||gm.value; gm.onchange=()=>localStorage.setItem('geminiModel',gm.value)}
refreshAll();};
    box.appendChild(div);
  });
  const selected=videos.find(v=>v.id===selectedVideoId); if(selected) selectVideo(selected,false);
}
function selectVideo(v,refresh=true){
  selectedVideoId=v.id;
  localStorage.setItem('selectedVideoId',v.id);

  document.getElementById('selectedInfo').textContent =
    `${niceVideoName(v)} · ${v.duration_text} · target ${v.word_range} words`;

  const preview = document.getElementById('preview');
  const newSrc = v.stream_url || '';
  const oldSrc = preview.getAttribute('src') || '';

  if(oldSrc !== newSrc){
    preview.src = newSrc;
  }

  if(refresh) refreshAll();
}
function renderJobs(jobs){
  jobs=(jobs||[]).filter(j=>(j.kind||'')===jobFilter);
  document.querySelectorAll('.jobtab').forEach(b=>b.classList.toggle('active',b.textContent.toLowerCase()===jobFilter));
  const box=document.getElementById('jobs'); box.innerHTML='';
  if(!jobs.length){box.innerHTML='<div class="small">'+jobFilter+' jobs မရှိသေးပါ။</div>';return}
  jobs.forEach(j=>{
    const cls=j.status==='done'?'done':(j.status==='error'?'error':'running');
    let links='';
    if(j.status==='done' && j.kind==='recap') links=`<a href="${j.video_url}" target="_blank">Download MP4</a> · <a href="${j.srt_url}" target="_blank">Download SRT</a>`;
    if(j.status==='done' && j.kind==='script') links=`<button class="secondary" onclick="useScript('${j.id}')">Use Script</button>`;
    if(j.status==='done' && j.kind==='youtube' && j.result_video_id) links=`<button class="secondary" onclick="selectDownloaded('${j.result_video_id}')">Select Video</button>`;
    const div=document.createElement('div'); div.className='job';
    const progressText = j.status==='done' ? '' : ` <span class="small">${j.progress||0}%</span>`;
    const progressBar = j.status==='done' ? '' : `<div class="progress" style="margin:8px 0"><div class="bar" style="width:${j.progress||0}%"></div></div>`;
    div.innerHTML=`<div class="title">${j.title||j.kind}</div><div><span class="pill ${cls}">${j.status}</span>${progressText}</div><div class="meta">${j.message||''}</div>${progressBar}<div class="small">${j.created_at||''}</div><div class="actions">${links}<button class="danger" onclick="deleteJob('${j.id}')">Delete</button></div>`;
    box.appendChild(div);
  });
}
async function uploadVideo(){
  const f=document.getElementById('videoFile').files[0]; if(!f){toast('MP4 file ရွေးပါ');return}
  const fd=new FormData(); fd.append('video',f); setStatus('Uploading...',25);
  try{const v=await api('/api/upload',{method:'POST',body:fd}); selectedVideoId=v.id; localStorage.setItem('selectedVideoId',v.id); setStatus('Uploaded',0); await refreshAll();}
  catch(e){toast(e.message); setStatus('Upload failed',0)}
}
async function downloadYoutube(){
  const fd=new FormData(); fd.append('url',document.getElementById('ytUrl').value); fd.append('start',document.getElementById('ytStart').value); fd.append('end',document.getElementById('ytEnd').value); fd.append('mode',document.getElementById('ytMode')?document.getElementById('ytMode').value:'hd'); fd.append('mode',document.getElementById('ytMode')?document.getElementById('ytMode').value:'hd');
  try{const r=await api('/api/youtube',{method:'POST',body:fd}); activeJobId=r.job_id; localStorage.setItem('activeJobId',activeJobId); pollJob(activeJobId); refreshAll();}
  catch(e){toast(e.message)}
}
async function generateScript(){
  if(!selectedVideoId){toast('Video တစ်ခု select လုပ်ပါ');return}
  const fd=new FormData(); fd.append('video_id',selectedVideoId); fd.append('gemini_model', getGeminiModel());
  try{const r=await api('/api/script',{method:'POST',body:fd}); activeJobId=r.job_id; localStorage.setItem('activeJobId',activeJobId); pollJob(activeJobId); refreshAll();}
  catch(e){toast(e.message)}
}
async function generateRecap(){
  if(!selectedVideoId){toast('Video တစ်ခု select လုပ်ပါ');return}
  const script=document.getElementById('scriptText').value; if(!script.trim()){toast('Script text ထည့်ပါ');return}
  const fd=new FormData(); fd.append('video_id',selectedVideoId); fd.append('gemini_model', getGeminiModel()); fd.append('script_text',script); fd.append('voice_name',document.getElementById('voiceName').value); fd.append('voice_speed',document.getElementById('voiceSpeed').value); fd.append('gemini_model', getGeminiModel());
  try{const r=await api('/api/generate',{method:'POST',body:fd}); activeJobId=r.job_id; localStorage.setItem('activeJobId',activeJobId); pollJob(activeJobId); refreshAll(); toast('Job started. Page refresh/close လုပ်လည်း VPS ပေါ်မှာ ဆက်လုပ်နေမယ်။')}
  catch(e){toast(e.message)}
}
async function pollJob(jobId){
  clearInterval(pollTimer);
  async function tick(){
    try{
      const j=await api('/api/jobs/'+jobId); setStatus(`${j.message||j.status}`, j.progress||0);
      if(j.status==='done'){
        if(j.kind==='script' && j.script_text){document.getElementById('scriptText').value=j.script_text; toast('Script ready')}
        if(j.kind==='youtube' && j.result_video_id){selectedVideoId=j.result_video_id; localStorage.setItem('selectedVideoId',selectedVideoId); toast('YouTube video cached')}
        if(j.kind==='recap'){toast('Final video ready in History')}
        localStorage.removeItem('activeJobId'); activeJobId=''; clearInterval(pollTimer); refreshAll();
      } else if(j.status==='error') {toast(j.message||'Job failed'); localStorage.removeItem('activeJobId'); activeJobId=''; clearInterval(pollTimer); refreshAll();}
    } catch(e){console.log(e)}
  }
  tick(); pollTimer=setInterval(tick,3000);
}
async function useScript(jobId){const j=await api('/api/jobs/'+jobId); if(j.script_text)document.getElementById('scriptText').value=j.script_text}
async function selectDownloaded(id){selectedVideoId=id; localStorage.setItem('selectedVideoId',id); await refreshAll()}
async function deleteJob(id){if(!confirm('Delete this history item?'))return; await api('/api/delete-job/'+id,{method:'POST'}); refreshAll()}
refreshAll(); if(activeJobId) pollJob(activeJobId); setInterval(refreshAll,3000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, access_log=False)
