import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const rootDir = process.cwd();
const outputDir = path.join(rootDir, "outputs");
const outputPath = path.join(outputDir, "office_risk_research_workbook.xlsx");

function parseCsvLine(line) {
  const cells = [];
  let current = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i += 1) {
    const char = line[i];
    const next = line[i + 1];
    if (char === '"' && inQuotes && next === '"') {
      current += '"';
      i += 1;
    } else if (char === '"') {
      inQuotes = !inQuotes;
    } else if (char === "," && !inQuotes) {
      cells.push(current);
      current = "";
    } else {
      current += char;
    }
  }
  cells.push(current);
  return cells;
}

async function readCsv(relativePath) {
  const text = await fs.readFile(path.join(rootDir, relativePath), "utf8");
  return text
    .trim()
    .split(/\r?\n/)
    .filter(Boolean)
    .map(parseCsvLine);
}

async function readJsonl(relativePath) {
  const text = await fs.readFile(path.join(rootDir, relativePath), "utf8");
  return text
    .trim()
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}

async function readJson(relativePath) {
  const text = await fs.readFile(path.join(rootDir, relativePath), "utf8");
  return JSON.parse(text);
}

function jsonCell(value) {
  if (value === undefined || value === null) return "";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return value;
  return JSON.stringify(value);
}

function columnName(indexOneBased) {
  let index = indexOneBased;
  let name = "";
  while (index > 0) {
    const remainder = (index - 1) % 26;
    name = String.fromCharCode(65 + remainder) + name;
    index = Math.floor((index - 1) / 26);
  }
  return name;
}

function addMatrixSheet(workbook, name, matrix, options = {}) {
  const sheet = workbook.worksheets.add(name);
  sheet.showGridLines = false;
  const rowCount = matrix.length;
  const colCount = Math.max(...matrix.map((row) => row.length));
  const padded = matrix.map((row) => {
    const clone = [...row];
    while (clone.length < colCount) clone.push("");
    return clone;
  });
  const range = sheet.getRangeByIndexes(0, 0, rowCount, colCount);
  range.values = padded;
  range.format.wrapText = true;
  range.format.borders = { preset: "all", style: "thin", color: "#D9DEE7" };
  range.format.font = { name: "Microsoft YaHei", size: 10, color: "#172033" };

  if (options.headerRows !== 0) {
    const headerRows = options.headerRows ?? 1;
    const header = sheet.getRangeByIndexes(0, 0, headerRows, colCount);
    header.format.fill = { color: "#EAF2F8" };
    header.format.font = { bold: true, color: "#17324D", name: "Microsoft YaHei", size: 10 };
  }

  if (options.titleRow) {
    sheet.mergeCells(`A1:${columnName(colCount)}1`);
    const title = sheet.getRangeByIndexes(0, 0, 1, colCount);
    title.format.fill = { color: "#EAF2F8" };
    title.format.font = { bold: true, color: "#17324D", name: "Microsoft YaHei", size: 14 };
    title.format.rowHeightPx = 32;
  }

  sheet.freezePanes.freezeRows(options.freezeRows ?? 1);
  range.format.autofitColumns();
  range.format.autofitRows();
  return sheet;
}

function labelsToMatrix(labels) {
  return [
    ["image_id", "scene_type", "expected_risks", "must_review"],
    ...labels.map((row) => [
      row.image_id,
      row.scene_type,
      jsonCell(row.expected_risks),
      row.must_review
    ])
  ];
}

function predictionsToMatrix(predictions) {
  return [
    ["image_id", "scene_type", "risks", "overall_severity", "needs_review", "unsupported_claims", "privacy_flags", "latency_ms", "estimated_cost"],
    ...predictions.map((row) => [
      row.image_id,
      row.scene_type,
      jsonCell(row.risks),
      row.overall_severity,
      row.needs_review,
      jsonCell(row.unsupported_claims),
      jsonCell(row.privacy_flags),
      row.model_info?.latency_ms ?? "",
      row.model_info?.estimated_cost ?? ""
    ])
  ];
}

function riskTaxonomyToMatrix(taxonomy) {
  return [
    ["code", "name_zh", "default_severity", "minimum_visible_evidence", "common_false_positives"],
    ...taxonomy.risk_types.map((risk) => [
      risk.code,
      risk.name_zh,
      risk.default_severity,
      risk.minimum_visible_evidence,
      risk.common_false_positives.join("；")
    ])
  ];
}

