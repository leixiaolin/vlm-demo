#!/usr/bin/env python
"""Run sampled video-frame descriptions and per-video summaries.

The script mirrors scripts/run_image_description_test.py for provider setup,
resume behavior, JSONL outputs, and mock validation. Videos are sampled at a
fixed time interval, each sampled frame is described by a vision model, and the
ordered frame descriptions are summarized into one report per video.

中文说明：
本脚本用于对视频进行抽样帧描述和按视频汇总。
主要流程为：
1. 扫描指定目录下的视频文件；
2. 按固定时间间隔对每个视频抽样帧；
3. 调用视觉模型（或 mock）对每一帧生成结构化描述；
4. 将每段视频的所有帧描述汇总为一份视频报告。
脚本在 provider 配置、断点续跑、JSONL 输出和 mock 校验等方面
复用了 run_image_description_test.py 中的实现。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from run_image_description_test import (
    DEFAULT_CHAT_COMPLETIONS_URL,
    DEFAULT_MODEL,
    DEFAULT_OPENAI_RESPONSES_URL,
    append_jsonl,
    call_chat_completions,
    call_openai,
    env_float,
    env_int,
    extract_output_text,
    format_exception,
    image_to_data_url,
    load_env_file,
)


# 默认视频输入目录
DEFAULT_VIDEO_DIR = Path("data/video")
# 默认结果输出目录
DEFAULT_OUTPUT_DIR = Path("outputs/video_description_results")
# 默认帧描述提示词文件
DEFAULT_FRAME_PROMPT = Path("prompts/video_frame_description.md")
# 默认视频汇总提示词文件
DEFAULT_SUMMARY_PROMPT = Path("prompts/video_summary.md")
# 默认抽帧间隔（秒）
DEFAULT_FRAME_INTERVAL_SECONDS = 0.5
# 默认最大输出 token 数
DEFAULT_FRAME_MAX_OUTPUT_TOKENS = 1800
DEFAULT_SUMMARY_MAX_OUTPUT_TOKENS = 3200
DEFAULT_RETRY_ATTEMPTS = 1
RETRY_PROMPT_SUFFIX = (
    "\n\n重试要求：上一次响应无法解析为完整 JSON。"
    "请只返回一个紧凑、完整、可被 json.loads 解析的 JSON 对象；"
    "不要使用 Markdown；每个字符串尽量不超过 80 个中文字符，数组最多 3 项。"
)
# 支持的视频文件扩展名集合
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}


@dataclass(frozen=True)
class VideoEntry:
    """单条视频条目：包含视频 ID 与路径。"""

    video_id: str
    path: Path


@dataclass(frozen=True)
class VideoMetadata:
    """视频元数据：帧率、总帧数、时长、宽高等基础信息。"""

    fps: float
    frame_count: int
    duration_seconds: float
    width: int
    height: int


def import_cv2():
    """按需导入 opencv-python（cv2），缺失时给出安装提示。"""
    try:
        import cv2  # type: ignore
    except Exception as exc:
        raise RuntimeError("Missing opencv-python. Install dependencies with `pip install -r requirements.txt`.") from exc
    return cv2


def sanitize_video_id(path: Path) -> str:
    """根据视频文件名生成安全的 video_id，仅保留字母、数字及 _.- 字符。"""
    video_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", path.stem).strip("._-")
    return video_id or "video"


def scan_videos(video_dir: Path) -> list[VideoEntry]:
    """扫描目录下所有支持的视频文件，并按文件名生成 VideoEntry 列表。"""
    if not video_dir.exists():
        raise FileNotFoundError(f"Video directory does not exist: {video_dir}")
    if not video_dir.is_dir():
        raise NotADirectoryError(f"Video path is not a directory: {video_dir}")

    paths = sorted(
        [path for path in video_dir.iterdir() if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS],
        key=lambda path: path.name.lower(),
    )
    if not paths:
        raise FileNotFoundError(f"No supported video files found in {video_dir}. Supported: {sorted(VIDEO_EXTENSIONS)}")

    # 对同名基础 ID 自动追加序号，避免 video_id 冲突
    seen: dict[str, int] = {}
    entries: list[VideoEntry] = []
    for path in paths:
        base_id = sanitize_video_id(path)
        count = seen.get(base_id, 0) + 1
        seen[base_id] = count
        video_id = base_id if count == 1 else f"{base_id}_{count}"
        entries.append(VideoEntry(video_id=video_id, path=path))
    return entries


def format_timestamp(seconds: float) -> str:
    """将秒数格式化为可读的时间戳字符串（MM:SS.mmm 或 HH:MM:SS.mmm）。"""
    total_ms = max(0, int(round(seconds * 1000)))
    ms = total_ms % 1000
    total_seconds = total_ms // 1000
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"
    return f"{minutes:02d}:{secs:02d}.{ms:03d}"


def strip_json_fence(text: str) -> str:
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", stripped, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        stripped = stripped.removeprefix("json").strip()
    return stripped


def extract_balanced_json_object(text: str) -> str:
    start = text.find("{")
    if start < 0:
        return text.strip()

    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1].strip()

    return text[start:].strip()


def parse_model_json(text: str) -> tuple[dict[str, Any] | None, str | None]:
    if not text.strip():
        return None, "empty model output"

    stripped = strip_json_fence(text)
    candidates = [stripped, extract_balanced_json_object(stripped), extract_balanced_json_object(text)]
    errors: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            errors.append(str(exc))
            continue
        if not isinstance(parsed, dict):
            return None, "model output is not a JSON object"
        return parsed, None

    error = errors[0] if errors else "model output does not contain a JSON object"
    if "Unterminated string" in error or "Expecting ',' delimiter" in error:
        error = f"{error}; response may be truncated"
    return None, error


def load_video_metadata(cv2, video_path: Path) -> VideoMetadata:
    """通过 cv2 读取视频元数据：fps、总帧数、宽高，并据此计算时长。"""
    capture = cv2.VideoCapture(str(video_path))
    try:
        if not capture.isOpened():
            raise RuntimeError(f"Unable to open video: {video_path}")
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        duration_seconds = frame_count / fps if fps > 0 and frame_count > 0 else 0.0
        return VideoMetadata(
            fps=round(fps, 4),
            frame_count=frame_count,
            duration_seconds=round(duration_seconds, 4),
            width=width,
            height=height,
        )
    finally:
        capture.release()


def sample_timestamps(duration_seconds: float, interval_seconds: float, limit: int) -> list[float]:
    """根据固定时间间隔生成抽帧时间点列表。

    Args:
        duration_seconds: 视频时长（秒），<=0 表示时长未知，仅采样第 0 秒。
        interval_seconds: 抽样间隔（秒），必须大于 0。
        limit: 抽样帧数上限，<=0 表示不限制。

    Returns:
        抽帧时间点（秒）的有序列表。
    """
    if interval_seconds <= 0:
        raise ValueError("--frame-interval-seconds must be greater than 0")

    timestamps: list[float] = []
    index = 0
    while True:
        timestamp = round(index * interval_seconds, 3)
        # 超出视频时长则结束采样
        if duration_seconds > 0 and timestamp > duration_seconds + 0.001:
            break
        timestamps.append(timestamp)
        if limit > 0 and len(timestamps) >= limit:
            break
        index += 1
        # 时长未知时仅取第 0 秒
        if duration_seconds <= 0:
            break
    return timestamps or [0.0]


def frame_id_for(ordinal: int, timestamp_seconds: float) -> str:
    """生成形如 frame_000001_000500ms 的帧 ID，便于排序与去重。"""
    timestamp_ms = max(0, int(round(timestamp_seconds * 1000)))
    return f"frame_{ordinal:06d}_{timestamp_ms:06d}ms"


def read_frame_at_timestamp(cv2, capture, metadata: VideoMetadata, timestamp_seconds: float):
    """在指定时间点读取一帧画面。

    优先按时间戳定位；若失败则回退为按帧索引定位，再失败返回 None。
    """
    # 优先按时间戳（毫秒）定位
    capture.set(cv2.CAP_PROP_POS_MSEC, timestamp_seconds * 1000)
    ok, frame = capture.read()
    if ok:
        return frame

    # 回退：按帧索引定位
    if metadata.fps > 0 and metadata.frame_count > 0:
        frame_index = min(max(0, int(round(timestamp_seconds * metadata.fps))), metadata.frame_count - 1)
        capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = capture.read()
        if ok:
            return frame
    return None


def extract_frames(
    *,
    cv2,
    video: VideoEntry,
    video_output_dir: Path,
    interval_seconds: float,
    limit_frames: int,
    no_resume: bool,
) -> tuple[VideoMetadata, list[dict[str, Any]]]:
    """读取视频元数据、按间隔抽帧并保存为 jpg，返回 (元数据, 帧信息列表)。

    支持断点续跑：若帧图片已存在且未启用 --no-resume，则跳过重新解码。
    """
    metadata = load_video_metadata(cv2, video.path)
    timestamps = sample_timestamps(metadata.duration_seconds, interval_seconds, limit_frames)
    frames_dir = video_output_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    capture = cv2.VideoCapture(str(video.path))
    try:
        if not capture.isOpened():
            raise RuntimeError(f"Unable to open video: {video.path}")

        frames: list[dict[str, Any]] = []
        for ordinal, timestamp_seconds in enumerate(timestamps, start=1):
            frame_id = frame_id_for(ordinal, timestamp_seconds)
            frame_path = frames_dir / f"{frame_id}.jpg"
            # 仅在需要时解码并写盘；已存在的帧直接复用，便于断点续跑
            if no_resume or not frame_path.exists():
                frame = read_frame_at_timestamp(cv2, capture, metadata, timestamp_seconds)
                if frame is None:
                    print(f"[warn] Could not read {video.video_id} at {format_timestamp(timestamp_seconds)}", flush=True)
                    continue
                if not cv2.imwrite(str(frame_path), frame):
                    raise RuntimeError(f"Unable to write frame image: {frame_path}")

            frames.append(
                {
                    "frame_id": frame_id,
                    "frame_path": str(frame_path),
                    "timestamp_seconds": timestamp_seconds,
                    "timestamp": format_timestamp(timestamp_seconds),
                }
            )
        return metadata, frames
    finally:
        capture.release()


def is_successful_frame_row(row: dict[str, Any]) -> bool:
    return bool(row.get("frame_id") and row.get("description") and not row.get("parse_error"))


def summary_needs_retry(path: Path) -> bool:
    if not path.exists():
        return True
    try:
        summary_doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return bool(summary_doc.get("parse_error") or not summary_doc.get("summary"))


def read_done_frame_ids(path: Path) -> set[str]:
    """读取帧描述 JSONL，返回已处理过的 frame_id 集合，用于断点续跑。"""
    if not path.exists():
        return set()
    done: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if is_successful_frame_row(row):
                done.add(str(row["frame_id"]))
    return done


def read_frame_description_rows(path: Path) -> list[dict[str, Any]]:
    """读取帧描述 JSONL 中的全部记录，供汇总阶段使用。"""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("frame_id"):
                rows.append(row)
    return rows


def latest_frame_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对同一 frame_id 去重保留最新记录，并按时间戳升序排序。"""
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        latest[str(row.get("frame_id", ""))] = row
    return sorted(latest.values(), key=lambda row: float(row.get("timestamp_seconds") or 0.0))


