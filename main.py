import shutil
import subprocess
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
BASE = Path("work")
BASE.mkdir(exist_ok=True)

# Arka plan işleri için durum tablosu ve tek seferde 1 iş sınırı (RAM'i korur)
JOBS = {}
JOBS_LOCK = threading.Lock()
RENDER_SLOT = threading.Semaphore(1)


def run(cmd, timeout=900, cwd=None):
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        timeout=timeout, cwd=cwd
    )
    if result.returncode:
        raise RuntimeError(result.stderr[-1800:] or "FFmpeg işlemi başarısız.")
    return result


def set_status(job_id, **kwargs):
    with JOBS_LOCK:
        JOBS.setdefault(job_id, {}).update(kwargs)


def build_video(job_id, job, sources, audio_path, width, height,
                music_volume, keep_original_audio):
    """Arka planda çalışır. Mantık /render ile aynı."""
    with RENDER_SLOT:
        try:
            set_status(job_id, state="processing", done=0, total=len(sources))
            normalized = []

            for i, source in enumerate(sources):
                probe = run([
                    "ffprobe", "-v", "error",
                    "-select_streams", "a:0",
                    "-show_entries", "stream=codec_type",
                    "-of", "csv=p=0", str(source)
                ], timeout=60)

                has_audio = bool(probe.stdout.strip())
                target = job / f"clip_{i:03d}.mp4"

                vf = (
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
                    "setsar=1,fps=30,format=yuv420p"
                )

                cmd = ["ffmpeg", "-y", "-threads", "1", "-i", str(source)]

                if has_audio and keep_original_audio:
                    cmd += [
                        "-map", "0:v:0", "-map", "0:a:0",
                        "-vf", vf, "-af", "aresample=44100,apad",
                        "-c:a", "aac", "-b:a", "160k",
                    ]
                else:
                    cmd += [
                        "-f", "lavfi", "-i",
                        "anullsrc=channel_layout=stereo:sample_rate=44100",
                        "-map", "0:v:0", "-map", "1:a:0",
                        "-vf", vf, "-c:a", "aac", "-b:a", "160k",
                    ]

                cmd += [
                    "-c:v", "libx264", "-preset", "ultrafast",
                    "-crf", "23", "-pix_fmt", "yuv420p",
                    "-ar", "44100", "-ac", "2", "-shortest",
                    str(target)
                ]
                run(cmd)
                normalized.append(target)
                set_status(job_id, done=i + 1)

                # Yer kazanmak için işlenen kaynağı sil
                source.unlink(missing_ok=True)

            listing = job / "list.txt"
            with listing.open("w", encoding="utf-8") as f:
                for path in normalized:
                    f.write(f"file '{path.name}'\n")

            joined = job / "joined.mp4"
            run([
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", listing.name, "-c", "copy", joined.name
            ], cwd=str(job))

            output = job / "output.mp4"
            cmd = [
                "ffmpeg", "-y", "-i", str(joined),
                "-stream_loop", "-1", "-i", str(audio_path),
            ]

            volume = f"{music_volume:.3f}"

            if keep_original_audio:
                cmd += [
                    "-filter_complex",
                    f"[0:a]volume=1[original];"
                    f"[1:a]volume={volume}[music];"
                    "[original][music]amix=inputs=2:duration=first:"
                    "dropout_transition=2[aout]",
                    "-map", "0:v:0", "-map", "[aout]",
                ]
            else:
                cmd += [
                    "-filter_complex", f"[1:a]volume={volume}[aout]",
                    "-map", "0:v:0", "-map", "[aout]",
                ]

            cmd += [
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", "-shortest", str(output)
            ]
            run(cmd)

            # Ara dosyaları temizle, sadece output.mp4 kalsın
            for p in normalized:
                p.unlink(missing_ok=True)
            joined.unlink(missing_ok=True)

            set_status(job_id, state="done",
                       download_url=f"/download/{job_id}")

        except subprocess.TimeoutExpired:
            shutil.rmtree(job, ignore_errors=True)
            set_status(job_id, state="error",
                       error="Video işlemi zaman aşımına uğradı.")
        except Exception as e:
            shutil.rmtree(job, ignore_errors=True)
            set_status(job_id, state="error",
                       error=f"Video oluşturulamadı: {e}")


@app.get("/")
def home():
    if not Path("index.html").is_file():
        raise HTTPException(404, "index.html bulunamadı.")
    return FileResponse("index.html")


# ---------- YENİ: arka plan tabanlı render ----------
@app.post("/render_start")
async def render_start(
    clips: list[UploadFile] = File(...),
    audio: UploadFile = File(...),
    aspect_ratio: str = Form("9:16"),
    music_volume: float = Form(0.7),
    keep_original_audio: bool = Form(True),
):
    if not clips:
        raise HTTPException(400, "En az bir video seçmelisin.")
    if aspect_ratio not in ("9:16", "16:9"):
        raise HTTPException(400, "Geçersiz video formatı.")
    if not 0 <= music_volume <= 1:
        raise HTTPException(400, "Ses seviyesi 0 ile 1 arasında olmalı.")
    if len(clips) > 30:
        raise HTTPException(400, "Tek seferde en fazla 30 klip seçebilirsin.")

    width, height = (1080, 1920) if aspect_ratio == "9:16" else (1920, 1080)
    job_id = uuid.uuid4().hex
    job = BASE / job_id
    job.mkdir(parents=True)

    try:
        # Dosyaları istek bitmeden diske kaydetmek zorundayız
        sources = []
        for i, clip in enumerate(clips):
            source = job / f"source_{i}"
            with source.open("wb") as f:
                shutil.copyfileobj(clip.file, f)
            sources.append(source)

        suffix = Path(audio.filename or "music.mp3").suffix or ".mp3"
        audio_path = job / f"music{suffix}"
        with audio_path.open("wb") as f:
            shutil.copyfileobj(audio.file, f)
    except Exception as e:
        shutil.rmtree(job, ignore_errors=True)
        raise HTTPException(500, f"Dosyalar kaydedilemedi: {e}")
    finally:
        for clip in clips:
            await clip.close()
        await audio.close()

    set_status(job_id, state="queued", done=0, total=len(sources))
    threading.Thread(
        target=build_video,
        args=(job_id, job, sources, audio_path, width, height,
              music_volume, keep_original_audio),
        daemon=True,
    ).start()

    return {"job_id": job_id}


@app.get("/status/{job_id}")
def status(job_id: str):
    if not job_id.isalnum():
        raise HTTPException(400, "Geçersiz işlem numarası.")
    with JOBS_LOCK:
        info = JOBS.get(job_id)
        if info is None:
            raise HTTPException(404, "İşlem bulunamadı.")
        return dict(info)


# ---------- ESKİ: aynen duruyor ----------
@app.post("/render")
async def render(
    clips: list[UploadFile] = File(...),
    audio: UploadFile = File(...),