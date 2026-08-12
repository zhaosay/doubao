"""
把一个项目里各分镜已生成的视频，按 Scene/Shot 顺序拼接成一条成片，
用 dialogue 字段生成字幕烧录进画面。纯本地 ffmpeg 调用，不依赖任何 AI 接口。
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from app.db import get_connection, get_settings
from app.services.paths import project_dir

FFMPEG_TIMEOUT_SEC = 600


class ExportError(RuntimeError):
    pass


def _probe_duration(video_path: str) -> Optional[float]:
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return float(proc.stdout.strip())
    except (ValueError, subprocess.SubprocessError):
        return None


def _srt_timestamp(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(shots_with_video: list[dict]) -> str:
    """shots_with_video: [{"dialogue": str|None, "videoPath": str, "durationSec": float}, ...]"""
    lines: list[str] = []
    cursor = 0.0
    index = 1
    for shot in shots_with_video:
        duration = _probe_duration(shot["videoPath"]) or shot.get("durationSec") or 4.0
        dialogue = (shot.get("dialogue") or "").strip()
        if dialogue:
            lines.append(str(index))
            lines.append(f"{_srt_timestamp(cursor)} --> {_srt_timestamp(cursor + duration)}")
            lines.append(dialogue)
            lines.append("")
            index += 1
        cursor += duration
    return "\n".join(lines)


def export_project_video(
    project_id: str, shots_with_video: list[dict], burn_subtitles: bool = True
) -> tuple[str, list[dict]]:
    """
    shots_with_video 需按最终顺序排好: [{"videoPath": str|None, "dialogue": str|None, "durationSec": float, ...}, ...]
    以前是"只要有一镜没视频就整个导出失败"，改成"缺视频的镜头直接跳过，不进最终成片和
    字幕时间轴，其余镜头正常拼"——不然一部剧几十镜，卡在其中一镜没出片，其它已经出完的
    都出不了成片。跳过的镜头会跟着调用方原样传入的其它字段（比如 sceneOrder/shotOrder）
    一起回传，方便调用方告诉用户"哪几镜被跳过了"。
    返回 (成片绝对路径, 被跳过的镜头列表)。
    """
    if not shots_with_video:
        raise ExportError("没有任何分镜有已完成的视频，先把 shot 的「生成视频」跑完")

    skipped = [s for s in shots_with_video if not s.get("videoPath")]
    shots_with_video = [s for s in shots_with_video if s.get("videoPath")]
    if not shots_with_video:
        raise ExportError("没有任何分镜有已完成的视频，先把 shot 的「生成视频」跑完")

    with get_connection() as conn:
        settings = get_settings(conn)
    export_dir_setting = (settings.get("exportDir") or "").strip()
    # 填了导出目录就直接存那儿（比如桌面/指定文件夹，方便直接找到成片）；没填就存在
    # 这个项目自己的文件夹下的 export/ 子目录里，跟 generated/characters/scenes 平级，
    # 一个项目的所有产物（角色图/场景图/分镜图片视频配音/成片）都在同一个项目文件夹下。
    out_dir = Path(export_dir_setting).expanduser() if export_dir_setting else project_dir(project_id) / "export"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        list_file = tmp_path / "list.txt"
        list_file.write_text(
            "\n".join(f"file '{s['videoPath']}'" for s in shots_with_video), encoding="utf-8"
        )

        concat_path = out_dir / f"concat_{stamp}.mp4"
        # 先尝试 stream copy（各分镜都是同参数生成的 Seedance 视频，编码/分辨率一致，速度快）。
        proc = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-c",
                "copy",
                str(concat_path),
            ],
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_SEC,
        )
        if proc.returncode != 0 or not concat_path.exists():
            # stream copy 失败（比如编码不一致），退回重新编码拼接。
            proc = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(list_file),
                    str(concat_path),
                ],
                capture_output=True,
                text=True,
                timeout=FFMPEG_TIMEOUT_SEC,
            )
            if proc.returncode != 0:
                raise ExportError(f"ffmpeg 拼接失败: {proc.stderr[-2000:]}")

        if not burn_subtitles:
            return str(concat_path), skipped

        srt_content = build_srt(shots_with_video)
        if not srt_content.strip():
            return str(concat_path), skipped

        srt_path = tmp_path / "subs.srt"
        srt_path.write_text(srt_content, encoding="utf-8")

        final_path = out_dir / f"final_{stamp}.mp4"
        # subtitles 滤镜的路径里不能有冒号/反斜杠转义问题，统一转成 posix 相对写法更稳。
        escaped_srt = str(srt_path).replace("\\", "/").replace(":", "\\:")
        proc = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(concat_path),
                "-vf",
                f"subtitles='{escaped_srt}'",
                "-c:a",
                "copy",
                str(final_path),
            ],
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_SEC,
        )
        if proc.returncode != 0 or not final_path.exists():
            # 字幕烧录失败不应该让整个导出失败，退回没字幕的拼接版本。
            return str(concat_path), skipped

        return str(final_path), skipped
