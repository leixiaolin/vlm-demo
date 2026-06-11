# 本地部署 GAR-1B 并测试 data 图片

本流程用于把 GAR-1B 作为本地多模态模型接入当前办公场所图片风险初筛评测。脚本读取 `data/office_images/evaluation_manifest_collected.csv`，逐张处理 `data/office_images/images/` 下的图片，并输出 JSONL 结果和汇总 JSON。

## 1. 配置模型路径

公开模型仓库为 `HaochenWang/GAR-1B`。可直接使用该 Hugging Face ID，或先把权重下载到本机后设置本地目录：

```powershell
$env:GAR_MODEL_ID="HaochenWang/GAR-1B"
```

也可以写入 `.env`：

```dotenv
GAR_MODEL_ID=HaochenWang/GAR-1B
```

脚本默认使用 `AutoModel`、`trust_remote_code=True`，并关闭 flash attention 以兼容 CPU/Windows 环境。GAR 是区域理解模型；本评测脚本会自动构造整图 mask，把整张 data 图片作为 `<Prompt0>` 区域进行风险初筛。

## 2. 先做 dry-run

```powershell
python scripts/run_gar1b_local_eval.py --dry-run --limit 3
```

这一步只验证 data 清单、图片选择和输出路径，不加载模型。

## 3. 跑 1 张冒烟测试

```powershell
python scripts/run_gar1b_local_eval.py `
  --limit 1 `
  --no-resume `
  --prompt-file prompts/ovis_security_risk_detection_compact.md `
  --max-new-tokens 128 `
  --max-pixels 131072
```

脚本会优先使用 GAR 官方风格的整图 mask 推理；若指定其他兼容模型，也可尝试通用推理方式：

- `gar`：整图 mask + GAR `model.generate(...)`
- `processor`：`AutoProcessor.apply_chat_template(...)` + `model.generate(...)`
- `chat`：模型自带 `model.chat(...)`
- `ovis`：兼容已有 Ovis 风格的 `preprocess_inputs(...)`

如果需要固定方式，可以用 `--infer-strategy gar|processor|chat|ovis` 指定。

## 4. 跑 data 目录图片测试

```powershell
python scripts/run_gar1b_local_eval.py `
  --limit 15 `
  --output-jsonl outputs/gar1b_data15_results.jsonl `
  --summary-json outputs/gar1b_data15_summary.json
```

汇总报告：

```powershell
python scripts/summarize_ovis25_results.py `
  --input-jsonl outputs/gar1b_data15_results.jsonl `
  --output-md outputs/gar1b_data15_report.md `
  --title "GAR-1B Local Evaluation Report" `
  --note "This run used the compact office security-risk prompt." `
  --note "JSON parse success does not guarantee business correctness; findings still require manual review."
```

## 5. 输出文件

- `outputs/gar1b_results.jsonl` 或你通过 `--output-jsonl` 指定的文件
- `outputs/gar1b_summary.json` 或你通过 `--summary-json` 指定的文件
- `outputs/gar1b_data15_report.md`

## 6. Windows torch 排障

如果运行时报：

```text
OSError: [WinError 126] Error loading "...torch\lib\fbgemm.dll" or one of its dependencies.
```

说明当前 Python 环境的 PyTorch 原生依赖不完整。需要先修复 `torch` 导入，再运行 GAR-1B：

```powershell
python -c "import torch; print(torch.__version__)"
```

常见处理方式是安装完整的 Microsoft Visual C++/OpenMP 运行库，或更换为当前机器可导入的 PyTorch wheel。本仓库当前验证通过的 Windows CPU 组合是：

```text
torch==2.12.0
torchvision==0.27.0
transformers==4.57.3
```

只有 `import torch` 成功后，本地 GAR-1B 和 Ovis 这类 VLM 测试才能继续。