function metricsToMatrix(metrics) {
  return [
    ["Metric", "Value", "Gate"],
    ["Label count", metrics.label_count, ""],
    ["Prediction count", metrics.prediction_count, ""],
    ["Schema compliance rate", metrics.schema_compliance_rate, ">= 0.99"],
    ["High-confidence alert precision", metrics.high_confidence_alert_precision, ">= 0.85"],
    ["Normal false positive rate", metrics.normal_false_positive_rate, "<= 0.10"],
    ["Risk miss rate", metrics.risk_miss_rate, "Lower is better"],
    ["Review hit rate", metrics.review_hit_rate, "Higher is better"],
    ["Average latency ms", metrics.average_latency_ms ?? "", "Record"],
    ["Average estimated cost", metrics.average_estimated_cost ?? "", "Record"],
    ["Passes research gate", metrics.passes_research_gate ? "TRUE" : "FALSE", "TRUE"]
  ];
}

function perRiskMatrix(metrics) {
  const rows = [["Risk Type", "TP", "FP", "FN"]];
  for (const [riskType, counter] of Object.entries(metrics.per_risk)) {
    rows.push([riskType, counter.tp ?? 0, counter.fp ?? 0, counter.fn ?? 0]);
  }
  return rows;
}

const manifest = await readCsv("data/evaluation_manifest.csv");
const experimentRecord = await readCsv("data/experiment_record.csv");
const labels = await readJsonl("data/labels_sample.jsonl");
const predictions = await readJsonl("data/predictions_sample.jsonl");
const metrics = await readJson("outputs/eval_metrics.json");
const taxonomy = await readJson("schemas/risk_taxonomy.json");

const workbook = Workbook.create();
const renderedSheets = [];

addMatrixSheet(workbook, "Summary", [
  ["室内办公图片风险分析预研工作簿", "", ""],
  ["Generated", `UTC ${new Date().toISOString()}`, ""],
  ["Purpose", "用于记录样本、人工标注、模型输出、评测指标和风险标签。", ""],
  ["Research Gate", metrics.passes_research_gate ? "PASS" : "CHECK REQUIRED", "样例数据仅用于脚本验证，真实结论以正式评测集为准。"],
  [],
  ["Core Metric", "Current Sample", "Target"],
  ["Schema compliance rate", metrics.schema_compliance_rate, ">= 0.99"],
  ["High-confidence alert precision", metrics.high_confidence_alert_precision, ">= 0.85"],
  ["Normal false positive rate", metrics.normal_false_positive_rate, "<= 0.10"],
  ["Risk miss rate", metrics.risk_miss_rate, "Lower is better"],
  ["Review hit rate", metrics.review_hit_rate, "Higher is better"]
], { titleRow: true, headerRows: 1, freezeRows: 1 });

addMatrixSheet(workbook, "Metrics", metricsToMatrix(metrics));
addMatrixSheet(workbook, "Per Risk", perRiskMatrix(metrics));
addMatrixSheet(workbook, "Evaluation Manifest", manifest);
addMatrixSheet(workbook, "Labels Sample", labelsToMatrix(labels));
addMatrixSheet(workbook, "Predictions Sample", predictionsToMatrix(predictions));
addMatrixSheet(workbook, "Experiment Record", experimentRecord);
addMatrixSheet(workbook, "Risk Taxonomy", riskTaxonomyToMatrix(taxonomy));

await fs.mkdir(outputDir, { recursive: true });
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(outputPath);

const previewDir = path.join(outputDir, "workbook_previews");
await fs.mkdir(previewDir, { recursive: true });
for (const sheetName of [
  "Summary",
  "Metrics",
  "Per Risk",
  "Evaluation Manifest",
  "Labels Sample",
  "Predictions Sample",
  "Experiment Record",
  "Risk Taxonomy"
]) {
  const preview = await workbook.render({ sheetName, autoCrop: "all", scale: 1, format: "png" });
  const bytes = new Uint8Array(await preview.arrayBuffer());
  const previewPath = path.join(previewDir, `${sheetName.replaceAll(" ", "_")}.png`);
  await fs.writeFile(previewPath, bytes);
  renderedSheets.push(sheetName);
}

const inspect = await workbook.inspect({
  kind: "table",
  range: "Summary!A1:C11",
  include: "values",
  tableMaxRows: 12,
  tableMaxCols: 4
});
const formulaErrors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan"
});
console.log(inspect.ndjson);
console.log(formulaErrors.ndjson);
console.log(`Rendered sheets: ${renderedSheets.join(", ")}`);
console.log(`Wrote ${outputPath}`);
process.exit(0);
