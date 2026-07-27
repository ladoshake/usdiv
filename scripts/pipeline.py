#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股 股息率排名 —— 每日自动更新流水线（单文件、可独立运行）
流程：取代码宇宙 -> 批量 quote(真实市值/股息率TTM) -> 批量分红历史 -> 分市值档 Top30 -> 写 HTML
数据来源：腾讯自选股（westock-data skill 的行情/分红接口）
"""
import subprocess, json, re, os, sys, time, datetime
from concurrent.futures import ThreadPoolExecutor

# ---------------- 环境常量（绝对路径，避免依赖环境变量） ----------------
NODE = "/Users/green/.workbuddy/binaries/node/versions/22.22.2/bin/node"
DATA_JS = "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/resources/builtin-skills/westock-data/scripts/index.js"
TOOL_JS = "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/resources/builtin-skills/westock-tool/scripts/index.js"

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
os.makedirs(DATA_DIR, exist_ok=True)
HTML_PATH = os.path.abspath(os.path.join(BASE, "..", "index.html"))

# ---------------- 动态日期 ----------------
TODAY = datetime.date.today()
TTM_START = TODAY - datetime.timedelta(days=365)
GEN = TODAY.isoformat()
TTM_START_STR = TTM_START.isoformat()
LATEST_YEAR = TODAY.year - 1   # 最近一个完整自然年（用于美股 LFY / 港股历史列）

FX = 7.8  # HKD per USD（联系汇率，仅说明用）
TOP = 30

def log(*a):
    print("[pipeline]", *a, flush=True)

# =====================================================================
# 1) 代码宇宙
# =====================================================================
def run_filter(market, limit):
    out = subprocess.run(
        [NODE, TOOL_JS, "filter", "intersect([TotalMV > 0])",
         "--market", market, "--orderby", "TotalMV", "--desc", "--limit", str(limit)],
        capture_output=True, text=True, timeout=180)
    return out.stdout

def get_us_codes():
    txt = run_filter("us", 800)
    codes = set()
    for l in txt.splitlines():
        m = re.search(r'\|\s*(us[A-Za-z0-9]+)\s*\|', l)
        if m:
            codes.add(m.group(1))
    codes = sorted(codes)
    open(os.path.join(DATA_DIR, "us_codes.txt"), "w").write("\n".join(codes))
    log(f"US universe codes: {len(codes)}")
    return codes

# =====================================================================
# 2) 批量行情 quote
# =====================================================================
def run_quote(codes):
    out = subprocess.run([NODE, DATA_JS, "quote", ",".join(codes)],
                         capture_output=True, text=True, timeout=120)
    return out.stdout

def parse_quotes_generic(text, market):
    """按列名解析 quote 输出。美股 mv 单位亿美元，港股 mv 单位亿港元。"""
    lines = text.splitlines()
    hidx = None
    for i, l in enumerate(lines):
        if "code" in l and "name" in l and "total_market_cap" in l and "|" in l:
            hidx = i
            break
    if hidx is None:
        return {}
    header = [c.strip() for c in lines[hidx].strip().strip("|").split("|")]
    if header and header[0] == "":
        header = header[1:]
    idx = {h: j for j, h in enumerate(header)}
    prefix = "us"
    res = {}
    p_idx, d_idx, m_idx = idx["price"], idx["dividend_ratio_ttm"], idx["total_market_cap"]
    for l in lines[hidx + 1:]:
        s = l.strip().strip("|").strip()
        if not s or not s.startswith(prefix):
            continue
        cols = [c.strip() for c in s.split("|")]
        if len(cols) <= m_idx:
            continue
        code = cols[idx["code"]]
        if not code.startswith(prefix):
            continue
        def num(x):
            try:
                return float(x) if x not in ("", "-") else None
            except:
                return None
        res[code] = {
            "code": code, "name": cols[idx["name"]],
            "price": num(cols[p_idx]), "ttm_yield": num(cols[d_idx]), "mv": num(cols[m_idx]),
        }
    return res

def fetch_quotes(codes, market):
    out = {}
    for i in range(0, len(codes), 50):
        batch = codes[i:i + 50]
        try:
            txt = run_quote(batch)
            parsed = parse_quotes_generic(txt, market)
            out.update(parsed)
        except Exception as e:
            log(f"  quote batch {i} error: {e}")
        if i % 200 == 0:
            log(f"  quote {min(i+50,len(codes))}/{len(codes)} parsed={len(out)}")
    log(f"{market} quotes parsed: {len(out)}")
    return out

# =====================================================================
# 3) 分红历史
# =====================================================================
def parse_date(s):
    s = (s or "").strip()
    if len(s) < 8:
        return None
    try:
        return datetime.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except:
        return None

def run_div(code, retries=3):
    for _ in range(retries):
        try:
            out = subprocess.run([NODE, DATA_JS, "dividend", "list", code, "--years", "5"],
                                 capture_output=True, text=True, timeout=60).stdout
            if "暂无分红数据" in out or "暂无" in out:
                return None
            if "reportEndDate" in out or "exDivDate" in out:
                return out
        except Exception:
            time.sleep(0.3)
    return None

def parse_us_div(txt):
    lines = txt.splitlines()
    hdr = [i for i, l in enumerate(lines) if l.startswith("|") and "exDivDate" in l]
    if not hdr:
        return []
    rows = []
    for l in lines[hdr[0] + 2:]:
        if not l.startswith("|"):
            continue
        c = [x.strip() for x in l.strip().strip("|").split("|")]
        if len(c) < 6:
            continue
        if c[3].upper() != "USD":
            continue
        d = parse_date(c[0])
        try:
            v = float(c[4])
        except:
            continue
        if d:
            rows.append((d, v))
    return rows

def fetch_us_div(bucketed):
    def work(d):
        code = d["code"]
        txt = run_div(code)
        if not txt:
            return code, {"has_div": False}
        rows = parse_us_div(txt)
        if not rows:
            return code, {"has_div": False}
        ttm = [v for d_, v in rows if d_ >= TTM_START]
        def yr(y):
            return [v for d_, v in rows if d_.year == y]
        yl, yp, yp2 = yr(LATEST_YEAR), yr(LATEST_YEAR - 1), yr(LATEST_YEAR - 2)
        price = d.get("price")
        def yld(s):
            return round(sum(s) / price * 100, 3) if (price and s) else None
        return code, {
            "has_div": True,
            "ttm_dps": round(sum(ttm), 4) if ttm else None,
            "ttm_count": len(ttm),
            "lfy_dps": round(sum(yl), 4) if yl else None,
            "lfy_count": len(yl),
            "lfy_yield": yld(yl),
            "prev_yield": yld(yp),
            "prev2_yield": yld(yp2),
        }
    result = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for i, (code, e) in enumerate(ex.map(work, bucketed), 1):
            result[code] = e
            if i % 80 == 0:
                log(f"  US div progress {i}/{len(bucketed)}")
    qmap = {d["code"]: d for d in bucketed}
    out = []
    for d in bucketed:
        e = result.get(d["code"], {})
        out.append({
            "code": d["code"], "name": d["name"], "price": d["price"],
            "mv": d["mv"], "ttm_yield": d["ttm_yield"],
            "ttm_dps": e.get("ttm_dps"), "ttm_count": e.get("ttm_count"),
            "lfy_dps": e.get("lfy_dps"), "lfy_count": e.get("lfy_count"),
            "lfy_yield": e.get("lfy_yield"), "prev_yield": e.get("prev_yield"),
            "prev2_yield": e.get("prev2_yield"), "has_div": e.get("has_div", False),
        })
    json.dump(out, open(os.path.join(DATA_DIR, "us_enriched.json"), "w"), ensure_ascii=False, indent=1)
    log(f"US enriched: {len(out)} (with div: {sum(1 for x in out if x['has_div'])})")
    return out

# =====================================================================
# 4) 构建 HTML
# =====================================================================
def sanitize(d):
    ttm = d.get("ttm_yield")
    for k in ("lfy_yield", "prev_yield", "prev2_yield"):
        v = d.get(k)
        if v is None or ttm is None:
            continue
        if v > ttm * 2.5 or v > 15:
            d[k] = None
    return d

def strip_mkt_prefix(code):
    """去掉行情接口返回的市场前缀 us / hk，仅用于展示。"""
    if code and code[:2].lower() in ("us", "hk"):
        return code[2:]
    return code

def build_html(us):
    def classify_us(d):
        mv = d["mv"]
        if mv > 1000: return "gt1000"
        if 500 < mv <= 1000: return "mid500"
        return None

    US_CAPS = [{"key": "gt1000", "label": "市值 > 1000亿美元"},
               {"key": "mid500", "label": "500亿 < 市值 ≤ 1000亿美元"}]

    def build_market(records, classify, mv_key, cap_specs):
        groups = {"gt1000": [], "mid500": []}
        for d in records:
            k = classify(d)
            if k: groups[k].append(d)
        tiers = []
        for cs in cap_specs:
            key, label = cs["key"], cs["label"]
            sub = groups[key]
            pt = sorted([x for x in sub if (x.get("ttm_yield") or 0) > 0],
                        key=lambda x: -x["ttm_yield"])[:TOP]
            pl = sorted([x for x in sub if (x.get("lfy_yield") is not None) or (x.get("ttm_yield") or 0) > 0],
                        key=lambda x: (x.get("lfy_yield") is None, -(x.get("lfy_yield") or 0)))[:TOP]
            def make_rows(rows, yk, dk, ck):
                out = []
                for i, d in enumerate(rows, 1):
                    out.append({
                        "rank": i, "code": strip_mkt_prefix(d["code"]), "name": d["name"], "price": d.get("price"),
                        "total_mv_yi": d.get(mv_key), "dps": d.get(dk),
                        "div_count": d.get(ck), "yield": d.get(yk),
                        "prev_yield": d.get("prev_yield"), "prev2_yield": d.get("prev2_yield"),
                    })
                return out
            tiers.append({
                "key": key, "label": label, "count": len(sub),
                "ttm": make_rows(pt, "ttm_yield", "ttm_dps", "ttm_count"),
                "lfy": make_rows(pl, "lfy_yield", "lfy_dps", "lfy_count"),
            })
        return tiers

    us_tiers = build_market(us, classify_us, "mv", US_CAPS)
    markets = [
        {"key": "us", "name": "美股", "cur": "美元", "price_unit": "美元", "dps_unit": "美元", "caps": US_CAPS, "tiers": us_tiers},
    ]
    EMBEDDED = {"generated_at": GEN, "ttm_start": TTM_START_STR, "markets": markets}
    json_str = json.dumps(EMBEDDED, ensure_ascii=False)

    HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>美股 股息率排名</title>
<style>
  :root{
    --bg:#f5f6f8; --card:#ffffff; --ink:#1f2430; --sub:#6b7280;
    --line:#e5e7eb; --brand:#c0392b; --brand2:#2563eb; --gold:#b8860b;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
       background:var(--bg);color:var(--ink);line-height:1.5}
  .wrap{max-width:1100px;margin:0 auto;padding:16px 16px 56px}
  header h1{font-size:21px;margin:0 0 4px}
  header p{margin:1px 0;color:var(--sub);font-size:13px}
  .subhead{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin:8px 0 2px}
  .desc{margin:0;color:var(--sub);font-size:clamp(12px,1.1vw,14px);line-height:1.5}
  .metabox{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
  .meta{margin:0;text-align:right;color:var(--sub);font-size:clamp(12px,1.1vw,14px);line-height:1.4;white-space:nowrap}
  .box{display:flex;align-items:baseline;gap:6px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px 12px;white-space:nowrap}
  .num{font-size:13px;font-weight:700;color:var(--brand2)}
  .lab{font-size:13px;color:var(--sub)}
  .tabs{display:flex;gap:8px;margin:14px 0 8px}
  .tab{padding:7px 20px;display:flex;flex-direction:column;align-items:center;gap:1px;border:1px solid var(--line);border-radius:999px;background:var(--card);cursor:pointer;font-size:14px;font-weight:600;color:var(--sub);line-height:1.2}
  .tab-main{white-space:nowrap}
  .tab-sub{font-size:11px;font-weight:400;opacity:.78}
  .tab.active .tab-sub{opacity:.92}
  .tab.active{background:var(--brand2);color:#fff;border-color:var(--brand2)}
  .capsel{display:flex;gap:8px;margin:14px 0 2px;flex-wrap:wrap}
  .cap{padding:6px 16px;border:1px solid var(--line);border-radius:999px;background:var(--card);cursor:pointer;font-size:13px;font-weight:600;color:var(--sub);line-height:1.2;white-space:nowrap}
  .cap.active{background:var(--brand);color:#fff;border-color:var(--brand)}
  .panel{display:none}
  .panel.active{display:block}
  table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:13.5px}
  .table-wrap{width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch}
  th,td{padding:9px 10px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
  th{background:#f0f1f4;color:#374151;font-weight:600;cursor:pointer;user-select:none;position:relative}
  th.sort-asc::after{content:" ▲";font-size:10px;color:var(--brand)}
  th.sort-desc::after{content:" ▼";font-size:10px;color:var(--brand)}
  tbody td{background:#fff}
  tbody tr:nth-child(even) td{background:#f7f8fa}
  tbody tr:hover td{background:#e8f0fe}
  .rk{display:inline-block;min-width:22px;text-align:center;font-weight:700;color:var(--brand2)}
  .code{color:var(--sub);font-size:12px}
  .yld{font-weight:700;color:var(--brand)}
  .panel.alt .yld{color:var(--brand2)}
  .yld2{font-weight:600;color:#0f766e}
  .sub{font-size:10px;color:var(--sub);font-weight:400;line-height:1.15}
  .note{width:100%;margin-top:28px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:clamp(12px,1.6vw,18px) clamp(14px,1.8vw,20px);font-size:clamp(12px,1.1vw,14px);color:#374151}
  .note h3{margin:0 0 8px;font-size:clamp(14px,1.4vw,16px)}
  .note ul{margin:6px 0;padding-left:20px}
  .note li{margin:3px 0}
  .tag{display:inline-block;background:#eef2ff;color:#4338ca;border-radius:6px;padding:1px 7px;font-size:11px;margin-left:6px}
  @media (max-width:560px){
    .subhead{flex-direction:column;align-items:flex-start;gap:8px}
    .metabox{flex-direction:column;align-items:flex-start;gap:8px}
    .meta{text-align:left;white-space:normal}
    .tabs{gap:6px}
    .tab{padding:8px 12px;font-size:clamp(12px,3.4vw,14px)}
    .wrap{padding:12px 12px 48px}
    /* 手机端：横向滑动时冻结「排名」与「名称」两列 */
    .table-wrap{position:relative}
    table{overflow:visible;border-radius:0}
    table th:nth-child(1), table td:nth-child(1),
    table th:nth-child(2), table td:nth-child(2){position:sticky;z-index:2;background-clip:padding-box}
    table th:nth-child(1), table td:nth-child(1){left:0;width:46px;min-width:46px;max-width:46px}
    table th:nth-child(2), table td:nth-child(2){left:46px;width:108px;min-width:108px;max-width:108px;white-space:normal;box-shadow:2px 0 0 rgba(15,23,42,.10)}
    table th:nth-child(1), table th:nth-child(2){z-index:3;background:#f0f1f4}
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>美股 股息率排名</h1>
    <div class="subhead">
      <p class="desc" id="desc">美股 · 市值 &gt; 1000亿美元 · 股息率排名</p>
      <div class="metabox">
        <div class="meta">数据日期：<span id="gen"></span> ｜ TTM计算起点：<span id="ttmstart"></span></div>
        <div class="box"><div class="lab" id="cntlab">公司数</div><div class="num" id="cnt"></div></div>
      </div>
    </div>
  </header>

  <div class="capsel" id="capsel">
    <div class="cap active" data-cap="gt1000">市值 &gt; 1000亿美元</div>
    <div class="cap" data-cap="mid500">500亿 &lt; 市值 ≤ 1000亿美元</div>
  </div>

  <div class="tabs">
    <div class="tab active" data-tab="ttm"><span class="tab-main">TTM 股息率</span><span class="tab-sub">最近12个月</span></div>
    <div class="tab alt" data-tab="lfy"><span class="tab-main">LFY 股息率</span><span class="tab-sub">最近财年</span></div>
  </div>

  <div class="panel active" id="panel-ttm">
    <div class="table-wrap">
    <table id="table-ttm">
      <thead><tr>
        <th data-k="rank">排名</th><th data-k="name">名称</th><th data-k="code">代码</th>
        <th data-k="price" id="th-price">现价(美元)</th><th data-k="total_mv_yi" id="th-mv">总市值(亿)</th>
        <th data-k="dps" id="th-dps">每股分红(美元)</th><th data-k="div_count">分红次数</th><th data-k="yield">TTM股息率</th>
      </tr></thead>
      <tbody></tbody>
    </table>
    </div>
  </div>

  <div class="panel" id="panel-lfy">
    <div class="table-wrap">
    <table id="table-lfy">
      <thead><tr>
        <th data-k="rank">排名</th><th data-k="name">名称</th><th data-k="code">代码</th>
        <th data-k="price" id="th-price2">现价(美元)</th><th data-k="total_mv_yi" id="th-mv2">总市值(亿)</th>
        <th data-k="dps" id="th-dps2">每股分红(美元)</th><th data-k="div_count">分红次数</th><th data-k="yield">LFY股息率</th><th data-k="prev_yield">2024年股息率</th><th data-k="prev2_yield">2023年股息率</th>
      </tr></thead>
      <tbody></tbody>
    </table>
    </div>
  </div>

  <div class="note">
    <h3>计算口径说明</h3>
    <ul>
      <li><b>市值筛选</b>：以美元计，提供两档——市值 &gt; 1000 亿美元、500 亿美元 &lt; 市值 ≤ 1000 亿美元，取各档内全部派息股按股息率排名（每档至多 Top 30）。</li>
      <li><b>股息率(TTM)</b>：行情接口「股息率TTM」= 近12个月每股股息 ÷ 现价 × 100%。</li>
      <li><b>每股分红 / 分红次数</b>：取自个股分红历史，统计除息日落在近 12 个月内的现金分红之和与次数；部分股票接口暂无分红历史，显示为「—」。</li>
      <li><b>LFY 股息率</b>：最近完整年度每股分红之和 ÷ 现价；历史列对应最近一年、前一年，口径同 LFY。</li>
      <li><b>货币单位</b>：美股为美元（现价、每股分红、总市值均按美元）。MLP（有限合伙）分红含返还资本，解读时请注意。</li>
      <li><b>数据来源</b>：腾讯自选股（美股）实时行情与历史分红。榜单为计算快照，非投资建议。</li>
    </ul>
  </div>

</div>

<script>
const EMBEDDED = __JSON__;
let DATA = EMBEDDED;
let currentMkt = DATA.markets[0].key;
let currentCap = "gt1000";
let currentTab = "ttm";

const fmt = (n,d=2)=> (n==null||isNaN(n))?"-":Number(n).toLocaleString("zh-CN",{minimumFractionDigits:d,maximumFractionDigits:d});

const sortState = {};
const sortKey = (mkt,cap,kind)=> mkt+"_"+cap+"_"+kind;

function getMarket(k){ return DATA.markets.find(m=>m.key===k); }
function getTier(mkt,k){ const m=getMarket(mkt); return m.tiers.find(t=>t.key===k); }
function rowsFor(mkt, cap, kind){ const t=getTier(mkt,cap); return (kind==="ttm"?t.ttm:t.lfy).slice(); }

function renderTable(tableId, rows, withFy){
  const tbody = document.querySelector(`#${tableId} tbody`);
  tbody.innerHTML = "";
  rows.forEach(r=>{
    const tr = document.createElement("tr");
    const lfyCell = `<td class="yld">${fmt(r.yield,2)}%</td>`;
    const prev = withFy ? `<td class="yld2">${fmt(r.prev_yield,2)}%</td><td class="yld2">${fmt(r.prev2_yield,2)}%</td>` : "";
    tr.innerHTML = `<td><span class="rk">${r.rank}</span></td>
      <td><b>${r.name}</b></td>
      <td class="code">${r.code}</td>
      <td>${fmt(r.price)}</td>
      <td>${fmt(r.total_mv_yi,0)}</td>
      <td>${fmt(r.dps,3)}</td>
      <td class="cnt">${fmt(r.div_count,0)}</td>
      ${lfyCell}${prev}`;
    tbody.appendChild(tr);
  });
}

function applyTable(tableId, withFy){
  const kind = withFy ? "lfy" : "ttm";
  const skey = sortKey(currentMkt, currentCap, kind);
  const sk = sortState[skey] || (sortState[skey] = {key:"yield", dir:-1});
  const rows = rowsFor(currentMkt, currentCap, kind);
  rows.sort((a,b)=>{
    let va=a[sk.key], vb=b[sk.key];
    if(typeof va==="string"){ return sk.dir*String(va).localeCompare(String(vb),"zh"); }
    if(va==null) va = sk.dir<0 ? -Infinity : Infinity;
    if(vb==null) vb = sk.dir<0 ? -Infinity : Infinity;
    return sk.dir*(va-vb);
  });
  renderTable(tableId, rows, withFy);
  const table = document.getElementById(tableId);
  table.querySelectorAll("th").forEach(x=>{
    x.classList.remove("sort-asc","sort-desc");
    if(x.dataset.k===sk.key) x.classList.add(sk.dir===1?"sort-asc":"sort-desc");
  });
}

function updateCaps(){
  const m = getMarket(currentMkt);
  const map = {};
  (m.caps || []).forEach(c=> map[c.key] = c.label);
  document.querySelectorAll(".cap").forEach(el=>{
    const k = el.dataset.cap;
    if(map[k] != null) el.textContent = map[k];
  });
}

function updateUnits(){
  const m = getMarket(currentMkt);
  const pu=m.price_unit, du=m.dps_unit, cu=m.cur;
  const set=(id,t)=>{const e=document.getElementById(id); if(e) e.textContent=t;};
  set("th-price",`现价(${pu})`); set("th-price2",`现价(${pu})`);
  set("th-mv",`总市值(亿${cu})`); set("th-mv2",`总市值(亿${cu})`);
  set("th-dps",`每股分红(${du})`); set("th-dps2",`每股分红(${du})`);
}

function renderAll(){
  applyTable("table-ttm", false);
  applyTable("table-lfy", true);
  const m = getMarket(currentMkt);
  const t = getTier(currentMkt, currentCap);
  document.getElementById("desc").textContent = `${m.name} · ${t.label} · 股息率排名`;
  document.getElementById("cntlab").textContent = `${m.name} ${t.label.replace(/\s+/g,"")}公司数`;
  document.getElementById("cnt").textContent = t.count;
  updateCaps();
  updateUnits();
}

function bindTable(tableId, withFy){
  const table = document.getElementById(tableId);
  table.querySelectorAll("th").forEach(th=>{
    th.addEventListener("click",()=>{
      const kind = withFy ? "lfy" : "ttm";
      const kp = th.dataset.k;
      const skey = sortKey(currentMkt, currentCap, kind);
      const sk = sortState[skey] || (sortState[skey] = {key:"yield", dir:-1});
      if(sk.key===kp){ sk.dir *= -1; }
      else { sk.key=kp; sk.dir = (kp==="rank"||kp==="name"||kp==="code")?1:-1; }
      applyTable(tableId, withFy);
    });
  });
}

function initData(d){
  DATA = d;
  document.getElementById("gen").textContent = DATA.generated_at;
  document.getElementById("ttmstart").textContent = DATA.ttm_start;
  renderAll();
}

bindTable("table-ttm", false);
bindTable("table-lfy", true);

document.querySelectorAll(".cap").forEach(c=>{
  c.addEventListener("click",()=>{
    document.querySelectorAll(".cap").forEach(x=>x.classList.remove("active"));
    c.classList.add("active");
    currentCap = c.dataset.cap;
    renderAll();
  });
});

document.querySelectorAll(".tab").forEach(t=>{
  t.addEventListener("click",()=>{
    document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));
    document.querySelectorAll(".panel").forEach(x=>x.classList.remove("active"));
    t.classList.add("active");
    document.getElementById("panel-"+t.dataset.tab).classList.add("active");
    currentTab = t.dataset.tab;
    renderAll();
  });
});

initData(EMBEDDED);
</script>
</body>
</html>
"""
    html_doc = HTML.replace("__JSON__", json_str)
    open(HTML_PATH, "w").write(html_doc)
    log(f"HTML written -> {HTML_PATH} ({len(html_doc)} bytes)")
    # 汇总输出，便于自动化验证
    summary = {}
    for m in markets:
        summary[m["name"]] = {}
        for t in m["tiers"]:
            top = t["ttm"][0] if t["ttm"] else None
            summary[m["name"]][t["label"]] = {
                "candidates": t["count"],
                "shown": len(t["ttm"]),
                "top1": (top["name"], top["yield"]) if top else None,
            }
    log("SUMMARY " + json.dumps(summary, ensure_ascii=False))

