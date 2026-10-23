import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
BASE = Path("work")
BASE.mkdir(exist_ok=True)

# Çıktı boyutları. Ücretsiz Render planında 720p güvenli.
# Ücretli plana geçersen (1080, 1920) / (1920, 1080) yapabilirsin.
SIZES = {"9:16": (720, 1280), "16:9": (1280, 720)}

# Eski iş klasörleri bu süre sonra silinir (saniye)
JOB_MAX_AGE = 3 * 60 * 60

# Müzik bittiğinde (veya video bittiğinde) sesin yumuşakça kısılma süresi (saniye)
MUSIC_FADE = 1.5

# Arka plan işleri için durum tablosu ve tek seferde 1 iş sınırı (RAM'i korur)
JOBS = {}
JOBS_LOCK = threading.Lock()
RENDER_SLOT = threading.Semaphore(1)

# Düşük bellek için ortak ffmpeg ayarları
FF_QUIET = ["-hide_banner", "-loglevel", "error", "-nostats"]


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


def cleanup_old_jobs():
    """Eski iş klasörlerini sil (diski doldurmasın)."""
    now = time.time()
    for p in BASE.iterdir():
        try:
            if p.is_dir() and now - p.stat().st_mtime > JOB_MAX_AGE:
                shutil.rmtree(p, ignore_errors=True)
                with JOBS_LOCK:
                    JOBS.pop(p.name, None)
        except Exception:
            pass


def probe_duration(path, stream=None):
    """Dosyanın süresini saniye olarak döndürür. Bulunamazsa None."""
    attempts = []
    if stream:
        attempts.append([
            "ffprobe", "-v", "error", "-select_streams", stream,
            "-show_entries", "stream=duration",
            "-of", "csv=p=0", str(path)
        ])
    attempts.append([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0", str(path)
    ])
    for cmd in attempts:
        try:
            lines = run(cmd, timeout=60).stdout.strip().splitlines()
            value = float(lines[0]) if lines else 0.0
            if value > 0:
                return value
        except Exception:
            continue
    return None


def normalize_clip(source, target, width, height, keep_original_audio):
    """Bir klibi ortak formata çevirir.
    Görüntü ve ses süresi birebir eşitlenir; böylece klip geçişlerinde
    donma olmaz."""
    probe = run([
        "ffprobe", "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=codec_type",
        "-of", "csv=p=0", str(source)
    ], timeout=60)
    has_audio = bool(probe.stdout.strip())
    duration = probe_duration(source, "v:0")

    vf = (
        "fps=30,"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        "setsar=1,format=yuv420p"
    )

    cmd = ["ffmpeg", "-y", *FF_QUIET,
           "-threads", "1", "-filter_threads", "1",
           "-i", str(source)]

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
        "-threads", "1",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-crf", "23", "-pix_fmt", "yuv420p",
        "-ar", "44100", "-ac", "2",
    ]

    if duration:
        # Hem görüntü hem ses tam olarak klibin görüntü süresi kadar olsun
        cmd += ["-t", f"{duration:.3f}"]
    else:
        cmd += ["-shortest"]

    cmd += ["-avoid_negative_ts", "make_zero", str(target)]
    run(cmd)


def concat_clips(job, clips):
    """Hazır klipleri yeniden kodlamadan uç uca ekler."""
    listing = job / "list.txt"
    with listing.open("w", encoding="utf-8") as f:
        for path in clips:
            f.write(f"file '{path.name}'\n")

    joined = job / "joined.mp4"
    run([
        "ffmpeg", "-y", *FF_QUIET, "-f", "concat", "-safe", "0",
        "-i", listing.name, "-c", "copy", joined.name
    ], cwd=str(job))
    return joined


def mix_final(joined, audio_path, output, music_volume, keep_original_audio):
    """Müziği ekler. Müzik başa sarılmaz; bir kez çalar ve sonunda yumuşakça
    kısılır. Video müzikten uzunsa kalan kısımda orijinal ses devam eder."""
    video_dur = probe_duration(joined, "v:0")
    music_dur = probe_duration(audio_path, "a:0")

    music = f"volume={music_volume:.3f}"
    if video_dur and music_dur:
        end = min(video_dur, music_dur)
        if end > MUSIC_FADE * 2:
            music += f",afade=t=out:st={end - MUSIC_FADE:.3f}:d={MUSIC_FADE}"
    # Müzik bittikten sonra sessizlikle doldur (video kısalmasın, ses başa sarmasın)
    music += ",apad"

    cmd = [
        "ffmpeg", "-y", *FF_QUIET, "-threads", "1",
        "-i", str(joined), "-i", str(audio_path),
    ]

    if keep_original_audio:
        cmd += [
            "-filter_complex",
            f"[0:a]volume=1[original];"
            f"[1:a]{music}[music];"
            "[original][music]amix=inputs=2:duration=first:"
            "dropout_transition=2[aout]",
            "-map", "0:v:0", "-map", "[aout]",
        ]
    else:
        cmd += [
            "-filter_complex", f"[1:a]{music}[aout]",
            "-map", "0:v:0", "-map", "[aout]",
        ]

    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart"]
    if video_dur:
        cmd += ["-t", f"{video_dur:.3f}"]
    cmd += ["-shortest", str(output)]
    run(cmd)


