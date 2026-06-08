#!/usr/bin/env python
"""Run batch image-description tests over a manifest.

Uses the OpenAI Responses API by default. Configuration is loaded from .env
first, then normal environment variables and CLI flags can override it.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_PROMPT = Path("prompts/image_description.md")
DEFAULT_MANIFEST = Path("data/office_images/evaluation_manifest_collected.csv")
DEFAULT_OUTPUT = Path("outputs/image_description_results.jsonl")


class ProviderConfigurationError(RuntimeError):
    pass


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(dotenv_path=path, override=False)
        return
    except Exception:
        pass

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def read_manifest(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_done_ids(path: Path) -> set[str]:
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
            image_id = row.get("image_id")
            if image_id:
                done.add(str(image_id))
    return done


def image_to_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    raw = path.read_bytes()
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def extract_output_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]

    texts: list[str] = []
    for output in payload.get("output", []) or []:
        for content in output.get("content", []) or []:
            if isinstance(content, dict):
                if isinstance(content.get("text"), str):
                    texts.append(content["text"])
                elif isinstance(content.get("output_text"), str):
                    texts.append(content["output_text"])
    return "\n".join(texts).strip()


def parse_model_json(text: str) -> tuple[dict[str, Any] | None, str | None]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        stripped = stripped.removeprefix("json").strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return None, str(exc)
    if not isinstance(parsed, dict):
        return None, "model output is not a JSON object"
    return parsed, None


def format_exception(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        if body:
            return f"HTTP Error {exc.code}: {body}"
        return f"HTTP Error {exc.code}: {exc.reason}"
    return str(exc)


def call_chat_completions(
    *,
    api_url: str,
    api_key: str,
    model: str,
    prompt: str,
    image_data_url: str,
    detail: str,
    timeout: int,
    max_output_tokens: int,
) -> dict[str, Any]:
    """调用 OpenAI 兼容的多模态 Chat Completions API。"""
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url, "detail": detail}},
                ],
            }
        ],
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
    print(f"[debug] API status={response.status} body_len={len(raw_resp)}", flush=True)
    if not raw_resp.strip():
        print(f"[debug] API returned empty body", flush=True)
        return {"output_text": ""}
    try:
        data = json.loads(raw_resp)
    except json.JSONDecodeError:
        print(f"[debug] API response (first 500 chars): {raw_resp[:500]}", flush=True)
        raise
    # 将 Chat Completions 响应统一为脚本期望的格式
    text = ""
    choices = data.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        # 推理模型（如 GLM-5V-Turbo）思考内容在 reasoning_content，最终输出在 content
        text = msg.get("content", "") or ""
        if not text:
            text = msg.get("reasoning_content", "") or ""
    if not text:
        print(f"[debug] Full API response: {json.dumps(data, ensure_ascii=False)[:500]}", flush=True)
    return {"output_text": text}


def call_openai(
    *,
    responses_url: str,
    api_key: str,
    model: str,
    prompt: str,
    image_data_url: str,
    detail: str,
    timeout: int,
    max_output_tokens: int,
) -> dict[str, Any]:
    body = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": image_data_url, "detail": detail},
                ],
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


def mock_description(row: dict[str, str]) -> dict[str, Any]:
    return {
        "scene_type": row.get("scene_type") or "unknown",
        "short_caption": "Mock description for pipeline validation.",
        "detailed_description": "This mock result verifies manifest reading, JSONL writing, resume behavior, and downstream review workflow without calling a remote model.",
        "people_count_range": {"min": 1, "max": 5},
        "visible_activities": ["unknown"],
        "visible_objects": ["unknown"],
        "privacy_flags": ["none"],
        "image_quality": "fair",
        "uncertain_points": ["Mock provider does not inspect image pixels."],
    }


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def resolve_image_path(value: str, root: Path) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    return root / candidate


def parse_args() -> argparse.Namespace:
    load_env_file(Path(".env"))
    parser = argparse.ArgumentParser(description="Batch test image description over collected office images.")
    parser.add_argument("--manifest", type=Path, default=Path(os.environ.get("IMAGE_DESCRIPTION_MANIFEST", DEFAULT_MANIFEST)))
    parser.add_argument("--output-jsonl", type=Path, default=Path(os.environ.get("IMAGE_DESCRIPTION_OUTPUT", DEFAULT_OUTPUT)))
    parser.add_argument("--prompt-file", type=Path, default=Path(os.environ.get("IMAGE_DESCRIPTION_PROMPT_FILE", DEFAULT_PROMPT)))
    parser.add_argument("--provider", choices=["openai", "openai_chat", "deepseek", "mock"], default=os.environ.get("LLM_PROVIDER", "openai"))
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
    parser.add_argument("--limit", type=int, default=env_int("IMAGE_DESCRIPTION_LIMIT", 5), help="Number of images to process in this run.")
    parser.add_argument("--offset", type=int, default=env_int("IMAGE_DESCRIPTION_OFFSET", 0))
    parser.add_argument("--detail", choices=["low", "high", "auto"], default=os.environ.get("IMAGE_DESCRIPTION_DETAIL", "low"))
    parser.add_argument("--timeout-seconds", type=int, default=env_int("IMAGE_DESCRIPTION_TIMEOUT_SECONDS", 60))
    parser.add_argument("--sleep-seconds", type=float, default=env_float("IMAGE_DESCRIPTION_SLEEP_SECONDS", 0.5))
    parser.add_argument("--max-output-tokens", type=int, default=env_int("IMAGE_DESCRIPTION_MAX_OUTPUT_TOKENS", 700))
    parser.add_argument("--no-resume", action="store_true", help="Do not skip image IDs already present in output JSONL.")
    parser.add_argument("--dry-run", action="store_true", help="Print selected image IDs and exit without model calls.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print(f"[config] provider={args.provider} model={args.model} detail={args.detail}", flush=True)
    print(f"[config] manifest={args.manifest} prompt={args.prompt_file} output={args.output_jsonl}", flush=True)
    print(f"[config] limit={args.limit} offset={args.offset} sleep={args.sleep_seconds}s timeout={args.timeout_seconds}s", flush=True)

    workspace_root = Path.cwd()
    manifest_rows = read_manifest(args.manifest)
    print(f"[info] 读取到 {len(manifest_rows)} 条清单记录", flush=True)

    prompt = args.prompt_file.read_text(encoding="utf-8")
    done_ids = set() if args.no_resume else read_done_ids(args.output_jsonl)
    if done_ids:
        print(f"[info] 已完成 {len(done_ids)} 条，将跳过: {sorted(done_ids)}", flush=True)

    selected = manifest_rows[args.offset :]
    if args.limit > 0:
        selected = selected[: args.limit]
    print(f"[info] 本次计划处理 {len(selected)} 条 (offset={args.offset}, limit={args.limit})", flush=True)

    planned = [row.get("image_id", "") for row in selected if row.get("image_id", "") not in done_ids]
    print(f"[info] 去重后实际待处理 {len(planned)} 条: {planned}", flush=True)
    if args.dry_run:
        print(json.dumps({"provider": args.provider, "model": args.model, "planned_image_ids": planned}, ensure_ascii=False, indent=2))
        return 0

    api_key = ""
    if args.provider in ("openai", "openai_chat", "deepseek"):
        api_key = os.environ.get(args.api_key_env, "")
        if not api_key:
            print(f"Missing API key. Set ${args.api_key_env} or use --provider mock.", file=sys.stderr)
            return 2

    processed = 0
    errors = 0
    for row in selected:
        image_id = row.get("image_id", "")
        if not image_id:
            print(f"[skip] 记录缺少 image_id: {row}", flush=True)
            continue
        if image_id in done_ids and not args.no_resume:
            print(f"[skip] {image_id} 已处理过，跳过", flush=True)
            continue

        image_path = resolve_image_path(row.get("image_path_or_url", ""), workspace_root)
        print(f"[run] 正在处理 {image_id} -> {image_path}", flush=True)
        started = time.time()
        result: dict[str, Any]
        raw_text = ""
        error = None
        parsed: dict[str, Any] | None = None

        try:
            if args.provider == "mock":
                parsed = mock_description(row)
                raw_text = json.dumps(parsed, ensure_ascii=False)
            elif args.provider in ("openai_chat", "deepseek"):
                if not image_path.exists():
                    raise FileNotFoundError(str(image_path))
                payload = call_chat_completions(
                    api_url=args.chat_completions_url,
                    api_key=api_key,
                    model=args.model,
                    prompt=prompt,
                    image_data_url=image_to_data_url(image_path),
                    detail=args.detail,
                    timeout=args.timeout_seconds,
                    max_output_tokens=args.max_output_tokens,
                )
                raw_text = extract_output_text(payload)
                parsed, error = parse_model_json(raw_text)
            else:
                if not image_path.exists():
                    raise FileNotFoundError(str(image_path))
                payload = call_openai(
                    responses_url=args.responses_url,
                    api_key=api_key,
                    model=args.model,
                    prompt=prompt,
                    image_data_url=image_to_data_url(image_path),
                    detail=args.detail,
                    timeout=args.timeout_seconds,
                    max_output_tokens=args.max_output_tokens,
                )
                raw_text = extract_output_text(payload)
                parsed, error = parse_model_json(raw_text)
        except (ProviderConfigurationError, OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            error = format_exception(exc)

        latency_ms = int((time.time() - started) * 1000)
        result = {
            "image_id": image_id,
            "image_path": str(image_path),
            "provider": args.provider,
            "model": args.model if args.provider != "mock" else "mock",
            "prompt_file": str(args.prompt_file),
            "detail": args.detail,
            "latency_ms": latency_ms,
            "description": parsed,
            "raw_output": raw_text,
            "parse_error": error,
        }
        append_jsonl(args.output_jsonl, result)
        processed += 1
        if error:
            errors += 1
            print(f"[error] {image_id}: {error}", flush=True)
        else:
            print(f"[ok] {image_id} {latency_ms}ms", flush=True)
        time.sleep(args.sleep_seconds)

    print(json.dumps({"processed": processed, "errors": errors, "output_jsonl": str(args.output_jsonl)}, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
