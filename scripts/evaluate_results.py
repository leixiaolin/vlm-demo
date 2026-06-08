#!/usr/bin/env python
"""评估结构化办公图像风险分析预测结果。

本脚本仅使用 Python 标准库，确保研究包可以在纯净环境中运行。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

# 高置信度阈值：置信度 >= 0.85 的风险视为高置信度告警
HIGH_CONFIDENCE_THRESHOLD = 0.85
# 中等审查阈值：置信度 >= 0.75 的风险需要人工审查
MEDIUM_REVIEW_THRESHOLD = 0.75
# 需要关注的风险严重级别
ALERT_SEVERITIES = {"medium", "high", "critical"}
# 预测结果中必须包含的顶层字段
REQUIRED_RESPONSE_FIELDS = {
    "image_id",
    "scene_type",
    "people_count_range",
    "risks",
    "overall_severity",
    "needs_review",
    "unsupported_claims",
    "privacy_flags",
    "model_info",
}
# 每个风险项必须包含的字段
REQUIRED_RISK_FIELDS = {"risk_type", "severity", "confidence", "evidence", "needs_review"}


@dataclass
class PredictionCheck:
    """预测结果的合规性检查结果。"""
    compliant: bool  # 是否符合 schema 规范
    errors: list[str]  # 不合规时的错误信息列表


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL 文件，每行一个 JSON 对象。

    Args:
        path: JSONL 文件路径

    Returns:
        包含所有 JSON 对象的列表

    Raises:
        ValueError: 当某行不是合法的 JSON 或不是 JSON 对象时抛出
    """
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no} is not valid JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no} must contain a JSON object")
            rows.append(row)
    return rows


def validate_prediction(row: dict[str, Any]) -> PredictionCheck:
    """验证单条预测结果是否符合 schema 规范。

    检查内容包括：
    - 必填字段是否齐全
    - people_count_range 是否包含 min/max 且为整数
    - risks 列表中每项是否包含必填字段
    - confidence 是否在 [0, 1] 范围内
    - evidence 是否非空

    Args:
        row: 单条预测结果字典

    Returns:
        PredictionCheck 包含合规状态和错误列表
    """
    errors: list[str] = []
    # 检查顶层必填字段
    missing = REQUIRED_RESPONSE_FIELDS - set(row)
    if missing:
        errors.append(f"missing response fields: {sorted(missing)}")

    # 检查 people_count_range 字段格式
    people_range = row.get("people_count_range")
    if not isinstance(people_range, dict) or not {"min", "max"} <= set(people_range):
        errors.append("people_count_range must contain min and max")
    elif not isinstance(people_range.get("min"), int) or not isinstance(people_range.get("max"), int):
        errors.append("people_count_range min/max must be integers")

    # 检查 risks 列表中每一项的字段完整性
    risks = row.get("risks")
    if not isinstance(risks, list):
        errors.append("risks must be a list")
    else:
        for idx, risk in enumerate(risks):
            if not isinstance(risk, dict):
                errors.append(f"risks[{idx}] must be an object")
                continue
            risk_missing = REQUIRED_RISK_FIELDS - set(risk)
            if risk_missing:
                errors.append(f"risks[{idx}] missing fields: {sorted(risk_missing)}")
            confidence = risk.get("confidence")
            if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                errors.append(f"risks[{idx}].confidence must be between 0 and 1")
            if not str(risk.get("evidence", "")).strip():
                errors.append(f"risks[{idx}].evidence is required")

    return PredictionCheck(compliant=not errors, errors=errors)


def expected_risk_types(label: dict[str, Any]) -> set[str]:
    """从标注数据中提取预期的风险类型集合。

    Args:
        label: 人工标注数据

    Returns:
        预期风险类型的字符串集合
    """
    return {
        str(risk.get("risk_type"))
        for risk in label.get("expected_risks", [])
        if isinstance(risk, dict) and risk.get("risk_type")
    }


