"""
WMS 质检报告匹配工具 - Web 版
==============================
适用场景：
  - 本机调试：python wms_web.py
  - 服务器部署：gunicorn -w 4 -b 0.0.0.0:5000 wms_web:app

部署到 Nginx 反代示例（/wms/ 路径前缀）：
  location /wms/ {
      proxy_pass http://127.0.0.1:5000/;
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
  }
"""

import os
import re
import io
import time
import json
import pickle
import zipfile
import threading
import uuid
import socket
import requests
import pandas as pd
from flask import Flask, render_template, request, jsonify, send_file, Blueprint
from werkzeug.middleware.proxy_fix import ProxyFix

# ================== 配置 ==================

class Config:
    # 简道云 API 配置
    APP_ID = "64cb10852e0a1a000839c489"
    ENTRY_ID = "64cc64c4fca89500080e4b71"
    APP_TOKEN = "Bearer jNBMJmq94vkwwoRyzB8JM37Wl8RrLvMw02971Cf46953432BcBb0223D0012cEa4"

    # Web 服务配置（可通过环境变量覆盖）
    SECRET_KEY = os.urandom(24).hex()
    URL_PREFIX = os.environ.get("WMS_URL_PREFIX", "")  # 如 /wms
    PORT = int(os.environ.get("WMS_PORT", 443))
    HOST = os.environ.get("WMS_HOST", "0.0.0.0")
    DEBUG = os.environ.get("WMS_DEBUG", "").lower() in ("1", "true", "yes")

    # 工作目录
    BASE_DIR = os.environ.get(
        "WMS_WORK_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_workspace")
    )
    CACHE_EXPIRE = 60 * 60

    # 简道云字段名
    BRAND_FIELD = "_widget_1691032933437"
    FILE_FIELD = "_widget_1691029648365"


# ================== 核心逻辑 ==================

def get_all_files(rec, config):
    """获取简道云记录中的全部附件，一个记录可能有多个文件"""
    files = rec.get(config.FILE_FIELD, [])
    result = []
    if isinstance(files, list):
        for f in files:
            if "name" in f:
                result.append((f.get("name", ""), f.get("url", "")))
            elif "value" in f:
                result.append((f["value"].get("name", ""), f["value"].get("url", "")))
    return result


def normalize_brand(text):
    text = str(text)
    # 判断是否无糖/零度/零卡版本
    is_zero = "零度" in text or "零卡" in text or "无糖" in text or "zero" in text.lower()

    if any(k in text for k in ["可口可乐", "可乐", "coke"]):
        return "零度可乐" if is_zero else "可乐"
    if "雪碧" in text:
        return "零度雪碧" if is_zero else "雪碧"
    if "芬达" in text:
        return "零度芬达" if is_zero else "芬达"
    if "果粒橙" in text or "美汁源" in text:
        return "美汁源"
    if "魔爪" in text:
        return "魔爪"
    if "纯悦" in text:
        return "纯悦"
    if "怡泉" in text:
        return "怡泉"
    if "酸梅汤" in text:
        return "酸梅汤"
    return text


def normalize_ml(text):
    text = str(text).lower()
    # 先找带 ml 的（如 330ml、550ml；也支持 pet500ml 连写）
    m = re.search(r'(\d+)\s*ml(?![a-z0-9])', text)
    if m:
        return m.group(1)
    # 再找带 L 的（如 10L → 10000；也支持 2L 连写）
    m = re.search(r'(\d+(\.\d+)?)\s*l(?![a-z0-9])', text)
    if m:
        return str(int(float(m.group(1)) * 1000))
    # 最后找孤立的 3-4 位数字（不要匹配日期）
    candidates = re.findall(r'(?a)\b(\d{3,4})\b', text)
    for c in candidates:
        if int(c) > 300:
            return c
    return ""