def build_video(job_id, job, sources, audio_path, width, height,
                music_volume, keep_original_audio):
    """Arka planda çalışır."""
    with RENDER_SLOT:
        try:
            set_status(job_id, state="processing", done=0, total=len(sources))
            normalized = []

            for i, source in enumerate(sources):
                target = job / f"clip_{i:03d}.mp4"
                normalize_clip(source, target, width, height,
                               keep_original_audio)
                normalized.append(target)
                set_status(job_id, done=i + 1)

                # Yer kazanmak için işlenen kaynağı sil
                source.unlink(missing_ok=True)

            joined = concat_clips(job, normalized)
            output = job / "output.mp4"
            mix_final(joined, audio_path, output,
                      music_volume, keep_original_audio)

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


# ---------- Arka plan tabanlı render ----------
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
    if aspect_ratio not in SIZES:
        raise HTTPException(400, "Geçersiz video formatı.")
    if not 0 <= music_volume <= 1:
        raise HTTPException(400, "Ses seviyesi 0 ile 1 arasında olmalı.")
    if len(clips) > 30:
        raise HTTPException(400, "Tek seferde en fazla 30 klip seçebilirsin.")

    cleanup_old_jobs()

    width, height = SIZES[aspect_ratio]
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


# ---------- ESKİ: tek istekte render (aynı yardımcıları kullanır) ----------
@app.post("/render")
async def render(
    clips: list[UploadFile] = File(...),
    audio: UploadFile = File(...),
    aspect_ratio: str = Form("9:16"),
    music_volume: float = Form(0.7),
    keep_original_audio: bool = Form(True),
):
    if not clips:
        raise HTTPException(400, "En az bir video seçmelisin.")
    if aspect_ratio not in SIZES:
        raise HTTPException(400, "Geçersiz video formatı.")
    if not 0 <= music_volume <= 1:
        raise HTTPException(400, "Ses seviyesi 0 ile 1 arasında olmalı.")
    if len(clips) > 30:
        raise HTTPException(400, "Tek seferde en fazla 30 klip seçebilirsin.")

    width, height = SIZES[aspect_ratio]
    job_id = uuid.uuid4().hex
    job = BASE / job_id
    job.mkdir(parents=True)

    try:
        normalized = []

        for i, clip in enumerate(clips):
            source = job / f"source_{i}"
            with source.open("wb") as f:
                shutil.copyfileobj(clip.file, f)

            target = job / f"clip_{i:03d}.mp4"
            normalize_clip(source, target, width, height, keep_original_audio)
            normalized.append(target)

        suffix = Path(audio.filename or "music.mp3").suffix or ".mp3"
        audio_path = job / f"music{suffix}"
        with audio_path.open("wb") as f:
            shutil.copyfileobj(audio.file, f)

        joined = concat_clips(job, normalized)
        output = job / "output.mp4"
        mix_final(joined, audio_path, output,
                  music_volume, keep_original_audio)

        return {"download_url": f"/download/{job_id}"}

    except subprocess.TimeoutExpired:
        shutil.rmtree(job, ignore_errors=True)
        raise HTTPException(504, "Video işlemi zaman aşımına uğradı.")
    except Exception as e:
        shutil.rmtree(job, ignore_errors=True)
        raise HTTPException(500, f"Video oluşturulamadı: {e}")
    finally:
        for clip in clips:
            await clip.close()
        await audio.close()


@app.get("/download/{job_id}")
def download(job_id: str):
    if not job_id.isalnum():
        raise HTTPException(400, "Geçersiz işlem numarası.")

    output = BASE / job_id / "output.mp4"
    if not output.is_file():
        raise HTTPException(404, "Video bulunamadı.")

    return FileResponse(
        output, media_type="video/mp4", filename="videom.mp4"
    )