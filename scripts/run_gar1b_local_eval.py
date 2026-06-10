#!/usr/bin/env python
"""Run local GAR-1B evaluation on images listed in a manifest."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_MODEL_ID = "GAR-1B"
DEFAULT_MANIFEST = Path("data/office_images/evaluation_manifest_collected.csv")
DEFAULT_PROMPT = Path("prompts/ovis_security_risk_detection_compact.md")
DEFAULT_OUTPUT = Path("outputs/gar1b_results.jsonl")
DEFAULT_SUMMARY = Path("outputs/gar1b_summary.json")
DEFAULT_MAX_PIXELS = 131072


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


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


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


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def resolve_image_path(value: str, root: Path) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    return root / candidate


def extract_json_object(text: str) -> tuple[dict[str, Any] | None, str | None]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        stripped = stripped.removeprefix("json").strip()
    stripped = stripped.replace("\n", " ").strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.S)
        if not match:
            return None, "no JSON object found in model output"
        candidate = match.group(0)
        if candidate.count("{") > candidate.count("}"):
            candidate += "}" * (candidate.count("{") - candidate.count("}"))
        if candidate.count("[") > candidate.count("]"):
            candidate += "]" * (candidate.count("[") - candidate.count("]"))
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            return None, str(exc)
    if not isinstance(parsed, dict):
        return None, "parsed output is not a JSON object"
    return parsed, None


def import_runtime():
    try:
        import torch
        from PIL import Image
        from transformers import AutoModelForCausalLM, AutoProcessor, AutoTokenizer
    except Exception as exc:
        raise RuntimeError("Missing local VLM dependencies. Install them with `pip install -r requirements.txt`.") from exc

    try:
        from transformers import AutoModelForVision2Seq
    except Exception:
        AutoModelForVision2Seq = None
    return torch, Image, AutoProcessor, AutoTokenizer, AutoModelForCausalLM, AutoModelForVision2Seq


def choose_device_dtype(torch, requested_device: str, requested_dtype: str):
    if requested_device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    else:
        device = requested_device

    if requested_dtype == "auto":
        dtype = torch.bfloat16 if device.startswith("cuda") else torch.float32
    else:
        dtype = {
            "float32": torch.float32,
            "float16": torch.float16,
            "bfloat16": torch.bfloat16,
        }[requested_dtype]
    return device, dtype


def tensor_to_device(value: Any, device: str, dtype) -> Any:
    if hasattr(value, "to"):
        if getattr(value, "dtype", None) is not None and str(value.dtype).startswith("torch.float"):
            return value.to(device).to(dtype)
        return value.to(device)
    if isinstance(value, dict):
        return {key: tensor_to_device(item, device, dtype) for key, item in value.items()}
    if isinstance(value, list):
        return [tensor_to_device(item, device, dtype) for item in value]
    return value


def load_model_and_processor(
    *,
    model_id: str,
    loader: str,
    device: str,
    dtype,
    torch,
    AutoProcessor,
    AutoTokenizer,
    AutoModelForCausalLM,
    AutoModelForVision2Seq,
):
    processor = None
    tokenizer = None
    try:
        processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
    except Exception:
        tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)

    if loader == "vision2seq":
        if AutoModelForVision2Seq is None:
            raise RuntimeError("This transformers version does not provide AutoModelForVision2Seq.")
        model_cls = AutoModelForVision2Seq
    else:
        model_cls = AutoModelForCausalLM

    model = model_cls.from_pretrained(
        model_id,
        torch_dtype=dtype,
        trust_remote_code=True,
    )
    model = model.to(device)
    model.eval()
    return model, processor, tokenizer


def decode_generated(model, processor, tokenizer, outputs, input_token_count: int | None) -> str:
    generated = outputs
    if input_token_count and hasattr(outputs, "__getitem__"):
        generated = outputs[:, input_token_count:]
    if processor is not None and hasattr(processor, "batch_decode"):
        return processor.batch_decode(generated, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]
    active_tokenizer = tokenizer or getattr(model, "text_tokenizer", None) or getattr(model, "tokenizer", None)
    if active_tokenizer is None:
        return str(outputs)
    return active_tokenizer.decode(generated[0], skip_special_tokens=True)


def run_with_ovis_style(
    *,
    model,
    torch,
    image,
    prompt: str,
    device: str,
    dtype,
    max_pixels: int,
    max_new_tokens: int,
    do_sample: bool,
) -> str:
    messages = [{"role": "user", "content": [{"type": "image", "image": image}, {"type": "text", "text": prompt}]}]
    input_ids, pixel_values, grid_thws = model.preprocess_inputs(
        messages=messages,
        add_generation_prompt=True,
        enable_thinking=False,
        max_pixels=max_pixels,
    )
    input_ids = input_ids.to(device)
    pixel_values = pixel_values.to(device).to(dtype) if pixel_values is not None else None
    grid_thws = grid_thws.to(device) if grid_thws is not None else None
    with torch.no_grad():
        outputs = model.generate(
            inputs=input_ids,
            pixel_values=pixel_values,
            grid_thws=grid_thws,
            max_new_tokens=max_new_tokens,
            do_sample=do_sample,
            eos_token_id=model.text_tokenizer.eos_token_id,
            pad_token_id=model.text_tokenizer.pad_token_id,
        )
    return model.text_tokenizer.decode(outputs[0], skip_special_tokens=True)


def run_with_processor(
    *,
    model,
    processor,
    tokenizer,
    torch,
    image,
    prompt: str,
    device: str,
    dtype,
    max_new_tokens: int,
    do_sample: bool,
) -> str:
    if processor is None:
        raise RuntimeError("AutoProcessor is unavailable for this model.")

    messages = [{"role": "user", "content": [{"type": "image", "image": image}, {"type": "text", "text": prompt}]}]
    if hasattr(processor, "apply_chat_template"):
        text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    else:
        text = prompt

    try:
        inputs = processor(text=[text], images=[image], return_tensors="pt")
    except TypeError:
        inputs = processor(images=image, text=text, return_tensors="pt")
    inputs = tensor_to_device(inputs, device, dtype)
    input_token_count = None
    if isinstance(inputs, dict) and "input_ids" in inputs and hasattr(inputs["input_ids"], "shape"):
        input_token_count = int(inputs["input_ids"].shape[-1])

    with torch.no_grad():
        outputs = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=do_sample)
    return decode_generated(model, processor, tokenizer, outputs, input_token_count)


def run_with_chat_method(
    *,
    model,
    processor,
    tokenizer,
    image,
    prompt: str,
    max_new_tokens: int,
    do_sample: bool,
) -> str:
    if not hasattr(model, "chat"):
        raise RuntimeError("model.chat is unavailable for this model.")
    generation_config = {"max_new_tokens": max_new_tokens, "do_sample": do_sample}
    candidates = [
        lambda: model.chat(image=image, text=prompt, generation_config=generation_config),
        lambda: model.chat(processor, image, prompt, generation_config=generation_config),
        lambda: model.chat(tokenizer or processor, image, prompt, generation_config=generation_config),
        lambda: model.chat(tokenizer or processor, prompt, image, generation_config=generation_config),
        lambda: model.chat(tokenizer or processor, prompt, images=[image], generation_config=generation_config),
    ]
    errors: list[str] = []
    for candidate in candidates:
        try:
            response = candidate()
            if isinstance(response, tuple):
                response = response[0]
            return str(response)
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError("model.chat attempts failed: " + " | ".join(errors[-3:]))


def run_one(
    *,
    model,
    processor,
    tokenizer,
    torch,
    Image,
    image_path: Path,
    prompt: str,
    device: str,
    dtype,
    max_pixels: int,
    max_new_tokens: int,
    do_sample: bool,
    infer_strategy: str,
) -> dict[str, Any]:
    image = Image.open(image_path).convert("RGB")
    generate_started = time.perf_counter()
    last_error = None

    strategies = [infer_strategy] if infer_strategy != "auto" else ["processor", "chat", "ovis"]
    for strategy in strategies:
        try:
            if strategy == "processor":
                raw_output = run_with_processor(
                    model=model,
                    processor=processor,
                    tokenizer=tokenizer,
                    torch=torch,
                    image=image,
                    prompt=prompt,
                    device=device,
                    dtype=dtype,
                    max_new_tokens=max_new_tokens,
                    do_sample=do_sample,
                )
            elif strategy == "chat":
                raw_output = run_with_chat_method(
                    model=model,
                    processor=processor,
                    tokenizer=tokenizer,
                    image=image,
                    prompt=prompt,
                    max_new_tokens=max_new_tokens,
                    do_sample=do_sample,
                )
            elif strategy == "ovis":
                raw_output = run_with_ovis_style(
                    model=model,
                    torch=torch,
                    image=image,
                    prompt=prompt,
                    device=device,
                    dtype=dtype,
                    max_pixels=max_pixels,
                    max_new_tokens=max_new_tokens,
                    do_sample=do_sample,
                )
            else:
                raise ValueError(f"Unsupported infer strategy: {strategy}")
            generate_seconds = time.perf_counter() - generate_started
            return {
                "raw_output": raw_output,
                "generate_seconds": round(generate_seconds, 4),
                "image_width": image.width,
                "image_height": image.height,
                "infer_strategy": strategy,
            }
        except Exception as exc:
            last_error = exc

    raise RuntimeError(str(last_error))


def parse_args() -> argparse.Namespace:
    load_env_file(Path(".env"))
    parser = argparse.ArgumentParser(description="Deploy and evaluate local GAR-1B over office images.")
    parser.add_argument("--model-id", default=os.environ.get("GAR_MODEL_ID", DEFAULT_MODEL_ID))
    parser.add_argument("--manifest", type=Path, default=Path(os.environ.get("GAR_EVAL_MANIFEST", DEFAULT_MANIFEST)))
    parser.add_argument("--prompt-file", type=Path, default=Path(os.environ.get("GAR_EVAL_PROMPT_FILE", DEFAULT_PROMPT)))
    parser.add_argument("--output-jsonl", type=Path, default=Path(os.environ.get("GAR_EVAL_OUTPUT", DEFAULT_OUTPUT)))
    parser.add_argument("--summary-json", type=Path, default=Path(os.environ.get("GAR_EVAL_SUMMARY", DEFAULT_SUMMARY)))
    parser.add_argument("--limit", type=int, default=env_int("GAR_EVAL_LIMIT", 3))
    parser.add_argument("--offset", type=int, default=env_int("GAR_EVAL_OFFSET", 0))
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default=os.environ.get("GAR_DEVICE", "auto"))
    parser.add_argument("--dtype", choices=["auto", "float32", "float16", "bfloat16"], default=os.environ.get("GAR_DTYPE", "auto"))
    parser.add_argument("--loader", choices=["causal", "vision2seq"], default=os.environ.get("GAR_LOADER", "causal"))
    parser.add_argument("--infer-strategy", choices=["auto", "processor", "chat", "ovis"], default=os.environ.get("GAR_INFER_STRATEGY", "auto"))
    parser.add_argument("--max-pixels", type=int, default=env_int("GAR_MAX_PIXELS", DEFAULT_MAX_PIXELS))
    parser.add_argument("--max-new-tokens", type=int, default=env_int("GAR_MAX_NEW_TOKENS", 128))
    parser.add_argument("--do-sample", action="store_true", default=env_bool("GAR_DO_SAMPLE", False))
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace_root = Path.cwd()
    manifest_rows = read_manifest(args.manifest)
    selected = manifest_rows[args.offset :]
    if args.limit > 0:
        selected = selected[: args.limit]

    done_ids = set() if args.no_resume else read_done_ids(args.output_jsonl)
    planned = [row.get("image_id", "") for row in selected if row.get("image_id", "") not in done_ids]
    print(
        json.dumps(
            {
                "model_id": args.model_id,
                "manifest": str(args.manifest),
                "prompt_file": str(args.prompt_file),
                "output_jsonl": str(args.output_jsonl),
                "selected": len(selected),
                "planned": planned,
                "device": args.device,
                "dtype": args.dtype,
                "loader": args.loader,
                "infer_strategy": args.infer_strategy,
                "max_pixels": args.max_pixels,
                "max_new_tokens": args.max_new_tokens,
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    if args.dry_run:
        return 0

    prompt = args.prompt_file.read_text(encoding="utf-8")
    torch, Image, AutoProcessor, AutoTokenizer, AutoModelForCausalLM, AutoModelForVision2Seq = import_runtime()
    device, dtype = choose_device_dtype(torch, args.device, args.dtype)
    if device == "cuda" and not torch.cuda.is_available():
        print("CUDA was requested but torch.cuda.is_available() is false.", file=sys.stderr)
        return 2

    print(f"[load] model={args.model_id} loader={args.loader} device={device} dtype={dtype}", flush=True)
    load_started = time.perf_counter()
    model, processor, tokenizer = load_model_and_processor(
        model_id=args.model_id,
        loader=args.loader,
        device=device,
        dtype=dtype,
        torch=torch,
        AutoProcessor=AutoProcessor,
        AutoTokenizer=AutoTokenizer,
        AutoModelForCausalLM=AutoModelForCausalLM,
        AutoModelForVision2Seq=AutoModelForVision2Seq,
    )
    load_seconds = time.perf_counter() - load_started
    print(f"[load] completed in {load_seconds:.2f}s", flush=True)

    results: list[dict[str, Any]] = []
    runtime_errors = 0
    for row in selected:
        image_id = row.get("image_id", "")
        if not image_id or (image_id in done_ids and not args.no_resume):
            continue
        image_path = resolve_image_path(row.get("image_path_or_url", ""), workspace_root)
        print(f"[run] {image_id} {image_path}", flush=True)
        started = time.perf_counter()
        error = None
        parsed = None
        raw_output = ""
        metrics: dict[str, Any] = {}
        try:
            if not image_path.exists():
                raise FileNotFoundError(str(image_path))
            metrics = run_one(
                model=model,
                processor=processor,
                tokenizer=tokenizer,
                torch=torch,
                Image=Image,
                image_path=image_path,
                prompt=prompt,
                device=device,
                dtype=dtype,
                max_pixels=args.max_pixels,
                max_new_tokens=args.max_new_tokens,
                do_sample=args.do_sample,
                infer_strategy=args.infer_strategy,
            )
            raw_output = metrics.pop("raw_output")
            parsed, error = extract_json_object(raw_output)
        except Exception as exc:
            error = str(exc)
            runtime_errors += 1

        total_seconds = time.perf_counter() - started
        result = {
            "image_id": image_id,
            "image_path": str(image_path),
            "model_id": args.model_id,
            "model_label": "GAR-1B",
            "device": device,
            "dtype": str(dtype).replace("torch.", ""),
            "loader": args.loader,
            "total_seconds": round(total_seconds, 4),
            "parsed_output": parsed,
            "raw_output": raw_output,
            "parse_error": error,
            **metrics,
        }
        append_jsonl(args.output_jsonl, result)
        results.append(result)
        status = "error" if error else "ok"
        print(f"[{status}] {image_id} {total_seconds:.2f}s {error or ''}", flush=True)

    successful = [row for row in results if not row.get("parse_error")]
    total_times = [row["total_seconds"] for row in results]
    generate_times = [row.get("generate_seconds") for row in results if isinstance(row.get("generate_seconds"), (int, float))]
    summary = {
        "model_id": args.model_id,
        "model_label": "GAR-1B",
        "device": device,
        "dtype": str(dtype).replace("torch.", ""),
        "loader": args.loader,
        "infer_strategy": args.infer_strategy,
        "load_seconds": round(load_seconds, 4),
        "processed": len(results),
        "successful_parses": len(successful),
        "errors": runtime_errors + (len(results) - len(successful)),
        "avg_total_seconds": round(sum(total_times) / len(total_times), 4) if total_times else None,
        "avg_generate_seconds": round(sum(generate_times) / len(generate_times), 4) if generate_times else None,
        "output_jsonl": str(args.output_jsonl),
    }
    args.summary_json.parent.mkdir(parents=True, exist_ok=True)
    args.summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