def parse_filename(filename):
    text = str(filename).lower().strip()
    text = re.sub(r'\.(pdf|jpg|png)$', '', text).strip()

    # 日期：优先 8 位（20240921），再 6 位（231105）
    date = ""
    m = re.search(r'(?a)\b(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\b', text)
    if m:
        date = m.group(1)[-2:] + m.group(2) + m.group(3)
    else:
        m = re.search(r'(?a)\b(\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\b', text)
        if m:
            date = m.group(1) + m.group(2) + m.group(3)

    ml = normalize_ml(text)

    loc = ""
    m = re.search(r'(bj[a-z0-9]+)', text)
    if m:
        loc = m.group(1).upper()

    brand = ""
    for b in ["零度", "零卡", "魔爪", "芬达", "雪碧", "可乐", "果粒橙", "美汁源", "纯悦", "怡泉", "酸梅汤"]:
        if b in text:
            brand = normalize_brand(b)
            break

    return {"date": date, "ml": ml, "brand": brand, "location": loc}


def extract_wms(desc):
    desc = str(desc)
    brand = normalize_brand(desc.split("/")[0])
    ml = normalize_ml(desc)
    pack = "瓶" if "PET" in desc.upper() else "罐"
    # 提取子品牌/口味：怡泉/汤力水 → "汤力水", 魔爪/超越葡萄 → "超越葡萄"
    sub_brand = ""
    parts = desc.split("/", 1)
    if len(parts) > 1:
        rest = parts[1]
        for sep in ["，", ",", "、"]:
            if sep in rest:
                rest = rest.split(sep)[0]
        if "/" in rest:
            rest = rest.split("/")[0]
        sub_brand = rest.strip()
    # 提取包数：PET2.00L/*6/ → "6"
    pack_count = ""
    m = re.search(r'\*(\d+)', desc)
    if m:
        pack_count = m.group(1)
    return brand, ml, pack, sub_brand, pack_count


def format_date(dt):
    try:
        return pd.to_datetime(dt).strftime("%y%m%d")
    except:
        return ""


def calc_score(w, j, factory="", filename=""):
    """日期必须一致 → 容量双方都标了必须一致 → 品牌匹配 → 工厂优先 → 子品牌匹配"""
    score = 0

    # 1. 日期必须匹配（硬条件）
    if w["date"] and j["date"] and w["date"] == j["date"]:
        score += 0.6
    else:
        return 0  # 日期不匹配直接淘汰

    w_ml = w.get("ml", "")
    j_ml = j.get("ml", "")

    # 2. 容量：双方都标了容量则必须一致，否则不匹配
    if w_ml and j_ml:
        if w_ml == j_ml:
            score += 0.3  # 容量一致加分
        else:
            return 0  # 都标了容量但不一样 → 不是同一产品
    # 如果有一方没标容量（如魔爪超越仙境只一种规格），靠品牌判断

    # 3. 品牌匹配 — 权重 0.25
    if normalize_brand(w["brand"]) == normalize_brand(j["brand"]):
        score += 0.25

    # 4. 工厂优先：北京 +0.15
    #   同时检查简道云工厂字段 和 文件名地点（如 BJ06）
    factory = str(factory)
    location = j.get("location", "")
    is_beijing = (
        "北京" in factory or
        factory.upper().startswith("BJ") or
        location.upper().startswith("BJ")
    )
    if is_beijing:
        score += 0.15

    # 5. 子品牌/口味匹配 — 权重 0.1
    #   如：WMS写"汤力水"，文件名含"汤力水"的优先
    sub_brand = w.get("sub_brand", "")
    if sub_brand and sub_brand.lower() in filename.lower():
        score += 0.1

    # 6. 包数匹配 — 权重 0.05
    #   如：WMS写/*6/，文件名含\"6\"（word boundary）的优先
    pack_count = w.get("pack_count", "")
    if pack_count and re.search(r'(?a)\b' + pack_count + r'\b', filename):
        score += 0.05

    return score