# =====================================================================
# 主流程
# =====================================================================
def main():
    log(f"=== start (TODAY={GEN}, TTM_START={TTM_START_STR}, LATEST_YEAR={LATEST_YEAR}) ===")
    t0 = time.time()

    log("[1/4] US universe")
    us_codes = get_us_codes()

    log("[2/4] US quotes")
    us_quotes = fetch_quotes(us_codes, "us")
    us_recs = [us_quotes[c] for c in us_codes if c in us_quotes]

    # 美股：过滤普通股 / 分桶（>1000亿 或 500-1000亿美元，单位亿美元）
    EXCLUDE = ("pfd", "pref", "depositary", "warrant", "wts")
    def is_common(d):
        nm = (d.get("name") or "").lower()
        return not any(s in nm for s in EXCLUDE)
    def us_bucket(d):
        mv = d.get("mv") or 0
        if mv > 1000: return 1
        if 500 < mv <= 1000: return 2
        return 0
    us_bucketed = [d for d in us_recs if is_common(d) and us_bucket(d) in (1, 2)]

    log("[3/4] US dividend history")
    us_enriched = fetch_us_div(us_bucketed)

    log("[4/4] build HTML")
    build_html([sanitize(d) for d in us_enriched])

    log(f"=== done in {time.time()-t0:.1f}s ===")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        sys.exit(1)
