from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

try:
    import static_ffmpeg
except ImportError:
    static_ffmpeg = None

try:
    import imageio_ffmpeg  # Fallback if static-ffmpeg cannot be initialized.
except ImportError:
    imageio_ffmpeg = None

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CLIPPER_DATA_DIR", BASE_DIR / "data")).resolve()
CLIPS_DIR = DATA_DIR / "clips"
TEMP_DIR = DATA_DIR / "temp"
CLIPS_DB = DATA_DIR / "clips.json"
CLIP_TTL_SECONDS = 24 * 60 * 60

for folder in (DATA_DIR, CLIPS_DIR, TEMP_DIR):
    folder.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()

VIDEO_FORMATS = {"mp4", "webm"}
AUDIO_FORMATS = {"mp3", "m4a", "ogg", "opus"}
ALLOWED_FORMATS = VIDEO_FORMATS | AUDIO_FORMATS
ALLOWED_QUALITIES = {"360", "480", "720", "1080", "1440", "2160", "best"}


class ClipError(Exception):
    """An error that can be safely shown in the local UI."""


def get_ffmpeg_path() -> str | None:
    """Find FFmpeg and make a paired FFprobe available when possible."""
    if static_ffmpeg is not None:
        try:
            # static-ffmpeg downloads a platform build on first use and adds both
            # ffmpeg and ffprobe to this process PATH. It does not need admin rights.
            static_ffmpeg.add_paths()
            path = shutil.which("ffmpeg")
            if path:
                return path
        except Exception:
            pass
    if imageio_ffmpeg is not None:
        try:
            path = imageio_ffmpeg.get_ffmpeg_exe()
            if path and Path(path).exists():
                return str(path)
        except Exception:
            pass
    return shutil.which("ffmpeg")


def read_clips_db() -> list[dict[str, Any]]:
    try:
        data = json.loads(CLIPS_DB.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def write_clips_db(clips: list[dict[str, Any]]) -> None:
    temporary = CLIPS_DB.with_suffix(".tmp")
    temporary.write_text(json.dumps(clips, indent=2), encoding="utf-8")
    temporary.replace(CLIPS_DB)


def cleanup_old_clips() -> None:
    """Keep generated clips for 24 hours, then remove their files and metadata."""
    cutoff = time.time() - CLIP_TTL_SECONDS
    retained: list[dict[str, Any]] = []
    changed = False
    for clip in read_clips_db():
        created = float(clip.get("created_at", 0) or 0)
        file_name = str(clip.get("file_name", ""))
        if created < cutoff:
            candidate = (CLIPS_DIR / file_name).resolve()
            if candidate.parent == CLIPS_DIR.resolve() and candidate.is_file():
                try:
                    candidate.unlink()
                except OSError:
                    pass
            changed = True
        else:
            retained.append(clip)
    if changed:
        write_clips_db(retained)

    # Remove abandoned temporary job folders left by a previous unclean shutdown.
    for child in TEMP_DIR.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


def validate_url(value: Any) -> str:
    if not isinstance(value, str):
        raise ClipError("Paste a video link first.")
    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ClipError("Enter a valid public video URL beginning with http:// or https://.")
    return url


def validate_time(value: Any, label: str) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ClipError(f"{label} time is not valid.")
    if seconds < 0:
        raise ClipError(f"{label} time cannot be negative.")
    return seconds


def public_error(exc: Exception) -> str:
    message = str(exc).strip()
    # Remove common noisy prefixes while keeping the useful provider message.
    message = re.sub(r"^(ERROR:\s*)+", "", message, flags=re.IGNORECASE)
    if not message:
        return "Something went wrong while processing this video."
    return message[:450]


def extractor_options() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
    }


def extract_video_info(url: str) -> dict[str, Any]:
    if yt_dlp is None:
        raise ClipError("The app dependencies are not installed. Run start.bat again.")
    options = extractor_options()
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info:
            raise ClipError("No video information was returned for this link.")
        if info.get("_type") == "playlist" or info.get("entries"):
            raise ClipError("Please paste a link to one video, not a playlist.")

    duration = info.get("duration")
    if duration is None:
        raise ClipError("This video does not provide a usable duration, so clip times cannot be set.")

    extractor = str(info.get("extractor_key") or info.get("extractor") or "")
    video_id = str(info.get("id") or "")
    is_youtube = "youtube" in extractor.lower() and bool(video_id)
    return {
        "id": video_id,
        "title": str(info.get("title") or "Untitled video"),
        "uploader": str(info.get("uploader") or info.get("channel") or ""),
        "duration": float(duration),
        "thumbnail": info.get("thumbnail") or "",
        "webpage_url": info.get("webpage_url") or url,
        "youtube_id": video_id if is_youtube else None,
        "site": str(info.get("extractor_key") or info.get("extractor") or "Video source"),
    }


