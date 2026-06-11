import fs from "node:fs/promises";
import path from "node:path";
import { Buffer } from "node:buffer";

const rootDir = process.cwd();
const outputDir = path.join(rootDir, "outputs");
const outputPath = path.join(outputDir, "office_risk_research_workbook.xlsx");

function stripBom(text) {
  return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
}

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
  const text = stripBom(await fs.readFile(path.join(rootDir, relativePath), "utf8"));
  return text
    .trim()
    .split(/\r?\n/)
    .filter(Boolean)
    .map(parseCsvLine);
}

async function readJsonl(relativePath) {
  const text = stripBom(await fs.readFile(path.join(rootDir, relativePath), "utf8"));
  return text
    .trim()
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}

async function readJson(relativePath) {
  const text = stripBom(await fs.readFile(path.join(rootDir, relativePath), "utf8"));
  return JSON.parse(text);
}

function jsonCell(value) {
  if (value === undefined || value === null) return "";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return value;
  return JSON.stringify(value);
}

function labelsToMatrix(labels) {
  return [
    ["image_id", "scene_type", "expected_risks", "must_review"],
    ...labels.map((row) => [row.image_id, row.scene_type, jsonCell(row.expected_risks), row.must_review]),
  ];
}

function predictionsToMatrix(predictions) {
  return [
    [
      "image_id",
      "scene_type",
      "risks",
      "overall_severity",
      "needs_review",
      "unsupported_claims",
      "privacy_flags",
      "latency_ms",
      "estimated_cost",
    ],
    ...predictions.map((row) => [
      row.image_id,
      row.scene_type,
      jsonCell(row.risks),
      row.overall_severity,
      row.needs_review,
      jsonCell(row.unsupported_claims),
      jsonCell(row.privacy_flags),
      row.model_info?.latency_ms ?? "",
      row.model_info?.estimated_cost ?? "",
    ]),
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
      risk.common_false_positives.join("；"),
    ]),
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
    ["Passes research gate", metrics.passes_research_gate ? "TRUE" : "FALSE", "TRUE"],
  ];
}

function perRiskMatrix(metrics) {
  const rows = [["Risk Type", "TP", "FP", "FN"]];
  for (const [riskType, counter] of Object.entries(metrics.per_risk)) {
    rows.push([riskType, counter.tp ?? 0, counter.fp ?? 0, counter.fn ?? 0]);
  }
  return rows;
}

