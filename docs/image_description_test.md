# 图片描述测试流程

图片描述测试用于先验证模型是否能稳定、客观地描述室内办公图片。它先不做风险判定，重点检查场景识别、人员数量范围、人员活动、可见物体、隐私标记和不确定项。

## 1. 抽样检查图片清单

确认正式图片都在 manifest 中：

```powershell
(Import-Csv data\office_images\evaluation_manifest_collected.csv).Count
Get-ChildItem data\office_images\images -File | Measure-Object
```

不要使用 `data/office_images/orphaned_after_timeout/` 里的图片做正式测试，因为这些文件缺少来源记录。

## 2. 先用 mock 跑通流程

建议先创建虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

```powershell
python scripts/run_image_description_test.py `
  --provider mock `
  --limit 3 `
  --output-jsonl outputs/image_description_mock.jsonl
```

这个命令不调用云端模型，只验证 manifest 读取、结果 JSONL 写入和断点续跑逻辑。

## 3. 调用视觉模型

大模型参数统一放在 `.env` 中：

```text
LLM_PROVIDER=openai
LLM_MODEL=gpt-4.1-mini
OPENAI_API_KEY=你的 API Key
IMAGE_DESCRIPTION_LIMIT=5
IMAGE_DESCRIPTION_DETAIL=low
```

配置好 `.env` 后运行：

```powershell
python scripts/run_image_description_test.py
```

注意：DeepSeek 官方 Chat Completions API 当前不适合本步骤的图片描述测试，因为该接口的 `messages[].content` 是文本字符串，不支持脚本需要的 `image_url` 图片输入。若使用 OpenAI-compatible 平台，必须选择明确支持视觉输入的模型，并设置：

```text
LLM_PROVIDER=openai_chat
LLM_MODEL=你的视觉模型
OPENAI_CHAT_COMPLETIONS_URL=你的 /chat/completions 地址
```

也可以用命令行临时覆盖 `.env`：

```powershell
python scripts/run_image_description_test.py `
  --provider openai `
  --model gpt-4.1-mini `
  --limit 5 `
  --detail low `
  --output-jsonl outputs/image_description_results.jsonl
```

确认结果正常后再扩大批量：

```powershell
python scripts/run_image_description_test.py `
  --provider openai `
  --model gpt-4.1-mini `
  --limit 50 `
  --detail low `
  --output-jsonl outputs/image_description_results.jsonl
```

脚本默认会跳过 `output-jsonl` 中已经处理过的 `image_id`，所以可以分批续跑。

## 4. 复核结果

重点抽查：

- `scene_type` 是否真是室内办公相关场景。
- `short_caption` 和 `detailed_description` 是否只描述可见事实。
- `people_count_range` 是否合理，不要求精确人数。
- `visible_activities` 是否能体现人在活动，例如开会、办公、走动、使用电脑。
- `privacy_flags` 是否标记了人脸、屏幕、白板、纸质资料、工牌。
- `uncertain_points` 是否能承认看不清或无法判断的内容。

## 5. 进入风险识别测试

图片描述测试通过后，再用 `prompts/structured_schema.md` 做风险识别。描述测试的作用是先筛掉非办公、无人、画质差或模型描述不稳定的样本，避免直接进入风险测试时误报太多。