def predicted_alerts(prediction: dict[str, Any]) -> list[dict[str, Any]]:
    """从预测结果中提取需要关注的告警风险。

    筛选条件：置信度 >= 0.75 且严重级别为 medium/high/critical。

    Args:
        prediction: 模型预测结果

    Returns:
        符合条件的风险项列表
    """
    alerts = []
    for risk in prediction.get("risks", []):
        if not isinstance(risk, dict):
            continue
        severity = str(risk.get("severity", "unknown"))
        confidence = risk.get("confidence", 0)
        if isinstance(confidence, (int, float)) and confidence >= MEDIUM_REVIEW_THRESHOLD and severity in ALERT_SEVERITIES:
            alerts.append(risk)
    return alerts


def high_confidence_alerts(prediction: dict[str, Any]) -> list[dict[str, Any]]:
    """从预测告警中进一步筛选高置信度告警（置信度 >= 0.85）。

    Args:
        prediction: 模型预测结果

    Returns:
        高置信度风险项列表
    """
    return [
        risk
        for risk in predicted_alerts(prediction)
        if isinstance(risk.get("confidence"), (int, float)) and risk["confidence"] >= HIGH_CONFIDENCE_THRESHOLD
    ]


def evaluate(labels: list[dict[str, Any]], predictions: list[dict[str, Any]]) -> dict[str, Any]:
    """对照人工标注评估模型预测结果，生成完整的评估指标。

    评估流程：
    1. 按 image_id 匹配标注和预测
    2. 检查每条预测的 schema 合规性
    3. 统计高置信度告警精确率、正常样本误报率、风险遗漏率等指标
    4. 按 risk_type 统计 TP/FP/FN
    5. 判断是否通过研究门控标准

    Args:
        labels: 人工标注数据列表
        predictions: 模型预测结果列表

    Returns:
        包含所有评估指标的字典
    """
    # 按 image_id 建立索引，方便快速查找
    label_by_id = {row["image_id"]: row for row in labels}
    prediction_by_id = {row.get("image_id"): row for row in predictions}
    all_ids = sorted(label_by_id)

    # 对所有预测进行 schema 合规性检查
    schema_checks = {row.get("image_id"): validate_prediction(row) for row in predictions}
    schema_compliant_count = sum(1 for check in schema_checks.values() if check.compliant)

    # 初始化各项统计计数器
    high_conf_alert_count = 0  # 高置信度告警总数
    true_high_conf_alert_count = 0  # 命中预期的高置信度告警数
    normal_count = 0  # 正常样本数（标注中无风险）
    normal_false_positive_count = 0  # 正常样本被误报为有风险的数量
    risky_count = 0  # 有风险的样本数
    missed_risky_count = 0  # 被遗漏的风险样本数
    review_count = 0  # 标记为需要审查的数量
    review_hit_count = 0  # 审查命中的数量（确实有风险）
    missing_prediction_ids: list[str] = []  # 缺少预测的 image_id 列表
    per_risk = defaultdict(lambda: Counter({"tp": 0, "fp": 0, "fn": 0}))  # 按风险类型统计 TP/FP/FN
    latency_values: list[float] = []  # 延迟数据
    cost_values: list[float] = []  # 成本数据
    failures: list[dict[str, Any]] = []  # 失败案例（遗漏或误报）

    for image_id in all_ids:
        label = label_by_id[image_id]
        expected = expected_risk_types(label)  # 获取预期的风险类型集合
        prediction = prediction_by_id.get(image_id)

        # 处理缺少预测结果的情况
        if not prediction:
            missing_prediction_ids.append(image_id)
            if expected:
                # 有风险但预测缺失 → 记为漏检
                risky_count += 1
                missed_risky_count += 1
                for risk_type in expected:
                    per_risk[risk_type]["fn"] += 1
            else:
                normal_count += 1
            continue

        # 提取延迟和成本数据
        model_info = prediction.get("model_info", {})
        if isinstance(model_info, dict):
            latency = model_info.get("latency_ms")
            cost = model_info.get("estimated_cost")
            if isinstance(latency, (int, float)):
                latency_values.append(float(latency))
            if isinstance(cost, (int, float)):
                cost_values.append(float(cost))

        # 获取高置信度告警和一般告警的风险类型集合
        high_alerts = high_confidence_alerts(prediction)
        alert_types = {str(risk.get("risk_type")) for risk in high_alerts if risk.get("risk_type")}
        review_alert_types = {str(risk.get("risk_type")) for risk in predicted_alerts(prediction) if risk.get("risk_type")}

        # 统计高置信度告警总数和命中数
        high_conf_alert_count += len(high_alerts)
        true_high_conf_alert_count += sum(1 for risk_type in alert_types if risk_type in expected)

        # 判断是风险样本还是正常样本，并统计遗漏或误报
        if expected:
            # 标注中存在预期风险
            risky_count += 1
            if not expected.intersection(alert_types):
                # 高置信度告警中未覆盖任何预期风险 → 漏检
                missed_risky_count += 1
                failures.append({"image_id": image_id, "type": "miss", "expected": sorted(expected), "predicted": sorted(alert_types)})
        else:
            # 标注中无预期风险（正常样本）
            normal_count += 1
            if alert_types:
                # 正常样本被错误标记为有风险 → 误报
                normal_false_positive_count += 1
                failures.append({"image_id": image_id, "type": "false_positive", "expected": [], "predicted": sorted(alert_types)})

        # 统计审查命中情况
        if prediction.get("needs_review") is True:
            review_count += 1
            if expected.intersection(review_alert_types) or expected.intersection(alert_types):
                # 审查标记确实对应了预期风险 → 命中
                review_hit_count += 1

        # 按风险类型统计 TP/FP/FN
        for risk_type in expected.intersection(alert_types):
            per_risk[risk_type]["tp"] += 1  # 预期且预测正确 → 真正例
        for risk_type in alert_types - expected:
            per_risk[risk_type]["fp"] += 1  # 预测了但不在预期中 → 假正例
        for risk_type in expected - alert_types:
            per_risk[risk_type]["fn"] += 1  # 预期但未预测到 → 假反例

    # 汇总所有评估指标
    metrics = {
        "label_count": len(labels),  # 标注总数
        "prediction_count": len(predictions),  # 预测总数
        "missing_prediction_count": len(missing_prediction_ids),  # 缺失预测数
        "missing_prediction_ids": missing_prediction_ids,  # 缺失预测的 ID 列表
        "schema_compliance_rate": safe_ratio(schema_compliant_count, len(predictions)),  # schema 合规率
        "high_confidence_alert_precision": safe_ratio(true_high_conf_alert_count, high_conf_alert_count),  # 高置信度告警精确率
        "normal_false_positive_rate": safe_ratio(normal_false_positive_count, normal_count),  # 正常样本误报率
        "risk_miss_rate": safe_ratio(missed_risky_count, risky_count),  # 风险遗漏率
        "review_hit_rate": safe_ratio(review_hit_count, review_count),  # 审查命中率
        "average_latency_ms": round(mean(latency_values), 2) if latency_values else None,  # 平均延迟（毫秒）
        "average_estimated_cost": round(mean(cost_values), 6) if cost_values else None,  # 平均成本
        "counts": {
            "schema_compliant": schema_compliant_count,
            "high_confidence_alerts": high_conf_alert_count,
            "true_high_confidence_alerts": true_high_conf_alert_count,
            "normal_samples": normal_count,
            "normal_false_positives": normal_false_positive_count,
            "risky_samples": risky_count,
            "missed_risky_samples": missed_risky_count,
            "review_items": review_count,
            "review_hits": review_hit_count,
        },
        "per_risk": {risk_type: dict(counter) for risk_type, counter in sorted(per_risk.items())},  # 按风险类型的 TP/FP/FN 明细
        "schema_errors": {  # schema 不合规的错误详情
            image_id: check.errors
            for image_id, check in schema_checks.items()
            if image_id and not check.compliant
        },
        "failure_cases": failures,  # 失败案例明细
        "pass_criteria": {  # 研究门控通过标准
            "high_confidence_alert_precision": ">= 0.85",
            "normal_false_positive_rate": "<= 0.10",
            "schema_compliance_rate": ">= 0.99"
        },
        "passes_research_gate": (  # 是否通过研究门控（精确率>=0.85、误报率<=0.10、合规率>=0.99）
            safe_ratio(true_high_conf_alert_count, high_conf_alert_count) >= 0.85
            and safe_ratio(normal_false_positive_count, normal_count) <= 0.10
            and safe_ratio(schema_compliant_count, len(predictions)) >= 0.99
        )
    }
    return metrics


