# 本地部署 Ovis2.5-2B 并测试 data 图片

本流程使用 Hugging Face Transformers 本地加载 `AIDC-AI/Ovis2.5-2B`，对 `data/office_images/evaluation_manifest_collected.csv` 中的图片逐张执行办公安全风险检测，并记录延迟、解析结果和原始输出。

官方模型卡给出的关键用法是：

- 模型 ID：`AIDC-AI/Ovis2.5-2B`
- 加载方式：`AutoModelForCausalLM.from_pretrained(..., trust_remote_code=True)`
- 单图输入：`messages=[{"role":"user","content":[{"type":"image","image": Image.open(...)},{"type":"text","text": "..."}]}]`
- 推荐依赖：`torch==2.4.0`、`transformers==4.51.3`、`numpy==1.25.0`、`pillow==10.3.0`、`moviepy==1.0.3`

## 1. 安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Windows/CPU 环境不要安装 `flash-attn`。如果有 CUDA GPU，可按你的 CUDA 版本安装对应 PyTorch。

## 2. 先做 dry-run

```powershell
python scripts/run_ovis25_local_eval.py --dry-run --limit 3
```

## 3. 跑 1 张冒烟测试

```powershell
python scripts/run_ovis25_local_eval.py --limit 1 --no-resume --max-new-tokens 512 --max-pixels 262144
```

CPU 环境建议先用短 prompt 降低输出长度：

```powershell
python scripts/run_ovis25_local_eval.py `
  --limit 1 `
  --no-resume `
  --prompt-file prompts/ovis_security_risk_detection_compact.md `
  --max-new-tokens 192 `
  --max-pixels 131072
```

## 4. 跑 data 目录图片性能测试

```powershell
python scripts/run_ovis25_local_eval.py --limit 15 --max-new-tokens 768 --max-pixels 262144
```

输出文件：

- `outputs/ovis25_2b_results.jsonl`
- `outputs/ovis25_2b_summary.json`

汇总 JSONL 结果：

```powershell
python scripts/summarize_ovis25_results.py `
  --input-jsonl outputs/ovis25_2b_data15_results.jsonl `
  --output-md outputs/ovis25_2b_data15_report.md
```

## 5. 性能指标

脚本记录：

- 模型加载耗时 `load_seconds`
- 每张图片总耗时 `total_seconds`
- 预处理耗时 `preprocess_seconds`
- 生成耗时 `generate_seconds`
- 输出 token 数 `output_token_count`
- JSON 解析是否成功

如果机器没有 GPU，CPU 跑 2B 多模态模型会很慢，冒烟测试可能也需要数分钟。