function xmlEscape(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
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

function sheetXml(matrix) {
  const rows = matrix.map((row, rowIndex) => {
    const cells = row.map((value, colIndex) => {
      const ref = `${columnName(colIndex + 1)}${rowIndex + 1}`;
      return `<c r="${ref}" t="inlineStr"><is><t>${xmlEscape(value)}</t></is></c>`;
    });
    return `<row r="${rowIndex + 1}">${cells.join("")}</row>`;
  });
  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
  <sheetData>${rows.join("")}</sheetData>
</worksheet>`;
}

function workbookXml(sheets) {
  const entries = sheets
    .map((sheet, index) => `<sheet name="${xmlEscape(sheet.name)}" sheetId="${index + 1}" r:id="rId${index + 1}"/>`)
    .join("");
  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>${entries}</sheets>
</workbook>`;
}

function workbookRelsXml(sheets) {
  const entries = sheets
    .map(
      (_sheet, index) =>
        `<Relationship Id="rId${index + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet${index + 1}.xml"/>`,
    )
    .join("");
  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">${entries}</Relationships>`;
}

function contentTypesXml(sheets) {
  const sheetEntries = sheets
    .map(
      (_sheet, index) =>
        `<Override PartName="/xl/worksheets/sheet${index + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>`,
    )
    .join("");
  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
  ${sheetEntries}
</Types>`;
}

function rootRelsXml() {
  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>`;
}

function corePropsXml() {
  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>Office Risk Research Workbook</dc:title>
  <dc:creator>vlm-demo</dc:creator>
  <dcterms:created xsi:type="dcterms:W3CDTF">${new Date().toISOString()}</dcterms:created>
</cp:coreProperties>`;
}

function appPropsXml(sheets) {
  const names = sheets.map((sheet) => `<vt:lpstr>${xmlEscape(sheet.name)}</vt:lpstr>`).join("");
  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>vlm-demo</Application>
  <TitlesOfParts><vt:vector size="${sheets.length}" baseType="lpstr">${names}</vt:vector></TitlesOfParts>
</Properties>`;
}

function makeCrc32Table() {
  const table = new Uint32Array(256);
  for (let i = 0; i < 256; i += 1) {
    let value = i;
    for (let bit = 0; bit < 8; bit += 1) {
      value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
    }
    table[i] = value >>> 0;
  }
  return table;
}

const crc32Table = makeCrc32Table();

function crc32(buffer) {
  let crc = 0xffffffff;
  for (const byte of buffer) {
    crc = crc32Table[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function dosDateTime(date = new Date()) {
  const year = Math.max(1980, date.getFullYear());
  const dosTime = (date.getHours() << 11) | (date.getMinutes() << 5) | Math.floor(date.getSeconds() / 2);
  const dosDate = ((year - 1980) << 9) | ((date.getMonth() + 1) << 5) | date.getDate();
  return { dosTime, dosDate };
}

function writeZip(entries) {
  const localParts = [];
  const centralParts = [];
  let offset = 0;
  const { dosTime, dosDate } = dosDateTime();

  for (const entry of entries) {
    const name = Buffer.from(entry.name, "utf8");
    const data = Buffer.isBuffer(entry.data) ? entry.data : Buffer.from(entry.data, "utf8");
    const checksum = crc32(data);

    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0x0800, 6);
    local.writeUInt16LE(0, 8);
    local.writeUInt16LE(dosTime, 10);
    local.writeUInt16LE(dosDate, 12);
    local.writeUInt32LE(checksum, 14);
    local.writeUInt32LE(data.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(name.length, 26);
    local.writeUInt16LE(0, 28);
    localParts.push(local, name, data);

    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(20, 4);
    central.writeUInt16LE(20, 6);
    central.writeUInt16LE(0x0800, 8);
    central.writeUInt16LE(0, 10);
    central.writeUInt16LE(dosTime, 12);
    central.writeUInt16LE(dosDate, 14);
    central.writeUInt32LE(checksum, 16);
    central.writeUInt32LE(data.length, 20);
    central.writeUInt32LE(data.length, 24);
    central.writeUInt16LE(name.length, 28);
    central.writeUInt16LE(0, 30);
    central.writeUInt16LE(0, 32);
    central.writeUInt16LE(0, 34);
    central.writeUInt16LE(0, 36);
    central.writeUInt32LE(0, 38);
    central.writeUInt32LE(offset, 42);
    centralParts.push(central, name);

    offset += local.length + name.length + data.length;
  }

  const centralDirectory = Buffer.concat(centralParts);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(0, 4);
  end.writeUInt16LE(0, 6);
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(centralDirectory.length, 12);
  end.writeUInt32LE(offset, 16);
  end.writeUInt16LE(0, 20);

  return Buffer.concat([...localParts, centralDirectory, end]);
}

function buildXlsx(sheets) {
  const entries = [
    { name: "[Content_Types].xml", data: contentTypesXml(sheets) },
    { name: "_rels/.rels", data: rootRelsXml() },
    { name: "docProps/core.xml", data: corePropsXml() },
    { name: "docProps/app.xml", data: appPropsXml(sheets) },
    { name: "xl/workbook.xml", data: workbookXml(sheets) },
    { name: "xl/_rels/workbook.xml.rels", data: workbookRelsXml(sheets) },
    ...sheets.map((sheet, index) => ({ name: `xl/worksheets/sheet${index + 1}.xml`, data: sheetXml(sheet.matrix) })),
  ];
  return writeZip(entries);
}

function toTsv(matrix) {
  return matrix.map((row) => row.map((cell) => String(cell ?? "").replaceAll("\t", " ").replaceAll("\n", " ")).join("\t")).join("\n") + "\n";
}

const manifest = await readCsv("data/evaluation_manifest.csv");
const experimentRecord = await readCsv("data/experiment_record.csv");
const labels = await readJsonl("data/labels_sample.jsonl");
const predictions = await readJsonl("data/predictions_sample.jsonl");
const metrics = await readJson("outputs/eval_metrics.json");
const taxonomy = await readJson("schemas/risk_taxonomy.json");

const sheets = [
  {
    name: "Summary",
    matrix: [
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
      ["Review hit rate", metrics.review_hit_rate, "Higher is better"],
    ],
  },
  { name: "Metrics", matrix: metricsToMatrix(metrics) },
  { name: "Per Risk", matrix: perRiskMatrix(metrics) },
  { name: "Evaluation Manifest", matrix: manifest },
  { name: "Labels Sample", matrix: labelsToMatrix(labels) },
  { name: "Predictions Sample", matrix: predictionsToMatrix(predictions) },
  { name: "Experiment Record", matrix: experimentRecord },
  { name: "Risk Taxonomy", matrix: riskTaxonomyToMatrix(taxonomy) },
];

await fs.mkdir(outputDir, { recursive: true });
await fs.writeFile(outputPath, buildXlsx(sheets));

const previewDir = path.join(outputDir, "workbook_previews");
await fs.mkdir(previewDir, { recursive: true });
for (const sheet of sheets) {
  const previewPath = path.join(previewDir, `${sheet.name.replaceAll(" ", "_")}.tsv`);
  await fs.writeFile(previewPath, toTsv(sheet.matrix), "utf8");
}

const formulaIssueTerms = ["#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#N/A"];
const formulaIssues = sheets.flatMap((sheet) =>
  sheet.matrix.flatMap((row, rowIndex) =>
    row
      .map((cell, colIndex) => ({ cell: String(cell ?? ""), rowIndex, colIndex }))
      .filter((item) => formulaIssueTerms.some((term) => item.cell.includes(term)))
      .map((item) => ({ sheet: sheet.name, row: item.rowIndex + 1, col: item.colIndex + 1, value: item.cell })),
  ),
);

console.log(JSON.stringify({ sheet: "Summary", rows: sheets[0].matrix.slice(0, 11) }, null, 2));
console.log(JSON.stringify({ formula_issues: formulaIssues }, null, 2));
console.log(`Rendered previews: ${sheets.map((sheet) => `${sheet.name}.tsv`).join(", ")}`);
console.log(`Wrote ${outputPath}`);
