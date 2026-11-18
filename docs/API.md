# 接口

模块GET使用仓内样例，模块POST可传csv或rows与context，直接计算当前规则。`/api/runs`使用完整流水线并原子保存。每个阶段返回rows、quarantine、metrics；后续阶段还返回累计隔离。`/api/upload`接收multipart文件file。`/api/runs/<id>/archive`只导出一份带清单与证据的CSV，其中`conclusion`行为孤存结论，与门禁、对账读取同一份结论。

## 孤存结论

- `GET /api/runs/<id>` 的 `orphan_conclusions`、`conclusion_digest`：该批次当前有效的孤存结论与摘要。
- `GET /api/runs/<id>/conclusions`：该批次落库后的当前结论，含`conclusion_id`与证据。
- `GET /api/conclusions`：所有批次的最新有效结论（每个批次键只取最新一次补发）。
- `GET /api/conclusions/history?batch=&doc_id=`：全部历史结论，含已被新证据取代的旧结论。
- `GET /api/runs/<id>/records?doc_id=`：回到批次载荷与源行号追回原始记录。

`POST /api/runs` 可传参数：`as_of`、`max_delay_seconds`、`source_watermark`、`orphan_grace_seconds`、`batch_id`。同一`batch_id`的补发重试按（文档，结论编号）去重：证据未变的旧结论不重复出现；结论变化时旧结论移出当前结果但保留在历史接口与原始批次中。
