# 室内办公公开图片采集说明

本仓库提供 `scripts/collect_office_images.py`，用于从 Openverse 与 Wikimedia Commons API 收集公开授权的室内办公图片。脚本会记录来源和许可信息，输出可接入现有评测流程的 manifest。

## 设计原则

- 只通过公开媒体 API 收集，不爬取任意网页。
- 默认目标总量 200 张，可调整到 150-300 张。脚本支持断点续跑：如果已有 80 张，执行 `--target-count 150` 会继续补到 150 张。
- 搜索词聚焦 `office + people + working/meeting/discussion`，提高“图片中有人在活动”的命中率。
- 下载后尽量做本地脱敏：如果安装了 Pillow，会重新编码图片并去除 EXIF/元数据；如果额外安装 OpenCV，可启用人脸模糊。
- 所有样本仍需人工复核，确认确实是室内办公、有人活动、可用于预研评测。

## 运行方式

默认收集到 200 张：

```powershell
python scripts/collect_office_images.py
```

收集到 150 张，并启用可选人脸模糊：

```powershell
python scripts/collect_office_images.py --target-count 150 --blur-faces
```

收集到 300 张，使用自定义搜索词：

```powershell
python scripts/collect_office_images.py --target-count 300 --query-file data/office_image_queries.txt
```

如果网络较慢，可以降低缩略图宽度：

```powershell
python scripts/collect_office_images.py --target-count 150 --thumb-width 900 --delay-seconds 0.2
```

如果只希望使用 Wikimedia Commons：

```powershell
python scripts/collect_office_images.py --target-count 150 --sources commons
```

只查看计划，不访问网络：

```powershell
python scripts/collect_office_images.py --dry-run
```

## 输出文件

- `data/office_images/images/`：下载并处理后的图片。
- `data/office_images/evaluation_manifest_collected.csv`：兼容预研评测集的样本清单。
- `data/office_images/source_metadata.csv`：图片来源、许可、描述页、下载 URL、SHA256 和尺寸信息。

## 可选依赖

脚本没有强制第三方依赖。推荐安装：

- `Pillow`：重新编码图片并去除 EXIF/元数据。
- `opencv-python`：配合 `--blur-faces` 做基础人脸模糊。

如果未安装 Pillow，脚本仍可保存公开来源图片；若希望强制所有图片都经过元数据清理，可使用：

```powershell
python scripts/collect_office_images.py --require-sanitization
```

## 人工复核建议

采集完成后，建议人工筛掉以下图片：

- 无人、仅办公家具、办公楼外景、Logo、图标、平面图。
- 非室内办公场景，例如工厂车间、户外活动、家庭办公。
- 人脸或屏幕内容过于清晰且未脱敏的图片。
- 与风险行为评测无关或画质过低的图片。

人工确认后，再把可用样本合并到 `data/evaluation_manifest.csv` 或作为正式评测集单独保存。