def update_job(job_id: str, **changes: Any) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(changes)
            JOBS[job_id]["updated_at"] = time.time()


def make_format_selector(quality: str, output_format: str) -> str:
    if output_format in AUDIO_FORMATS:
        return "bestaudio/best"
    if quality == "best":
        return "bestvideo+bestaudio/best"
    return f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]"


def find_downloaded_source(folder: Path) -> Path:
    candidates = []
    for path in folder.glob("source.*"):
        if not path.is_file():
            continue
        if path.name.endswith((".part", ".ytdl", ".temp")):
            continue
        candidates.append(path)
    if not candidates:
        raise ClipError("The source download finished without a media file. Try another link or quality.")
    return max(candidates, key=lambda item: item.stat().st_size)


def ffmpeg_command(
    ffmpeg: str,
    source: Path,
    destination: Path,
    start: float,
    end: float,
    output_format: str,
    cut_mode: str,
) -> list[str]:
    duration = end - start
    source_ext = source.suffix.lower().lstrip(".")
    args = [ffmpeg, "-hide_banner", "-y", "-loglevel", "error"]

    # Stream-copy cuts are fast but may begin at the nearest keyframe. Precise cuts
    # decode/re-encode so the selected start and end are frame-accurate.
    precise = cut_mode == "precise"
    can_copy_video = (output_format == "mp4" and source_ext in {"mp4", "m4v", "mov"}) or (
        output_format == "webm" and source_ext == "webm"
    )
    use_copy = not precise and output_format in VIDEO_FORMATS and can_copy_video

    if not precise:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", str(source)]
    if precise:
        args += ["-ss", f"{start:.3f}"]
    args += ["-t", f"{duration:.3f}"]

    if output_format in VIDEO_FORMATS:
        args += ["-map", "0:v:0", "-map", "0:a:0?"]
        if use_copy:
            args += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
        elif output_format == "mp4":
            args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
        else:
            args += ["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", "-c:a", "libopus", "-b:a", "160k"]
    else:
        args += ["-map", "0:a:0", "-vn"]
        if output_format == "mp3":
            args += ["-c:a", "libmp3lame", "-q:a", "2"]
        elif output_format == "m4a":
            args += ["-c:a", "aac", "-b:a", "256k"]
        elif output_format == "ogg":
            args += ["-c:a", "libvorbis", "-q:a", "5"]
        elif output_format == "opus":
            args += ["-c:a", "libopus", "-b:a", "160k"]

    args += ["-progress", "pipe:1", "-nostats", str(destination)]
    return args


def run_ffmpeg(job_id: str, command: list[str], clip_duration: float) -> None:
    update_job(job_id, status="processing", progress=76, message="Trimming and preparing your clip…")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    last_error_lines: list[str] = []
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.strip()
        if not line:
            continue
        if "=" not in line:
            last_error_lines.append(line)
            last_error_lines = last_error_lines[-5:]
            continue
        key, value = line.split("=", 1)
        if key == "out_time_ms":
            try:
                elapsed = int(value) / 1_000_000
                percent = min(98, 76 + int((elapsed / max(clip_duration, 0.1)) * 21))
                update_job(job_id, progress=percent, message="Trimming and preparing your clip…")
            except ValueError:
                pass
    return_code = process.wait()
    if return_code != 0:
        detail = " ".join(last_error_lines).strip()
        raise ClipError(detail or "FFmpeg could not create this clip. Try a different format or cut mode.")


