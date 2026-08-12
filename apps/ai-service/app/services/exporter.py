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


def _has_audio_stream(video_path: str) -> bool:
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a",
                "-show_entries", "stream=index", "-of", "csv=p=0", video_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return bool(proc.stdout.strip())
    except subprocess.SubprocessError:
        return False


def _mix_background_music(video_path: str, bgm_path: str, bgm_volume: float, dest_path: str) -> bool:
    """把背景音乐循环叠加到成片下面。Seedance 视频默认不带音频，所以成片可能压根没有
    音轨——这种情况直接把（调低音量的）背景音乐当唯一音轨；如果成片本来就有音轨，
    就用 amix 把两条轨混在一起，背景音乐调低音量、原音轨音量不变，避免盖过对白/
    原始音效。用 -stream_loop -1 让背景音乐无限循环，配合 -shortest 让最终时长
    跟着（更短的）视频走，不用自己算时长对不对得上。
    这一步是锦上添花，不应该拖垮整个导出——调用方在失败时应该回退用不带背景音乐的
    版本，不抛异常。
    """
    has_audio = _has_audio_stream(video_path)
    if has_audio:
        filter_complex = (
            f"[1:a]volume={bgm_volume}[bgm];[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=0[aout]"
        )
    else:
        filter_complex = f"[1:a]volume={bgm_volume}[aout]"
    proc = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", video_path,
            "-stream_loop", "-1", "-i", bgm_path,
            "-filter_complex", filter_complex,
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy",
            "-shortest",
            str(dest_path),
        ],
        capture_output=True,
        text=True,
        timeout=FFMPEG_TIMEOUT_SEC,
    )
    return proc.returncode == 0 and Path(dest_path).exists()


def export_project_video(
    project_id: str,
    shots_with_video: list[dict],
    burn_subtitles: bool = True,
    bgm_path: Optional[str] = None,
    bgm_volume: float = 0.2,
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

        # best_path 全程指向"目前为止最完整的一版成片"——后面每一步（烧字幕、加背景
        # 音乐）都是在上一步的产物基础上继续处理，任意一步失败都退回上一步的结果，
        # 不会让"锦上添花"的步骤拖垮已经成功的部分。
        best_path = concat_path

        if burn_subtitles:
            srt_content = build_srt(shots_with_video)
            if srt_content.strip():
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
                        str(best_path),
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
                if proc.returncode == 0 and final_path.exists():
                    best_path = final_path
                # 烧字幕失败不影响 best_path，继续用拼接版本走下一步。

        if bgm_path and Path(bgm_path).is_file():
            bgm_out_path = out_dir / f"final_bgm_{stamp}.mp4"
            if _mix_background_music(str(best_path), bgm_path, bgm_volume, str(bgm_out_path)):
                best_path = bgm_out_path
            # 混音失败同样不影响 best_path，返回混音之前的版本。

        return str(best_path), skipped