def fetch_all(config):
    """从简道云拉取数据（带1小时缓存）"""
    cache_file = os.path.join(config.BASE_DIR, "jd_cache.pkl")

    # 有缓存且没过期 → 直接用
    if os.path.exists(cache_file):
        mtime = os.path.getmtime(cache_file)
        if time.time() - mtime < config.CACHE_EXPIRE:
            with open(cache_file, "rb") as f:
                return pickle.load(f)

    # 没缓存 → 拉接口
    url = "https://api.jiandaoyun.com/api/v5/app/entry/data/list"
    headers = {"Authorization": config.APP_TOKEN, "Content-Type": "application/json"}
    all_data = []
    data_id = None

    # 只拉2026年的数据
    year_filter = {
        "rel": "and",
        "cond": [{
            "field": "_widget_1691032933442",
            "type": "text",
            "method": "like",
            "value": ["2026"]
        }]
    }

    while True:
        payload = {
            "app_id": config.APP_ID,
            "entry_id": config.ENTRY_ID,
            "limit": 100,
            "filter": year_filter
        }
        if data_id:
            payload["data_id"] = data_id
        try:
            res = requests.post(url, json=payload, headers=headers, timeout=30)
            data = res.json().get("data", [])
        except:
            break
        if not data:
            break
        all_data.extend(data)
        data_id = data[-1]["_id"]
        if len(data) < 100:
            break
        time.sleep(0.2)

    # 存缓存
    with open(cache_file, "wb") as f:
        pickle.dump(all_data, f)

    return all_data


# ================== Web 应用工厂 ==================

tasks = {}  # 任务状态存储


def create_app(config=None):
    """创建 Flask 应用"""
    if config is None:
        config = Config()

    # 初始化 Flask
    app = Flask(__name__)
    app.config.from_object(config)
    app.secret_key = config.SECRET_KEY
    app.config["URL_PREFIX"] = config.URL_PREFIX

    # 反向代理支持（获取真实IP）
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # 创建蓝图
    prefix = config.URL_PREFIX
    bp = Blueprint("wms", __name__, url_prefix=prefix if prefix else None)

    # 创建工作目录
    os.makedirs(config.BASE_DIR, exist_ok=True)

    # ============ 路由 ============

    @bp.route("/")
    def index():
        return render_template("index.html", url_prefix=config.URL_PREFIX)

    @bp.route("/api/run", methods=["POST"])
    def run_matching():
        if "file" not in request.files:
            return jsonify({"error": "请上传 WMS Excel 文件"}), 400
        file = request.files["file"]
        if not file.filename:
            return jsonify({"error": "请选择文件"}), 400

        task_id = str(uuid.uuid4())[:8]
        work_dir = os.path.join(config.BASE_DIR, task_id)
        os.makedirs(work_dir, exist_ok=True)

        wms_path = os.path.join(work_dir, "wms.xlsx")
        file.save(wms_path)

        tasks[task_id] = {
            "status": "running",
            "progress": 0,
            "logs": [],
            "results": [],
            "success": 0,
            "fail": 0,
            "total": 0,
            "work_dir": work_dir,
            "report_dir": os.path.join(work_dir, "reports"),
        }

        thread = threading.Thread(
            target=_do_matching,
            args=(task_id, wms_path, config),
            daemon=True
        )
        thread.start()

        return jsonify({"task_id": task_id})

    @bp.route("/api/status/<task_id>")
    def get_status(task_id):
        task = tasks.get(task_id)
        if not task:
            return jsonify({"status": "expired", "error": "任务已过期或不存在"})
        return jsonify({
            "status": task["status"],
            "progress": task["progress"],
            "logs": task["logs"][-20:],
            "results": task["results"][-50:],
            "success": task["success"],
            "fail": task["fail"],
            "total": task["total"],
        })

    @bp.route("/api/download/<task_id>")
    def download_results(task_id):
        task = tasks.get(task_id)
        if not task:
            return jsonify({"error": "任务不存在"}), 404

        report_dir = task["report_dir"]
        log_path = os.path.join(task["work_dir"], "匹配结果.xlsx")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            if os.path.exists(log_path):
                zf.write(log_path, "匹配结果.xlsx")
            if os.path.exists(report_dir):
                for fname in os.listdir(report_dir):
                    fpath = os.path.join(report_dir, fname)
                    zf.write(fpath, f"reports/{fname}")
        buf.seek(0)

        return send_file(
            buf, mimetype="application/zip", as_attachment=True,
            download_name=f"wms_results_{task_id}.zip"
        )

    @bp.route("/api/clear/<task_id>", methods=["POST"])
    def clear_task(task_id):
        task = tasks.pop(task_id, None)
        if task:
            import shutil
            shutil.rmtree(task["work_dir"], ignore_errors=True)
        return jsonify({"ok": True})

    app.register_blueprint(bp)
    return app