def safe_ratio(numerator: int, denominator: int) -> float:
    """安全计算比率，分母为零时返回 0.0。

    Args:
        numerator: 分子
        denominator: 分母

    Returns:
        保留 4 位小数的比率值
    """
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 4)


def render_markdown(metrics: dict[str, Any]) -> str:
    """将评估指标渲染为 Markdown 格式报告。

    报告包含：总览统计、按风险类型的 TP/FP/FN 表格、失败案例、schema 错误等。

    Args:
        metrics: evaluate() 函数返回的指标字典

    Returns:
        Markdown 格式的评估报告字符串
    """
    lines = [
        "# Evaluation Report",
        "",
        f"- Label count: {metrics['label_count']}",
        f"- Prediction count: {metrics['prediction_count']}",
        f"- Schema compliance rate: {metrics['schema_compliance_rate']:.2%}",
        f"- High-confidence alert precision: {metrics['high_confidence_alert_precision']:.2%}",
        f"- Normal false positive rate: {metrics['normal_false_positive_rate']:.2%}",
        f"- Risk miss rate: {metrics['risk_miss_rate']:.2%}",
        f"- Review hit rate: {metrics['review_hit_rate']:.2%}",
        f"- Average latency ms: {metrics['average_latency_ms']}",
        f"- Average estimated cost: {metrics['average_estimated_cost']}",
        f"- Passes research gate: {metrics['passes_research_gate']}",
        "",
        "## Per-risk Counts",
        "",
        "| Risk Type | TP | FP | FN |",
        "| --- | ---: | ---: | ---: |",
    ]
    # 按风险类型填充 TP/FP/FN 表格
    for risk_type, counter in metrics["per_risk"].items():
        lines.append(f"| {risk_type} | {counter.get('tp', 0)} | {counter.get('fp', 0)} | {counter.get('fn', 0)} |")

    # 附加失败案例详情
    if metrics["failure_cases"]:
        lines.extend(["", "## Failure Cases", ""])
        for case in metrics["failure_cases"]:
            lines.append(f"- `{case['image_id']}`: {case['type']} expected={case['expected']} predicted={case['predicted']}")

    # 附加 schema 错误详情
    if metrics["schema_errors"]:
        lines.extend(["", "## Schema Errors", ""])
        for image_id, errors in metrics["schema_errors"].items():
            lines.append(f"- `{image_id}`: {'; '.join(errors)}")

    return "\n".join(lines) + "\n"


def main() -> None:
    """主函数：解析命令行参数，执行评估并输出结果。

    输出文件：
    - eval_metrics.json: 完整评估指标（JSON 格式）
    - eval_report.md: 可读的 Markdown 评估报告
    """
    parser = argparse.ArgumentParser(description="Evaluate office image risk analysis predictions.")
    parser.add_argument("--labels", type=Path, required=True, help="JSONL file with human labels.")
    parser.add_argument("--predictions", type=Path, required=True, help="JSONL file with model predictions.")
    parser.add_argument("--out-dir", type=Path, default=Path("outputs"), help="Directory for eval outputs.")
    args = parser.parse_args()

    # 读取标注和预测数据
    labels = read_jsonl(args.labels)
    predictions = read_jsonl(args.predictions)
    # 执行评估
    metrics = evaluate(labels, predictions)

    # 写入评估结果文件
    args.out_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = args.out_dir / "eval_metrics.json"
    report_path = args.out_dir / "eval_report.md"
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(render_markdown(metrics), encoding="utf-8")

    print(f"Wrote {metrics_path}")
    print(f"Wrote {report_path}")


if __name__ == "__main__":
    main()
