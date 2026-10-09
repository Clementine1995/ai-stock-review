# 临时脚本：从通达信 tdx_kline 工具结果文件中提取 Rows，组装审计信封。
# 仅本次使用，不提交仓库。
import json
from pathlib import Path

RESULTS_DIR = Path(r"C:/Users/335086/.workbuddy/projects/f-codex-project-ai-stock-review/58116c71-d1d4-4f1c-be92-50e3d8cc9f0e/tool-results")
OUT_PATH = Path(r"F:/codex-project/ai-stock-review/data/evidence/2026-08-07_tdx_market_raw.json")
TARGET = "20260807"

# 信封中的 setcode/period/tqFlag 是查询时传入的固定参数（CLI 依此校验），不从返回结果推导。
QUERY_CONFIG = {
    "000001": {"setcode": 1},
    "000002": {"setcode": 1},
    "399001": {"setcode": 0},
    "399106": {"setcode": 0},
}

files = sorted(RESULTS_DIR.glob("mcp-connector-proxy-tdx-connector_tdx_kline-*.txt"))
print("找到结果文件：", len(files))

by_code = {}
for f in files:
    text = f.read_text(encoding="utf-8", errors="replace")
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise SystemExit(f"未找到 JSON：{f.name}")
    payload = json.loads(text[start : end + 1])
    code = str(payload.get("Code"))
    rows = payload.get("Rows")
    if not isinstance(rows, list) or not rows:
        raise SystemExit(f"{code} Rows 为空或非数组")
    by_code[code] = rows
    target_rows = [r for r in rows if r.get("Data") == TARGET]
    print(f"代码 {code} Rows={len(rows)} 目标日行数={len(target_rows)}")
    if len(target_rows) == 1:
        r = target_rows[0]
        print(f"   目标日 {TARGET}: Close={r.get('Close')} Amount={r.get('Amount')}")

expected = set(QUERY_CONFIG)
missing = expected - set(by_code)
if missing:
    raise SystemExit(f"缺少代码：{missing}")

queries = []
for code in ("000001", "000002", "399001", "399106"):
    queries.append({
        "code": code,
        "setcode": QUERY_CONFIG[code]["setcode"],
        "period": 4,
        "tqFlag": 0,
        "Rows": by_code[code],
    })

envelope = {"queries": queries}
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
OUT_PATH.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
print("已写入审计信封：", OUT_PATH)
print("文件大小(字节)：", OUT_PATH.stat().st_size)
