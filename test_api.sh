#!/bin/bash
# 服务器 API 测试脚本
# 先启动后端：cd backend && uvicorn main:app --port 8000
# 然后运行：bash test_api.sh

BASE="http://localhost:8000"

echo "===== 1. 健康检查 ====="
curl -s "$BASE/api/health" | python3 -m json.tool

echo ""
echo "===== 2. 配置信息 ====="
curl -s "$BASE/api/config" | python3 -m json.tool

echo ""
echo "===== 3. 基础对话（SSE 流）====="
curl -s -N -X POST "$BASE/api/chat" \
  -H "Content-Type: application/json" \
  -d '{"message":"计算 (3+4)*7，然后告诉我当前工作目录","history":[]}' \
  | head -50

echo ""
echo "===== 4. Skill 测试 ====="
curl -s -N -X POST "$BASE/api/chat" \
  -H "Content-Type: application/json" \
  -d '{"message":"加载 code-review 技能并简要说明它的作用","history":[]}' \
  | head -30

echo ""
echo "===== 5. 长对话测试（轮1）====="
curl -s -N -X POST "$BASE/api/chat" \
  -H "Content-Type: application/json" \
  -d '{"message":"我叫小红，请记住我的名字","history":[]}' \
  | head -20

echo ""
echo "✅ API 测试完成"