def run_clip_job(job_id: str, payload: dict[str, Any]) -> None:
    work_dir = TEMP_DIR / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        ffmpeg = get_ffmpeg_path()
        if not ffmpeg:
            raise ClipError("FFmpeg was not found. Run start.bat to install the bundled FFmpeg package.")
        if yt_dlp is None:
            raise ClipError("yt-dlp is not installed. Run start.bat again.")

        url = validate_url(payload.get("url"))
        output_format = str(payload.get("format", "mp4")).lower()
        quality = str(payload.get("quality", "720"))
        cut_mode = str(payload.get("cut_mode", "fast"))
        start = validate_time(payload.get("start"), "Start")
        end = validate_time(payload.get("end"), "End")
        duration = float(payload.get("duration", 0) or 0)
        title = str(payload.get("title") or "video")

        if output_format not in ALLOWED_FORMATS:
            raise ClipError("Choose one of the available output formats.")
        if quality not in ALLOWED_QUALITIES:
            raise ClipError("Choose one of the available video qualities.")
        if cut_mode not in {"fast", "precise"}:
            raise ClipError("Choose a valid cut mode.")
        if end <= start:
            raise ClipError("End time must be later than start time.")
        if duration <= 0 or end > duration + 0.05:
            raise ClipError("The selected time range is outside the video duration. Reload the video and try again.")

        update_job(job_id, status="downloading", progress=1, message="Connecting to the video source…")

        def progress_hook(status: dict[str, Any]) -> None:
            state = status.get("status")
            if state == "downloading":
                total = status.get("total_bytes") or status.get("total_bytes_estimate")
                downloaded = status.get("downloaded_bytes") or 0
                pct = int((downloaded / total) * 74) if total else 10
                update_job(job_id, status="downloading", progress=max(2, min(74, pct)), message="Downloading source media…")
            elif state == "finished":
                update_job(job_id, progress=75, message="Source downloaded. Preparing the clip…")

        base_ydl_options = extractor_options()
        base_ydl_options.update({
            "format": make_format_selector(quality, output_format),
            "outtmpl": str(work_dir / "source.%(ext)s"),
            "merge_output_format": "mkv",
            "ffmpeg_location": ffmpeg,
            "progress_hooks": [progress_hook],
            "overwrites": True,
            "continuedl": True,
        })

        used_section_download = False
        if cut_mode == "fast":
            # Fetch only the requested section where the source supports it. These
            # quick cuts use stream-copy and can begin at a nearby keyframe.
            section_options = dict(base_ydl_options)
            section_options["download_ranges"] = yt_dlp.utils.download_range_func(None, [(start, end)])
            update_job(job_id, status="downloading", progress=2, message="Fetching the selected section…")
            try:
                with yt_dlp.YoutubeDL(section_options) as ydl:
                    info = ydl.extract_info(url, download=True)
                if not info:
                    raise ClipError("The source did not return a downloadable media stream.")
                used_section_download = True
            except Exception:
                # Some extractors do not support section downloads. Fall back to
                # downloading the source and trimming it locally.
                for partial in work_dir.iterdir():
                    if partial.is_file():
                        try:
                            partial.unlink()
                        except OSError:
                            pass
                update_job(job_id, status="downloading", progress=3, message="This source needs a full download. Continuing…")
                with yt_dlp.YoutubeDL(base_ydl_options) as ydl:
                    info = ydl.extract_info(url, download=True)
                if not info:
                    raise ClipError("The source did not return a downloadable media stream.")
        else:
            # Precise cuts need the original frames around the requested points.
            # Downloading the full source avoids cutting away keyframes too early.
            update_job(job_id, status="downloading", progress=2, message="Downloading source for a precise cut…")
            with yt_dlp.YoutubeDL(base_ydl_options) as ydl:
                info = ydl.extract_info(url, download=True)
            if not info:
                raise ClipError("The source did not return a downloadable media stream.")

        title = str(info.get("title") or title)
        source = find_downloaded_source(work_dir)
        clip_id = uuid.uuid4().hex[:12]
        safe_title = secure_filename(title)[:58] or "video-clip"
        file_name = f"{safe_title}-{clip_id}.{output_format}"
        destination = CLIPS_DIR / file_name

        # A section download starts at local time zero; a fallback/full download
        # retains the video's original timeline.
        source_start = 0.0 if used_section_download else start
        source_end = (end - start) if used_section_download else end
        command = ffmpeg_command(ffmpeg, source, destination, source_start, source_end, output_format, cut_mode)
        try:
            run_ffmpeg(job_id, command, end - start)
        except ClipError:
            # MP4/WebM stream-copy can fail when the source codecs do not fit the
            # selected container. Retry once with a compatible video/audio encode.
            if cut_mode != "fast" or output_format not in VIDEO_FORMATS:
                raise
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            update_job(job_id, status="processing", progress=76, message="Converting to a compatible format…")
            compatible_command = ffmpeg_command(ffmpeg, source, destination, source_start, source_end, output_format, "precise")
            run_ffmpeg(job_id, compatible_command, end - start)
        if not destination.exists() or destination.stat().st_size == 0:
            raise ClipError("The clip file was not created. Try another format or cut mode.")

        clip_record = {
            "id": clip_id,
            "title": title,
            "file_name": file_name,
            "format": output_format,
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(end - start, 3),
            "size": destination.stat().st_size,
            "created_at": time.time(),
            "cut_mode": cut_mode,
        }
        with JOBS_LOCK:
            clips = read_clips_db()
            clips.insert(0, clip_record)
            write_clips_db(clips)
        update_job(job_id, status="completed", progress=100, message="Your clip is ready.", clip=clip_record)
    except Exception as exc:
        update_job(job_id, status="failed", progress=0, message=public_error(exc), error=public_error(exc))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def serialize_clip(clip: dict[str, Any]) -> dict[str, Any]:
    result = dict(clip)
    result.pop("file_name", None)
    return result


