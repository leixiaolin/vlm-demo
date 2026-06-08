# 图片风险分析接口草案

## 1. 请求接口

`POST /api/v1/office-risk/images/analyze`

请求体字段遵循 `schemas/image_risk_analysis_request.schema.json`。

最小请求：

```json
{
  "image_id": "office_0001",
  "image_url": "https://example.com/sanitized/office_0001.jpg",
  "site_context": {
    "site_type": "indoor_office",
    "zone_type": "open_office",
    "restricted_area": false
  },
  "analysis_profile": "structured_schema_v1"
}
```

## 2. 响应接口

响应字段遵循 `schemas/image_risk_analysis_response.schema.json`。

最小响应：

```json
{
  "image_id": "office_0001",
  "scene_type": "open_office",
  "people_count_range": {"min": 3, "max": 5},
  "risks": [],
  "overall_severity": "none",
  "needs_review": false,
  "unsupported_claims": [],
  "privacy_flags": [],
  "model_info": {
    "provider": "cloud_vlm",
    "model": "vision_model",
    "prompt_profile": "structured_schema_v1"
  }
}
```

## 3. 风险等级

- `critical`：疑似危及人身安全或重大安防事件，必须人工复核。
- `high`：倒地、冲突、明火、摄像头遮挡等明确风险，默认人工复核。
- `medium`：禁区出现、疑似资料拍摄、异常聚集等需结合业务规则判断的风险。
- `low`：工牌缺失、轻微秩序异常等管理提醒。
- `none`：未发现可见风险。
- `unknown`：图片质量不足或证据不足，不能做判断。

## 4. 阈值策略

默认阈值：

- `confidence >= 0.85` 且 `severity in ["high", "critical"]`：进入高优先级复核。
- `confidence >= 0.75` 且 `severity == "medium"`：进入普通复核。
- `confidence < 0.75`：不触发正式告警，只记录为观察项。
- 任意风险缺少 `evidence`：不得进入自动告警，只能进入人工复核或失败案例库。

## 5. 人工复核规则

必须人工复核：

- `overall_severity` 为 `medium`、`high` 或 `critical`。
- `risks[].confidence < 0.85` 但风险类型属于倒地、冲突、明火、遮挡。
- `unsupported_claims` 非空，且模型提出了可能影响告警的不可确认信息。
- 图片质量不足但存在安全风险迹象。

复核结果建议记录：

- `review_result`: `confirmed`, `false_positive`, `uncertain`, `not_actionable`
- `reviewer_note`
- `corrected_risks`
- `prompt_issue`
- `data_issue`

## 6. 安全与合规约束

- 请求侧应去除 EXIF、地理位置等无关元数据。
- 真实办公图片默认先脱敏，尤其是人脸、屏幕内容、白板内容和纸质资料。
- 响应不得包含身份识别、敏感属性推断、主观动机判断或法律责任判断。
- 模型输出必须基于图片中可见内容，不能引用摄像头外的信息。