def _do_matching(task_id, wms_path, config):
    """后台执行匹配"""
    task = tasks[task_id]
    report_dir = task["report_dir"]
    os.makedirs(report_dir, exist_ok=True)

    def log(msg):
        task["logs"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    try:
        log("📖 读取 WMS 文件...")
        df = pd.read_excel(wms_path)
        total = len(df)
        task["total"] = total
        log(f"✅ 读取到 {total} 条产品记录")

        log("🌐 从简道云拉取数据...")
        records = fetch_all(config)
        log(f"✅ 获取到 {len(records)} 条记录")

        log("🔍 解析附件文件名...")
        parsed_records = []
        for r in records:
            files = get_all_files(r, config)
            if not files:
                continue
            factory = r.get("_widget_1691033492836", "")
            for name, url in files:
                if not name:
                    continue
                parsed = parse_filename(name)
                parsed_records.append({"rec": r, "name": name, "url": url, "parsed": parsed, "factory": factory})
        log(f"✅ 有效附件：{len(parsed_records)} 个")

        # 北京优先排序：BJ 记录排前面，同分时先匹配 BJ
        def is_bj_record(item):
            factory = str(item.get("factory", ""))
            location = item["parsed"].get("location", "")
            return "北京" in factory or location.upper().startswith("BJ")

        parsed_records.sort(key=lambda x: is_bj_record(x), reverse=True)
        log(f"📌 北京工厂记录：{sum(1 for x in parsed_records if is_bj_record(x))} 个")

        log("🔄 开始匹配...")
        result_rows = []

        for idx, (_, row) in enumerate(df.iterrows()):
            desc = row["产品描述（L）"]
            date_key = format_date(row["生产日期"])
            brand, ml, pack, sub_brand, pack_count = extract_wms(desc)
            wms_item = {"brand": brand, "ml": ml, "date": date_key, "sub_brand": sub_brand, "pack_count": pack_count}

            best = None
            best_score = 0
            candidates = []  # 调试用
            for item in parsed_records:
                j = item["parsed"]
                factory = item.get("factory", "")
                score = calc_score(wms_item, j, factory, item["name"])
                if score > 0:
                    candidates.append((score, item["name"], j, factory))
                if score > best_score:
                    best_score = score
                    best = item

            # 调试：如果选了JN，打印所有候选（看BJ为什么没赢）
            if best and best_score >= 0.6 and "JN" in best["name"].upper():
                log(f"🔍 {desc} | 日期={date_key} ml={ml} 品牌={brand}")
                for s, n, j, f in sorted(candidates, reverse=True)[:10]:
                    bj = "🏷BJ" if ("BJ" in n.upper() or "北京" in str(f)) else ""
                    log(f"   {s:.2f}分 {n}  {bj}")

            if best and best_score >= 0.6:
                def safe_filename(text):
                    return re.sub(r'[\\/:*?"<>|]', '_', str(text))
                desc_safe = safe_filename(desc)
                save_name = f"{date_key}_{desc_safe}_{int(time.time() * 1000)}.pdf"
                path = os.path.join(report_dir, save_name)
                r = requests.get(best["url"])
                with open(path, "wb") as f:
                    f.write(r.content)
                status = "成功"
                task["success"] += 1
                log(f"✅ {save_name} 得分={best_score:.2f}")
            else:
                status = "失败"
                task["fail"] += 1
                log(f"❌ {desc} 未匹配")

            result_rows.append([desc, best["name"] if best else "", f"{best_score:.2f}", status])
            task["results"] = result_rows
            task["progress"] = int((idx + 1) / total * 100)

        log_path = os.path.join(task["work_dir"], "匹配结果.xlsx")
        pd.DataFrame(result_rows, columns=["产品", "文件", "得分", "状态"]).to_excel(log_path, index=False)
        log(f"📊 结果已保存")
        log(f"🎉 完成！成功 {task['success']} / 失败 {task['fail']}")
        task["status"] = "done"

    except Exception as e:
        log(f"🔥 错误：{e}")
        import traceback
        log(traceback.format_exc())
        task["status"] = "error"


# ================== 模板 HTML ==================

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WMS 质检报告匹配工具</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, "Microsoft YaHei", sans-serif; background: #f0f2f5; color: #333; min-height: 100vh; }
.header { background: linear-gradient(135deg, #1a73e8, #0d47a1); color: #fff; padding: 20px 30px; }
.header h1 { font-size: 22px; }
.header p { font-size: 13px; opacity: .8; margin-top: 4px; }
.container { max-width: 1000px; margin: 0 auto; padding: 20px; }
.card { background: #fff; border-radius: 10px; box-shadow: 0 2px 8px rgba(0,0,0,.08); padding: 24px; margin-bottom: 16px; }
.card h2 { font-size: 16px; margin-bottom: 16px; color: #1a73e8; }
.upload-area { border: 2px dashed #ccc; border-radius: 8px; padding: 30px; text-align: center; cursor: pointer; transition: .2s; }
.upload-area:hover, .upload-area.dragover { border-color: #1a73e8; background: #f0f7ff; }
.upload-area .icon { font-size: 40px; }
.upload-area p { color: #666; margin-top: 8px; font-size: 14px; }
.upload-area .filename { color: #1a73e8; font-weight: bold; margin-top: 8px; }
.btn { display: inline-flex; align-items: center; gap: 6px; padding: 10px 24px; border: none; border-radius: 6px; font-size: 14px; cursor: pointer; transition: .2s; }
.btn-primary { background: #1a73e8; color: #fff; }
.btn-primary:hover { background: #1557b0; }
.btn-primary:disabled { background: #93b8f0; cursor: not-allowed; }
.btn-success { background: #34a853; color: #fff; }
.btn-success:hover { background: #2d8f47; }
.btn-danger { background: #ea4335; color: #fff; }
.btn-danger:hover { background: #c62828; }
.log-box { background: #1e1e1e; color: #d4d4d4; font-family: Consolas, monospace; font-size: 12px; padding: 12px; border-radius: 6px; height: 250px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; line-height: 1.6; }
.results-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.results-table th { background: #f5f7fa; padding: 8px 12px; text-align: left; font-weight: 600; border-bottom: 2px solid #e0e0e0; }
.results-table td { padding: 8px 12px; border-bottom: 1px solid #eee; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 600; }
.tag-success { background: #e6f4ea; color: #34a853; }
.tag-fail { background: #fce8e6; color: #ea4335; }
.stats { display: flex; gap: 20px; margin: 12px 0; font-size: 14px; }
.stats-item { text-align: center; padding: 10px 20px; background: #f8f9fa; border-radius: 8px; flex: 1; }
.stats-item .num { font-size: 24px; font-weight: 700; }
.stats-item .label { color: #666; font-size: 12px; margin-top: 2px; }
.actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 16px; }
.hidden { display: none; }
.progress-bar { height: 4px; background: #e0e0e0; border-radius: 2px; margin: 12px 0; overflow: hidden; }
.progress-fill { height: 100%; background: #1a73e8; border-radius: 2px; transition: width .3s; width: 0; }
</style>
</head>
<body>
<div class="header">
  <h1>📋 WMS 质检报告智能匹配</h1>
  <p>上传 WMS Excel 文件，自动从简道云匹配并下载质检报告</p>
</div>
<div class="container">

  <div class="card">
    <h2>📂 上传 WMS 文件</h2>
    <div class="upload-area" id="uploadArea">
      <div class="icon">📄</div>
      <p>点击选择或拖拽 WMS Excel 文件到这里</p>
      <div class="filename" id="fileName"></div>
    </div>
    <input type="file" id="fileInput" accept=".xlsx,.xls" style="display:none">
    <div style="margin-top: 16px;">
      <button class="btn btn-primary" id="runBtn" disabled onclick="startMatch()">▶ 开始匹配</button>
    </div>
  </div>

  <div class="card hidden" id="progressCard">
    <h2>⏳ 运行进度</h2>
    <div class="progress-bar"><div class="progress-fill" id="progressFill"></div></div>
    <div class="log-box" id="logBox"></div>
    <div class="stats" id="statsArea">
      <div class="stats-item"><div class="num" id="statTotal">-</div><div class="label">总计</div></div>
      <div class="stats-item"><div class="num" id="statSuccess" style="color:#34a853">-</div><div class="label">成功</div></div>
      <div class="stats-item"><div class="num" id="statFail" style="color:#ea4335">-</div><div class="label">失败</div></div>
    </div>
  </div>

  <div class="card hidden" id="resultCard">
    <h2>📊 匹配结果</h2>
    <div style="max-height: 300px; overflow-y: auto;">
      <table class="results-table">
        <thead><tr><th>产品描述</th><th>匹配文件</th><th>得分</th><th>状态</th></tr></thead>
        <tbody id="resultBody"></tbody>
      </table>
    </div>
    <div class="actions">
      <button class="btn btn-success" id="downloadBtn" onclick="downloadResults()">⬇ 下载结果</button>
      <button class="btn btn-primary" onclick="resetPage()">🔄 重新匹配</button>
    </div>
  </div>

</div>

<script>
const URL_PREFIX = "{{ url_prefix|default('') }}";

let taskId = null;
let pollTimer = null;

const uploadArea = document.getElementById('uploadArea');
const fileInput = document.getElementById('fileInput');

uploadArea.onclick = () => fileInput.click();
uploadArea.ondragover = e => { e.preventDefault(); uploadArea.classList.add('dragover'); };
uploadArea.ondragleave = () => uploadArea.classList.remove('dragover');
uploadArea.ondrop = e => { e.preventDefault(); uploadArea.classList.remove('dragover'); handleFile(e.dataTransfer.files[0]); };
fileInput.onchange = () => handleFile(fileInput.files[0]);

function handleFile(file) {
  if (!file) return;
  document.getElementById('fileName').textContent = '✅ ' + file.name;
  document.getElementById('runBtn').disabled = false;
  window._uploadedFile = file;
}

async function startMatch() {
  const file = window._uploadedFile;
  if (!file) return;

  document.getElementById('runBtn').disabled = true;
  document.getElementById('progressCard').classList.remove('hidden');
  document.getElementById('resultCard').classList.add('hidden');
  document.getElementById('logBox').innerHTML = '';
  document.getElementById('resultBody').innerHTML = '';

  const formData = new FormData();
  formData.append('file', file);

  try {
    const res = await fetch(URL_PREFIX + '/api/run', { method: 'POST', body: formData });
    const data = await res.json();
    if (data.error) {
      addLog('❌ ' + data.error);
      document.getElementById('runBtn').disabled = false;
      return;
    }
    taskId = data.task_id;
    addLog('✅ 任务已创建，ID: ' + taskId);
    pollTimer = setInterval(() => pollStatus(), 800);
  } catch (e) {
    addLog('❌ 网络错误：无法连接到服务器');
    document.getElementById('runBtn').disabled = false;
  }
}

function addLog(msg) {
  const box = document.getElementById('logBox');
  box.innerHTML += '<div>' + msg + '</div>';
  box.scrollTop = box.scrollHeight;
}

async function pollStatus() {
  if (!taskId) return;
  const res = await fetch(URL_PREFIX + '/api/status/' + taskId);
  const data = await res.json();

  const logBox = document.getElementById('logBox');
  if (data.logs) {
    logBox.innerHTML = data.logs.map(l => `<div>${l}</div>`).join('');
    logBox.scrollTop = logBox.scrollHeight;
  }

  document.getElementById('progressFill').style.width = (data.progress || 0) + '%';
  document.getElementById('statTotal').textContent = data.total || '-';
  document.getElementById('statSuccess').textContent = data.success || 0;
  document.getElementById('statFail').textContent = data.fail || 0;

  if (data.results && data.results.length > 0) {
    const tbody = document.getElementById('resultBody');
    tbody.innerHTML = data.results.map(r => {
      const cls = r[3] === '成功' ? 'tag-success' : 'tag-fail';
      return `<tr><td>${r[0]}</td><td>${r[1]}</td><td>${r[2]}</td><td><span class="tag ${cls}">${r[3]}</span></td></tr>`;
    }).join('');
  }

  if (data.status === 'done' || data.status === 'error' || data.status === 'expired') {
    clearInterval(pollTimer);
    if (data.status === 'expired') {
      // 旧任务已过期，忽略
      return;
    }
    document.getElementById('resultCard').classList.remove('hidden');
    document.getElementById('runBtn').disabled = false;
    if (data.status === 'done') {
      document.getElementById('progressFill').style.width = '100%';
      document.getElementById('progressFill').style.background = '#34a853';
    } else {
      document.getElementById('progressFill').style.background = '#ea4335';
    }
  }
}

async function downloadResults() {
  if (!taskId) return;
  window.open(URL_PREFIX + '/api/download/' + taskId, '_blank');
}

function resetPage() {
  clearInterval(pollTimer);
  taskId = null;
  document.getElementById('progressCard').classList.add('hidden');
  document.getElementById('resultCard').classList.add('hidden');
  document.getElementById('fileName').textContent = '';
  document.getElementById('runBtn').disabled = true;
  document.getElementById('progressFill').style.width = '0';
  document.getElementById('progressFill').style.background = '#1a73e8';
  fileInput.value = '';
}
</script>
</body>
</html>"""


# ================== 入口 ==================

app = create_app()


def _write_template(app):
    """自动生成 HTML 模板文件"""
    template_dir = os.path.join(app.root_path, "templates")
    os.makedirs(template_dir, exist_ok=True)
    html_path = os.path.join(template_dir, "index.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(HTML_TEMPLATE)


# 自动写入模板文件
_write_template(app)

if __name__ == "__main__":

    cfg = app.config
    prefix = cfg.get("URL_PREFIX", "")
    prefix_display = prefix or "/"

    print("=" * 50)
    print("  📋 WMS 质检报告匹配工具")
    print("=" * 50)
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except:
        local_ip = "?"
    print(f"  本地地址: http://127.0.0.1:{cfg['PORT']}{prefix}")
    print(f"  局域网:   http://{local_ip}:{cfg['PORT']}{prefix}")
    print(f"  路径前缀: {prefix_display}")
    print(f"  工作目录: {cfg['BASE_DIR']}")
    print("-" * 50)
    print(f"  生产部署: gunicorn -w 4 -b 0.0.0.0:{cfg['PORT']} wms_web:app")
    print(f"  路径前缀: WMS_URL_PREFIX=/wms gunicorn ...")
    print("=" * 50)

    app.run(host=cfg["HOST"], port=cfg["PORT"], debug=cfg["DEBUG"], threaded=True)