@app.get("/")
def index():
    cleanup_old_clips()
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "ffmpeg": bool(get_ffmpeg_path()), "yt_dlp": yt_dlp is not None})


@app.post("/api/info")
def video_info():
    payload = request.get_json(silent=True) or {}
    try:
        url = validate_url(payload.get("url"))
        info = extract_video_info(url)
        return jsonify({"ok": True, "video": info})
    except Exception as exc:
        return jsonify({"ok": False, "error": public_error(exc)}), 400


@app.post("/api/clip")
def create_clip():
    payload = request.get_json(silent=True) or {}
    try:
        validate_url(payload.get("url"))
        job_id = uuid.uuid4().hex
        with JOBS_LOCK:
            JOBS[job_id] = {
                "id": job_id,
                "status": "queued",
                "progress": 0,
                "message": "Clip queued…",
                "created_at": time.time(),
                "updated_at": time.time(),
            }
        worker = threading.Thread(target=run_clip_job, args=(job_id, payload), daemon=True)
        worker.start()
        return jsonify({"ok": True, "job_id": job_id})
    except Exception as exc:
        return jsonify({"ok": False, "error": public_error(exc)}), 400


@app.get("/api/job/<job_id>")
def job_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "This clip job could not be found."}), 404
        response = dict(job)
    if response.get("clip"):
        response["clip"] = serialize_clip(response["clip"])
    return jsonify({"ok": True, "job": response})


@app.get("/api/clips")
def list_clips():
    cleanup_old_clips()
    clips = [clip for clip in read_clips_db() if (CLIPS_DIR / str(clip.get("file_name", ""))).is_file()]
    return jsonify({"ok": True, "clips": [serialize_clip(clip) for clip in clips]})


@app.get("/api/clips/<clip_id>/download")
def download_clip(clip_id: str):
    cleanup_old_clips()
    clip = next((item for item in read_clips_db() if item.get("id") == clip_id), None)
    if not clip:
        return jsonify({"ok": False, "error": "This clip has expired or was deleted."}), 404
    file_name = str(clip.get("file_name", ""))
    candidate = (CLIPS_DIR / file_name).resolve()
    if candidate.parent != CLIPS_DIR.resolve() or not candidate.is_file():
        return jsonify({"ok": False, "error": "Clip file not found."}), 404
    return send_from_directory(CLIPS_DIR, file_name, as_attachment=True, download_name=file_name)


@app.delete("/api/clips/<clip_id>")
def delete_clip(clip_id: str):
    clips = read_clips_db()
    target = next((item for item in clips if item.get("id") == clip_id), None)
    if not target:
        return jsonify({"ok": False, "error": "Clip not found."}), 404
    candidate = (CLIPS_DIR / str(target.get("file_name", ""))).resolve()
    if candidate.parent == CLIPS_DIR.resolve() and candidate.is_file():
        try:
            candidate.unlink()
        except OSError:
            pass
    write_clips_db([item for item in clips if item.get("id") != clip_id])
    return jsonify({"ok": True})


if __name__ == "__main__":
    cleanup_old_clips()
    print("\n  Local Clipper is starting…")
    ffmpeg_path = get_ffmpeg_path()
    if ffmpeg_path:
        print("  FFmpeg is ready.")
    else:
        print("  FFmpeg was not found yet; clip exports may need setup.")
    print("  Open http://127.0.0.1:5050 in your browser.\n")
    app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)
