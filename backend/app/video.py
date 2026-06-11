from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from PIL import Image

from .config import DEFAULT_VIDEO_FPS, DEFAULT_VIDEO_SECONDS


class VideoConversionError(RuntimeError):
    pass


def create_static_video(
    image_path: Path,
    output_path: Path,
    duration_seconds: int = DEFAULT_VIDEO_SECONDS,
    fps: int = DEFAULT_VIDEO_FPS,
) -> Path:
    """Convert one cover image into a real MP4 with repeated frames and silence."""
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise VideoConversionError(
            "imageio-ffmpeg is not installed; run the backend requirements install."
        ) from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration_seconds = max(2, int(duration_seconds))
    fps = max(1, int(fps))

    with Image.open(image_path) as image:
        image = image.convert("RGB")
        width, height = image.size
        if width % 2:
            width -= 1
        if height % 2:
            height -= 1
        if (width, height) != image.size:
            image = image.resize((width, height))

        with tempfile.TemporaryDirectory() as tmpdir:
            frame_path = Path(tmpdir) / "frame.png"
            image.save(frame_path, "PNG")

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            command = [
                ffmpeg,
                "-y",
                "-loop",
                "1",
                "-framerate",
                str(fps),
                "-i",
                str(frame_path),
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-t",
                str(duration_seconds),
                "-vf",
                "format=yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-c:a",
                "aac",
                "-shortest",
                str(output_path),
            ]
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise VideoConversionError(result.stderr.strip() or "ffmpeg failed")

    return output_path
