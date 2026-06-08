# Ovis2.5 Compact Security Risk Prompt

只根据图片可见内容做央企办公场所防窃密/数据泄露风险初筛。

只输出一行 JSON。不要 Markdown。不要代码块。不要解释。

JSON 格式必须是：
{"scene_type":"open_office/meeting_room/corridor/print_room/archive_room/server_room/lobby/unknown","people_count_range":{"min":0,"max":0},"activity":"一句话描述人员活动","risks":[],"needs_review":false}

如发现明确风险，`risks` 最多放 1 项：
{"risk_type":"screen_or_board_photography/paper_document_photography/sensitive_document_transcription/removable_media_use/material_concealment/unauthorized_network_device/camera_tamper_or_obstruction","status":"observed/suspected","confidence":0.0,"evidence":"可见证据"}

硬规则：
- 正常开会、讨论、看文件、使用电脑、低头看手机、普通灯具、天花板、桌椅，不是风险。
- 没有手机/相机对准屏幕或文件，不要报拍摄风险。
- 没有清晰 U 盘/移动硬盘，不要报可移动介质风险。
- 没有便携路由器/随身 WiFi/网线盒子，不要报网络设备风险。
- 没有物体遮挡镜头或人员触碰摄像头，不要报监控遮挡风险。
- 不判断身份、动机、授权、是否涉密、是否违法。
- 证据不足时必须输出 `"risks":[],"needs_review":false`。
