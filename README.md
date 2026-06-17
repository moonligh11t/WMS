# WMS 质检报告匹配工具

可口可乐北京厂 WMS 质检报告在线匹配工具。

## 快速启动

```bash
pip install -r requirements.txt
python wms_web.py
```

访问 http://localhost:5000

## 部署

使用 gunicorn：
```bash
gunicorn -w 4 -b 0.0.0.0:5000 wms_web:app
```
