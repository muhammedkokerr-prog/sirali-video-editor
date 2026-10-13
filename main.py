import shutil
import subprocess
import uuid
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
BASE = Path("work")
BASE.mkdir(exist_ok=True)


def run(cmd, timeout=900, cwd=None):
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        timeout=timeout, cwd=cwd
    )
    if result.returncode:
        raise RuntimeError(result.stderr[-1800:] or "FFmpeg işlemi başarısız.")
    return result


@app.get("/")
def home():
    if not Path("index.html").is_file():
        raise HTTPException(404, "index.html bulunamadı.")
    return FileResponse("index.html")


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
        normalized = []

        for i, clip in enumerate(clips):
            source = job / f"source_{i}"
            with source.open("wb") as f:
                shutil.copyfileobj(clip.file, f)

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

            cmd = ["ffmpeg", "-y", "-i", str(source)]

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

        audio_path = job / "music"
        suffix = Path(audio.filename or "music.mp3").suffix or ".mp3"
        audio_path = audio_path.with_suffix(suffix)
        with audio_path.open("wb") as f:
            shutil.copyfileobj(audio.file, f)

        listing = job / "list.txt"
        with listing.open("w", encoding="utf-8") as f:
            for path in normalized:
                f.write(f"file '{path.name}'\n")

        joined = job / "joined.mp4"
        run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", listing.name, "-c", "copy", str(joined)
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