def render_frame_prompt(template: str, video: VideoEntry, frame: dict[str, Any]) -> str:
    """将帧元数据拼接到单帧描述模板中，生成最终发送给模型的提示词。"""
    metadata = {
        "video_id": video.video_id,
        "video_path": str(video.path),
        "frame_id": frame["frame_id"],
        "timestamp": frame["timestamp"],
        "timestamp_seconds": frame["timestamp_seconds"],
    }
    return f"{template}\n\n画面元数据：\n{json.dumps(metadata, ensure_ascii=False, indent=2)}"


def truncate_text(value: Any, limit: int = 80) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def compact_list(value: Any, *, max_items: int = 3, item_limit: int = 40) -> list[str]:
    if not isinstance(value, list):
        value = [] if value in (None, "") else [value]
    compact: list[str] = []
    for item in value:
        text = truncate_text(item, item_limit)
        if text:
            compact.append(text)
        if len(compact) >= max_items:
            break
    return compact


def compact_risks(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    risks: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        risks.append(
            {
                "type": truncate_text(item.get("risk_type"), 32),
                "evidence": truncate_text(item.get("evidence"), 80),
                "confidence": truncate_text(item.get("confidence"), 16),
            }
        )
        if len(risks) >= 3:
            break
    return risks


def compact_description(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "scene": truncate_text(value.get("scene_type"), 32),
        "caption": truncate_text(value.get("content_caption"), 100),
        "subjects": compact_list(value.get("visible_subjects")),
        "actions": compact_list(value.get("visible_actions")),
        "changes": compact_list(value.get("key_change_cues")),
        "risks": compact_risks(value.get("risk_observations")),
        "quality": truncate_text(value.get("image_quality"), 16),
    }


def compact_frame_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将帧描述记录压缩为只含关键字段的精简列表，用于汇总提示词。"""
    records: list[dict[str, Any]] = []
    for row in latest_frame_rows(rows):
        records.append(
            {
                "frame_id": row.get("frame_id"),
                "timestamp": row.get("timestamp"),
                "timestamp_seconds": row.get("timestamp_seconds"),
                "description": compact_description(row.get("description")),
            }
        )
    return records


def render_summary_prompt(
    template: str,
    *,
    video: VideoEntry,
    metadata: VideoMetadata | None,
    interval_seconds: float,
    frame_rows: list[dict[str, Any]],
) -> str:
    """将视频元数据与所有帧描述拼接到汇总模板中，生成汇总提示词。"""
    payload = {
        "video_id": video.video_id,
        "video_path": str(video.path),
        "frame_interval_seconds": interval_seconds,
        "video_metadata": None
        if metadata is None
        else {
            "fps": metadata.fps,
            "frame_count": metadata.frame_count,
            "duration_seconds": metadata.duration_seconds,
            "width": metadata.width,
            "height": metadata.height,
        },
        "frames": compact_frame_records(frame_rows),
    }
    return f"{template}\n\n输入数据：\n{json.dumps(payload, ensure_ascii=False, indent=2)}"


def mock_frame_description(frame: dict[str, Any]) -> dict[str, Any]:
    """mock 提供方：返回固定结构但时间戳正确的帧描述，仅用于本地流水线校验。"""
    return {
        "scene_type": "unknown",
        "content_caption": f"Mock frame description at {frame['timestamp']}.",
        "visible_subjects": ["mock_frame"],
        "visible_actions": ["mock_pipeline_validation"],
        "key_change_cues": ["mock timestamp sequence preserved"],
        "risk_observations": [
            {
                "risk_type": "none",
                "evidence": "Mock provider does not inspect image pixels.",
                "confidence": "low",
            }
        ],
        "image_quality": "fair",
        "uncertain_points": ["Mock provider does not inspect image pixels."],
    }


def mock_video_summary(video: VideoEntry, frame_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """mock 提供方：根据抽帧结果返回固定结构的视频汇总，仅用于流水线联调。"""
    compact = compact_frame_records(frame_rows)
    if compact:
        time_range = f"{compact[0]['timestamp']} - {compact[-1]['timestamp']}"
        evidence_frames = [str(row["frame_id"]) for row in compact[:5]]
    else:
        time_range = "00:00.000 - 00:00.000"
        evidence_frames = []

    return {
        "content_summary": (
            f"Mock summary for {video.video_id}. The pipeline processed {len(compact)} sampled frame descriptions "
            "and preserved their chronological order."
        ),
        "timeline": [
            {
                "time_range": time_range,
                "event": "Mock event covering the sampled frame sequence.",
                "evidence_frames": evidence_frames,
            }
        ],
        "key_events": ["Mock frame extraction and summary pipeline validation."],
        "risk_analysis": [
            {
                "risk_type": "none",
                "summary": "Mock provider does not inspect video content, so no real risk is asserted.",
                "evidence_time_ranges": [time_range],
                "confidence": "low",
            }
        ],
        "recommendations": ["Use a real vision provider for content and risk assessment."],
        "uncertain_points": ["Mock provider output is deterministic and not based on pixels."],
    }


def call_openai_text(
    *,
    responses_url: str,
    api_key: str,
    model: str,
    prompt: str,
    timeout: int,
    max_output_tokens: int,
) -> dict[str, Any]:
    """调用 OpenAI Responses API 的纯文本接口（视频汇总阶段使用，不携带图像）。"""
    body = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }
        ],
        "max_output_tokens": max_output_tokens,
    }
    request = urllib.request.Request(
        responses_url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def call_chat_completions_text(
    *,
    api_url: str,
    api_key: str,
    model: str,
    prompt: str,
    timeout: int,
    max_output_tokens: int,
) -> dict[str, Any]:
    """调用 Chat Completions 风格接口（openai_chat / deepseek），用于纯文本请求。"""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_output_tokens,
    }
    request = urllib.request.Request(
        api_url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw_resp = response.read().decode("utf-8")
    if not raw_resp.strip():
        return {"output_text": ""}
    data = json.loads(raw_resp)
    text = ""
    choices = data.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        # 优先取 content，缺失时回退到 reasoning_content（部分推理模型）
        text = msg.get("content", "") or msg.get("reasoning_content", "") or ""
    return {"output_text": text}


def describe_frame(
    *,
    args: argparse.Namespace,
    api_key: str,
    prompt: str,
    frame: dict[str, Any],
) -> tuple[dict[str, Any] | None, str, str | None]:
    """对单张帧调用视觉模型生成结构化描述。

    Returns:
        (解析后的 dict 或 None, 模型原始文本, 解析错误信息或 None)
    """
    if args.provider == "mock":
        parsed = mock_frame_description(frame)
        return parsed, json.dumps(parsed, ensure_ascii=False), None

    frame_path = Path(frame["frame_path"])
    if not frame_path.exists():
        raise FileNotFoundError(str(frame_path))

    # 根据提供方选择带图像的调用入口
    if args.provider in ("openai_chat", "deepseek"):
        payload = call_chat_completions(
            api_url=args.chat_completions_url,
            api_key=api_key,
            model=args.model,
            prompt=prompt,
            image_data_url=image_to_data_url(frame_path),
            detail=args.detail,
            timeout=args.timeout_seconds,
            max_output_tokens=args.frame_max_output_tokens,
        )
    else:
        payload = call_openai(
            responses_url=args.responses_url,
            api_key=api_key,
            model=args.model,
            prompt=prompt,
            image_data_url=image_to_data_url(frame_path),
            detail=args.detail,
            timeout=args.timeout_seconds,
            max_output_tokens=args.frame_max_output_tokens,
        )

    raw_text = extract_output_text(payload)
    parsed, parse_error = parse_model_json(raw_text)
    return parsed, raw_text, parse_error


def summarize_video(
    *,
    args: argparse.Namespace,
    api_key: str,
    prompt: str,
    video: VideoEntry,
    frame_rows: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str, str | None]:
    """基于已成功的帧描述，调用模型生成视频级别的汇总报告。"""
    if args.provider == "mock":
        parsed = mock_video_summary(video, frame_rows)
        return parsed, json.dumps(parsed, ensure_ascii=False), None

    # 汇总阶段为纯文本请求，按提供方选择对应接口
    if args.provider in ("openai_chat", "deepseek"):
        payload = call_chat_completions_text(
            api_url=args.chat_completions_url,
            api_key=api_key,
            model=args.model,
            prompt=prompt,
            timeout=args.timeout_seconds,
            max_output_tokens=args.summary_max_output_tokens,
        )
    else:
        payload = call_openai_text(
            responses_url=args.responses_url,
            api_key=api_key,
            model=args.model,
            prompt=prompt,
            timeout=args.timeout_seconds,
            max_output_tokens=args.summary_max_output_tokens,
        )

    raw_text = extract_output_text(payload)
    parsed, parse_error = parse_model_json(raw_text)
    return parsed, raw_text, parse_error


def escape_table(value: Any) -> str:
    """转义 Markdown 表格中的特殊字符（| 与换行）。"""
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def as_list(value: Any) -> list[Any]:
    """将单值或空值统一为 list，便于在渲染 Markdown 时迭代。"""
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def render_summary_markdown(summary_doc: dict[str, Any]) -> str:
    """将视频汇总 JSON 渲染为人类可读的 Markdown 报告。"""
    lines = [
        f"# Video Summary: {summary_doc.get('video_id', 'unknown')}",
        "",
        f"- Video: `{summary_doc.get('video_path', '')}`",
        f"- Provider: `{summary_doc.get('provider', '')}`",
        f"- Model: `{summary_doc.get('model', '')}`",
        f"- Frame descriptions: {summary_doc.get('successful_frame_descriptions', 0)}/{summary_doc.get('frame_descriptions', 0)}",
        "",
    ]

    if summary_doc.get("parse_error"):
        lines.extend(["## Error", "", str(summary_doc["parse_error"]), ""])

    summary = summary_doc.get("summary")
    if not isinstance(summary, dict):
        raw_output = summary_doc.get("raw_output") or ""
        if raw_output:
            lines.extend(["## Raw Output", "", "```text", str(raw_output), "```", ""])
        return "\n".join(lines)

    lines.extend(["## Content Summary", "", str(summary.get("content_summary") or ""), ""])

    timeline = as_list(summary.get("timeline"))
    if timeline:
        lines.extend(["## Timeline", "", "| Time Range | Event | Evidence Frames |", "| --- | --- | --- |"])
        for item in timeline:
            if not isinstance(item, dict):
                continue
            evidence = ", ".join(str(frame) for frame in as_list(item.get("evidence_frames")))
            lines.append(
                f"| {escape_table(item.get('time_range', ''))} | "
                f"{escape_table(item.get('event', ''))} | {escape_table(evidence)} |"
            )
        lines.append("")

    key_events = as_list(summary.get("key_events"))
    if key_events:
        lines.extend(["## Key Events", ""])
        lines.extend(f"- {event}" for event in key_events)
        lines.append("")

    risk_analysis = as_list(summary.get("risk_analysis"))
    if risk_analysis:
        lines.extend(["## Risk Analysis", "", "| Risk Type | Summary | Evidence | Confidence |", "| --- | --- | --- | --- |"])
        for item in risk_analysis:
            if not isinstance(item, dict):
                continue
            evidence = ", ".join(str(value) for value in as_list(item.get("evidence_time_ranges")))
            lines.append(
                f"| {escape_table(item.get('risk_type', ''))} | "
                f"{escape_table(item.get('summary', ''))} | "
                f"{escape_table(evidence)} | {escape_table(item.get('confidence', ''))} |"
            )
        lines.append("")

    recommendations = as_list(summary.get("recommendations"))
    if recommendations:
        lines.extend(["## Recommendations", ""])
        lines.extend(f"- {item}" for item in recommendations)
        lines.append("")

    uncertain_points = as_list(summary.get("uncertain_points"))
    if uncertain_points:
        lines.extend(["## Uncertain Points", ""])
        lines.extend(f"- {item}" for item in uncertain_points)
        lines.append("")

    return "\n".join(lines)


def write_video_error_summary(
    *,
    args: argparse.Namespace,
    video: VideoEntry,
    video_output_dir: Path,
    error: str,
) -> None:
    """在抽帧或汇总失败时，写入带 parse_error 的占位 summary 文件，保证结果完整可追踪。"""
    video_output_dir.mkdir(parents=True, exist_ok=True)
    summary_doc = {
        "video_id": video.video_id,
        "video_path": str(video.path),
        "provider": args.provider,
        "model": args.model if args.provider != "mock" else "mock",
        "frame_interval_seconds": args.frame_interval_seconds,
        "frame_descriptions": 0,
        "successful_frame_descriptions": 0,
        "summary_prompt_file": str(args.summary_prompt_file),
        "summary": None,
        "raw_output": "",
        "parse_error": error,
    }
    (video_output_dir / "video_summary.json").write_text(
        json.dumps(summary_doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (video_output_dir / "video_summary.md").write_text(render_summary_markdown(summary_doc), encoding="utf-8")


def process_video(
    *,
    args: argparse.Namespace,
    cv2,
    api_key: str,
    video: VideoEntry,
    frame_prompt_template: str,
    summary_prompt_template: str,
) -> dict[str, Any]:
    """处理单个视频：抽帧 -> 逐帧描述 -> 视频汇总，并写出 JSONL/JSON/MD。

    返回包含处理统计信息的字典（处理帧数、错误数、汇总路径等）。
    """
    video_output_dir = args.output_dir / video.video_id
    frame_jsonl = video_output_dir / "frame_descriptions.jsonl"
    summary_json = video_output_dir / "video_summary.json"
    summary_md = video_output_dir / "video_summary.md"

    try:
        metadata, frames = extract_frames(
            cv2=cv2,
            video=video,
            video_output_dir=video_output_dir,
            interval_seconds=args.frame_interval_seconds,
            limit_frames=args.limit_frames_per_video,
            no_resume=args.no_resume,
        )
    except Exception as exc:
        error = format_exception(exc)
        print(f"[error] {video.video_id}: {error}", flush=True)
        write_video_error_summary(args=args, video=video, video_output_dir=video_output_dir, error=error)
        return {"video_id": video.video_id, "processed_frames": 0, "frame_errors": 1, "summary_error": True}

    # 断点续跑：读取已完成的 frame_id；--no-resume 时全部重新处理
    done_ids = set() if args.no_resume else read_done_frame_ids(frame_jsonl)
    planned_frames = [frame for frame in frames if frame["frame_id"] not in done_ids or args.no_resume]
    print(
        f"[video] {video.video_id} frames={len(frames)} planned={len(planned_frames)} "
        f"duration={metadata.duration_seconds:.2f}s",
        flush=True,
    )

    processed_frames = 0
    frame_errors = 0
    # 逐帧调用视觉模型生成描述，结果按行写入 frame_descriptions.jsonl
    for frame in frames:
        frame_id = frame["frame_id"]
        if frame_id in done_ids and not args.no_resume:
            print(f"[skip] {video.video_id} {frame_id} already described", flush=True)
            continue

        prompt = render_frame_prompt(frame_prompt_template, video, frame)
        started = time.time()
        parsed: dict[str, Any] | None = None
        raw_text = ""
        error = None
        attempts = max(0, args.retry_attempts) + 1
        for attempt in range(attempts):
            attempt_prompt = prompt if attempt == 0 else prompt + RETRY_PROMPT_SUFFIX
            try:
                parsed, raw_text, error = describe_frame(args=args, api_key=api_key, prompt=attempt_prompt, frame=frame)
            except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                error = format_exception(exc)
            if not error:
                break
            if attempt + 1 < attempts:
                print(
                    f"[retry] {video.video_id} {frame_id} attempt={attempt + 2}/{attempts}: {error}",
                    flush=True,
                )
                if args.sleep_seconds > 0:
                    time.sleep(args.sleep_seconds)

        latency_ms = int((time.time() - started) * 1000)
        result = {
            "video_id": video.video_id,
            "video_path": str(video.path),
            "frame_id": frame_id,
            "frame_path": frame["frame_path"],
            "timestamp_seconds": frame["timestamp_seconds"],
            "timestamp": frame["timestamp"],
            "provider": args.provider,
            "model": args.model if args.provider != "mock" else "mock",
            "prompt_file": str(args.frame_prompt_file),
            "detail": args.detail,
            "max_output_tokens": args.frame_max_output_tokens,
            "attempts": attempts if error else attempt + 1,
            "latency_ms": latency_ms,
            "description": parsed,
            "raw_output": raw_text,
            "parse_error": error,
        }
        append_jsonl(frame_jsonl, result)
        processed_frames += 1
        if error:
            frame_errors += 1
            print(f"[error] {video.video_id} {frame_id}: {error}", flush=True)
        else:
            print(f"[ok] {video.video_id} {frame_id} {latency_ms}ms", flush=True)
        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    # 取最新的帧描述记录，过滤掉解析失败的帧
    frame_rows = latest_frame_rows(read_frame_description_rows(frame_jsonl))
    successful_rows = [row for row in frame_rows if is_successful_frame_row(row)]
    # 决定是否需要重新生成汇总：强制刷新 / 本轮有新帧 / 汇总不存在
    should_generate_summary = args.no_resume or processed_frames > 0 or summary_needs_retry(summary_json)
    summary_error = False

    if not successful_rows:
        summary_error = True
        error = "No successful frame descriptions are available for video summary."
        print(f"[summary-error] {video.video_id}: {error}", flush=True)
        write_video_error_summary(args=args, video=video, video_output_dir=video_output_dir, error=error)
    elif should_generate_summary:
        summary_prompt = render_summary_prompt(
            summary_prompt_template,
            video=video,
            metadata=metadata,
            interval_seconds=args.frame_interval_seconds,
            frame_rows=successful_rows,
        )
        started = time.time()
        parsed_summary: dict[str, Any] | None = None
        raw_summary = ""
        error = None
        attempts = max(0, args.retry_attempts) + 1
        for attempt in range(attempts):
            attempt_prompt = summary_prompt if attempt == 0 else summary_prompt + RETRY_PROMPT_SUFFIX
            try:
                parsed_summary, raw_summary, error = summarize_video(
                    args=args,
                    api_key=api_key,
                    prompt=attempt_prompt,
                    video=video,
                    frame_rows=successful_rows,
                )
            except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                error = format_exception(exc)
            if not error:
                break
            if attempt + 1 < attempts:
                print(
                    f"[summary-retry] {video.video_id} attempt={attempt + 2}/{attempts}: {error}",
                    flush=True,
                )
                if args.sleep_seconds > 0:
                    time.sleep(args.sleep_seconds)

        latency_ms = int((time.time() - started) * 1000)
        summary_doc = {
            "video_id": video.video_id,
            "video_path": str(video.path),
            "provider": args.provider,
            "model": args.model if args.provider != "mock" else "mock",
            "frame_interval_seconds": args.frame_interval_seconds,
            "video_metadata": {
                "fps": metadata.fps,
                "frame_count": metadata.frame_count,
                "duration_seconds": metadata.duration_seconds,
                "width": metadata.width,
                "height": metadata.height,
            },
            "frame_descriptions": len(frame_rows),
            "successful_frame_descriptions": len(successful_rows),
            "frame_descriptions_jsonl": str(frame_jsonl),
            "summary_prompt_file": str(args.summary_prompt_file),
            "max_output_tokens": args.summary_max_output_tokens,
            "attempts": attempts if error else attempt + 1,
            "latency_ms": latency_ms,
            "summary": parsed_summary,
            "raw_output": raw_summary,
            "parse_error": error,
        }
        summary_json.parent.mkdir(parents=True, exist_ok=True)
        summary_json.write_text(json.dumps(summary_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary_md.write_text(render_summary_markdown(summary_doc), encoding="utf-8")
        if error:
            summary_error = True
            print(f"[summary-error] {video.video_id}: {error}", flush=True)
        else:
            print(f"[summary-ok] {video.video_id} {latency_ms}ms", flush=True)
        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)
    else:
        print(f"[summary-skip] {video.video_id} existing summary retained", flush=True)

    return {
        "video_id": video.video_id,
        "processed_frames": processed_frames,
        "frame_errors": frame_errors,
        "summary_error": summary_error,
        "frame_descriptions": len(frame_rows),
        "successful_frame_descriptions": len(successful_rows),
        "summary_json": str(summary_json),
        "summary_md": str(summary_md),
    }


def dry_run(args: argparse.Namespace, cv2, videos: list[VideoEntry]) -> int:
    """干跑模式：仅打印每个视频的元数据与预计抽帧数，不写任何输出。"""
    planned: list[dict[str, Any]] = []
    for video in videos:
        try:
            metadata = load_video_metadata(cv2, video.path)
            timestamps = sample_timestamps(metadata.duration_seconds, args.frame_interval_seconds, args.limit_frames_per_video)
            planned.append(
                {
                    "video_id": video.video_id,
                    "video_path": str(video.path),
                    "duration_seconds": metadata.duration_seconds,
                    "fps": metadata.fps,
                    "source_frame_count": metadata.frame_count,
                    "planned_sampled_frames": len(timestamps),
                    "first_timestamps": [format_timestamp(value) for value in timestamps[:10]],
                }
            )
        except Exception as exc:
            planned.append(
                {
                    "video_id": video.video_id,
                    "video_path": str(video.path),
                    "metadata_error": format_exception(exc),
                }
            )
    print(
        json.dumps(
            {
                "provider": args.provider,
                "model": args.model if args.provider != "mock" else "mock",
                "video_dir": str(args.video_dir),
                "output_dir": str(args.output_dir),
                "frame_interval_seconds": args.frame_interval_seconds,
                "planned_videos": planned,
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


def parse_args() -> argparse.Namespace:
    """解析命令行参数；优先级：命令行 > 环境变量 > 默认值。.env 文件会被自动加载。"""
    load_env_file(Path(".env"))
    legacy_max_output_tokens = env_int("VIDEO_DESCRIPTION_MAX_OUTPUT_TOKENS", 0)
    frame_max_output_tokens = (
        legacy_max_output_tokens
        if legacy_max_output_tokens > 0
        else env_int("VIDEO_DESCRIPTION_FRAME_MAX_OUTPUT_TOKENS", DEFAULT_FRAME_MAX_OUTPUT_TOKENS)
    )
    summary_max_output_tokens = (
        legacy_max_output_tokens
        if legacy_max_output_tokens > 0
        else env_int("VIDEO_DESCRIPTION_SUMMARY_MAX_OUTPUT_TOKENS", DEFAULT_SUMMARY_MAX_OUTPUT_TOKENS)
    )
    parser = argparse.ArgumentParser(description="Sample videos into frames, describe each frame, and summarize each video.")
    parser.add_argument("--video-dir", type=Path, default=Path(os.environ.get("VIDEO_DESCRIPTION_VIDEO_DIR", DEFAULT_VIDEO_DIR)))
    parser.add_argument("--output-dir", type=Path, default=Path(os.environ.get("VIDEO_DESCRIPTION_OUTPUT_DIR", DEFAULT_OUTPUT_DIR)))
    parser.add_argument(
        "--frame-prompt-file",
        type=Path,
        default=Path(os.environ.get("VIDEO_FRAME_DESCRIPTION_PROMPT_FILE", DEFAULT_FRAME_PROMPT)),
    )
    parser.add_argument(
        "--summary-prompt-file",
        type=Path,
        default=Path(os.environ.get("VIDEO_SUMMARY_PROMPT_FILE", DEFAULT_SUMMARY_PROMPT)),
    )
    parser.add_argument(
        "--provider",
        choices=["openai", "openai_chat", "deepseek", "mock"],
        default=os.environ.get("LLM_PROVIDER", "openai"),
    )
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL", os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)))
    parser.add_argument(
        "--responses-url",
        default=os.environ.get("LLM_API_URL", os.environ.get("OPENAI_RESPONSES_URL", DEFAULT_OPENAI_RESPONSES_URL)),
    )
    parser.add_argument(
        "--chat-completions-url",
        default=os.environ.get("LLM_API_URL", os.environ.get("OPENAI_CHAT_COMPLETIONS_URL", DEFAULT_CHAT_COMPLETIONS_URL)),
    )
    parser.add_argument("--api-key-env", default=os.environ.get("LLM_API_KEY_ENV", "OPENAI_API_KEY"))
    parser.add_argument(
        "--frame-interval-seconds",
        type=float,
        default=env_float("VIDEO_DESCRIPTION_FRAME_INTERVAL_SECONDS", DEFAULT_FRAME_INTERVAL_SECONDS),
    )
    parser.add_argument("--limit-videos", type=int, default=env_int("VIDEO_DESCRIPTION_LIMIT_VIDEOS", 0))
    parser.add_argument("--offset-videos", type=int, default=env_int("VIDEO_DESCRIPTION_OFFSET_VIDEOS", 0))
    parser.add_argument(
        "--limit-frames-per-video",
        type=int,
        default=env_int("VIDEO_DESCRIPTION_LIMIT_FRAMES_PER_VIDEO", 0),
    )
    parser.add_argument("--detail", choices=["low", "high", "auto"], default=os.environ.get("VIDEO_DESCRIPTION_DETAIL", "low"))
    parser.add_argument("--timeout-seconds", type=int, default=env_int("VIDEO_DESCRIPTION_TIMEOUT_SECONDS", 60))
    parser.add_argument("--sleep-seconds", type=float, default=env_float("VIDEO_DESCRIPTION_SLEEP_SECONDS", 0.5))
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=legacy_max_output_tokens,
        help="Compatibility override for both frame and summary token budgets.",
    )
    parser.add_argument(
        "--frame-max-output-tokens",
        type=int,
        default=frame_max_output_tokens,
        help="Maximum output tokens for each frame description.",
    )
    parser.add_argument(
        "--summary-max-output-tokens",
        type=int,
        default=summary_max_output_tokens,
        help="Maximum output tokens for each video summary.",
    )
    parser.add_argument(
        "--retry-attempts",
        type=int,
        default=env_int("VIDEO_DESCRIPTION_RETRY_ATTEMPTS", DEFAULT_RETRY_ATTEMPTS),
        help="Retry failed model calls before writing a frame or summary error.",
    )
    parser.add_argument("--no-resume", action="store_true", help="Do not skip frame IDs already present in JSONL.")
    parser.add_argument("--dry-run", action="store_true", help="Print selected videos and estimated frames without writing outputs.")
    args = parser.parse_args()
    if args.max_output_tokens > 0:
        args.frame_max_output_tokens = args.max_output_tokens
        args.summary_max_output_tokens = args.max_output_tokens
    args.retry_attempts = max(0, args.retry_attempts)
    return args


def main() -> int:
    """入口函数：解析参数、扫描视频、循环处理每个视频并输出汇总统计。"""
    args = parse_args()
    print(
        f"[config] provider={args.provider} model={args.model if args.provider != 'mock' else 'mock'} "
        f"detail={args.detail}",
        flush=True,
    )
    print(
        f"[config] video_dir={args.video_dir} output_dir={args.output_dir} "
        f"interval={args.frame_interval_seconds}s",
        flush=True,
    )
    print(
        f"[config] frame_max_tokens={args.frame_max_output_tokens} "
        f"summary_max_tokens={args.summary_max_output_tokens} retry_attempts={args.retry_attempts}",
        flush=True,
    )

    try:
        videos = scan_videos(args.video_dir)
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"[error] {exc}", file=sys.stderr, flush=True)
        return 2

    # 按偏移与上限筛选本次要处理的视频子集
    selected = videos[args.offset_videos :]
    if args.limit_videos > 0:
        selected = selected[: args.limit_videos]
    print(f"[info] selected_videos={len(selected)} offset={args.offset_videos} limit={args.limit_videos}", flush=True)

    try:
        cv2 = import_cv2()
    except RuntimeError as exc:
        print(f"[error] {exc}", file=sys.stderr, flush=True)
        return 2

    if args.dry_run:
        return dry_run(args, cv2, selected)

    api_key = ""
    # 非 mock 提供方必须配置 API Key
    if args.provider in ("openai", "openai_chat", "deepseek"):
        api_key = os.environ.get(args.api_key_env, "")
        if not api_key:
            print(f"Missing API key. Set ${args.api_key_env} or use --provider mock.", file=sys.stderr, flush=True)
            return 2

    frame_prompt_template = args.frame_prompt_file.read_text(encoding="utf-8")
    summary_prompt_template = args.summary_prompt_file.read_text(encoding="utf-8")

    results: list[dict[str, Any]] = []
    # 依次处理每个视频
    for video in selected:
        print(f"[run] {video.video_id} -> {video.path}", flush=True)
        result = process_video(
            args=args,
            cv2=cv2,
            api_key=api_key,
            video=video,
            frame_prompt_template=frame_prompt_template,
            summary_prompt_template=summary_prompt_template,
        )
        results.append(result)

    # 汇总所有视频的统计；存在任意错误时返回退出码 1
    frame_errors = sum(int(result.get("frame_errors", 0)) for result in results)
    summary_errors = sum(1 for result in results if result.get("summary_error"))
    summary = {
        "videos": len(results),
        "processed_frames": sum(int(result.get("processed_frames", 0)) for result in results),
        "frame_errors": frame_errors,
        "summary_errors": summary_errors,
        "output_dir": str(args.output_dir),
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 1 if frame_errors or summary_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
