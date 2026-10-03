#!/usr/bin/env bash
# 博物馆展柜温度暴露审定服务 —— curl 调用示例
# 用法：BASE_URL=http://localhost:8000 bash examples/curl-examples.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
api() { # method path [json]
  local method="$1" path="$2" json="${3:-}"
  if [[ -n "$json" ]]; then
    curl -sS -X "$method" "$BASE_URL$path" -H 'Content-Type: application/json' -d "$json"
  else
    curl -sS -X "$method" "$BASE_URL$path"
  fi
  echo
}

echo "== 健康检查 =="
api GET /health

echo "== 1. 创建审定窗口（1 小时；允许 18~22°C；最长连续超限 300s；最大采样间隔 600s）=="
WINDOW=$(api POST /api/v1/windows '{
  "name": "展柜A-2026-10-01",
  "start_at": "2026-10-01T00:00:00Z",
  "end_at":   "2026-10-01T01:00:00Z",
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 22.0,
  "max_continuous_exceed_seconds": 300,
  "max_sample_interval_seconds": 600
}')
echo "$WINDOW" | python3 -m json.tool
WID=$(echo "$WINDOW" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

echo "== 2. 提交读数（reading_id + revision）=="
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-00","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.1}' >/dev/null
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-01","revision":1,"measured_at":"2026-10-01T00:10:00Z","temperature":22.4}' >/dev/null
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-02","revision":1,"measured_at":"2026-10-01T00:20:00Z","temperature":23.1}' >/dev/null
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-03","revision":1,"measured_at":"2026-10-01T00:30:00Z","temperature":20.5}' >/dev/null

echo "== 3. 相同 ID + 修订号重复提交（幂等，内容一致）=="
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-00","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.1}'

echo "== 4. 相同 ID + 修订号但温度冲突 -> 409 =="
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-00","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":99.9}' || true

echo "== 5. 旧修订号 -> 409；更高修订号替换重算 =="
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-01","revision":1,"measured_at":"2026-10-01T00:10:00Z","temperature":22.4}' >/dev/null
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-01","revision":1,"measured_at":"2026-10-01T00:10:00Z","temperature":20.0}' || true
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-01","revision":2,"measured_at":"2026-10-01T00:10:00Z","temperature":20.0}'

echo "== 6. 补齐整窗读数（间隔 <= 600s；末尾含窗口终点读数作为覆盖终止采样）=="
for min in 40 50 60; do
  if [[ "$min" -eq 60 ]]; then ts="2026-10-01T01:00:00Z"; else ts="2026-10-01T00:$min:00Z"; fi
  api POST "/api/v1/windows/$WID/readings" \
    "{\"reading_id\":\"r-$min\",\"revision\":1,\"measured_at\":\"$ts\",\"temperature\":20.0}" >/dev/null
done

echo "== 7. 查看当前审定（区段 + 最长连续超限 + 结论）=="
api GET "/api/v1/windows/$WID/evaluation" | python3 -m json.tool

echo "== 8. 首次签发（v1）=="
api POST "/api/v1/windows/$WID/certificates/issue" '{"note":"首次签发"}' | python3 -m json.tool

echo "== 9. 修订 r-02 为正常值后再次签发（v2；v1 快照不变）=="
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-02","revision":2,"measured_at":"2026-10-01T00:20:00Z","temperature":20.2}' >/dev/null
api POST "/api/v1/windows/$WID/certificates/issue" '{"note":"修订后重新签发"}' | python3 -m json.tool

echo "== 10. 列出全部证书版本 =="
api GET "/api/v1/windows/$WID/certificates" | python3 -m json.tool

echo "== 11. 修订影响报告：比对 v1/v2 两张已签发证书的不可变快照 =="
# 从版本列表取两个证书主键 ID（响应只解析一次，避免 api 尾部换行拆词）
CIDS=$(api GET "/api/v1/windows/$WID/certificates" \
  | python3 -c "import sys,json;print(' '.join(str(c['id']) for c in json.load(sys.stdin)))")
read -r OLD_ID NEW_ID <<< "$CIDS"
api GET "/api/v1/certificates/revision-report?old_certificate_id=$OLD_ID&new_certificate_id=$NEW_ID" \
  | python3 -m json.tool

echo "== 12. 报告拒绝：版本倒序 / 跨窗口 / 同 ID（均 422），不存在 404 =="
api GET "/api/v1/certificates/revision-report?old_certificate_id=$NEW_ID&new_certificate_id=$OLD_ID" || true
api GET "/api/v1/certificates/revision-report?old_certificate_id=$OLD_ID&new_certificate_id=99999" || true

echo "== 13. 采样仪器补传：单独建一个窗口演示批量提交（整批事务）=="
BW=$(api POST /api/v1/windows '{
  "name": "展柜B-补传",
  "start_at": "2026-10-01T00:00:00Z",
  "end_at":   "2026-10-01T01:00:00Z",
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 22.0,
  "max_continuous_exceed_seconds": 300,
  "max_sample_interval_seconds": 600
}')
BID=$(echo "$BW" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

echo "-- 13.1 一次批量创建多条读数（逐项返回 created，并按最终状态重算审定）--"
api POST "/api/v1/windows/$BID/readings/batch" '{
  "readings": [
    {"reading_id":"a","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.1},
    {"reading_id":"b","revision":1,"measured_at":"2026-10-01T00:10:00Z","temperature":23.0},
    {"reading_id":"c","revision":1,"measured_at":"2026-10-01T00:20:00Z","temperature":20.2}
  ]
}' | python3 -c "import sys,json;d=json.load(sys.stdin);print('effects:',[x['effect'] for x in d['results']]);print('verdict:',d['current_evaluation']['verdict'])"

echo "-- 13.2 同批重放：全部 idempotent，不产生新修订 --"
api POST "/api/v1/windows/$BID/readings/batch" '{
  "readings": [
    {"reading_id":"a","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.1},
    {"reading_id":"b","revision":1,"measured_at":"2026-10-01T00:10:00Z","temperature":23.0},
    {"reading_id":"c","revision":1,"measured_at":"2026-10-01T00:20:00Z","temperature":20.2}
  ]
}' | python3 -c "import sys,json;print('effects:',[x['effect'] for x in json.load(sys.stdin)['results']])"

echo "-- 13.3 两条读数以更高修订号互换采样时刻（按最终状态判同刻，顺序无关，均 updated）--"
api POST "/api/v1/windows/$BID/readings/batch" '{
  "readings": [
    {"reading_id":"a","revision":2,"measured_at":"2026-10-01T00:10:00Z","temperature":20.1},
    {"reading_id":"b","revision":2,"measured_at":"2026-10-01T00:00:00Z","temperature":20.0}
  ]
}' | python3 -c "import sys,json;print([(x['reading']['reading_id'],x['effect'],x['reading']['measured_at']) for x in json.load(sys.stdin)['results']])"

echo "-- 13.4 整批拒收：一项 409（b 用旧修订号 rev1）则整批不变（c 不会被创建）--"
api POST "/api/v1/windows/$BID/readings/batch" '{
  "readings": [
    {"reading_id":"b","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.0},
    {"reading_id":"x","revision":1,"measured_at":"2026-10-01T00:30:00Z","temperature":20.0}
  ]
}' || true
echo "活动读数（应仍为 a/b/c 三条，无 x）："
api GET "/api/v1/windows/$BID/readings" \
  | python3 -c "import sys,json;print(sorted(r['reading_id'] for r in json.load(sys.stdin)))"

echo "-- 13.5 整批拒收：同批 reading_id 重复（422）；空批次也是 422 --"
api POST "/api/v1/windows/$BID/readings/batch" '{
  "readings": [
    {"reading_id":"a","revision":3,"measured_at":"2026-10-01T00:30:00Z","temperature":20.0},
    {"reading_id":"a","revision":3,"measured_at":"2026-10-01T00:40:00Z","temperature":20.0}
  ]
}' || true
api POST "/api/v1/windows/$BID/readings/batch" '{"readings":[]}' || true

echo "== 14. 误采作废与恢复（窗口 $WID 上演示）=="
echo "-- 14.1 作废 r-03（当前 rev1）：更高修订号 rev2 + 非空原因；当前审定立即剔除并重算 --"
api POST "/api/v1/windows/$WID/readings/r-03/void" \
  '{"revision":2,"reason":"误采：该时刻传感器未校准"}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('effect:',d['effect']);print('void:',json.dumps(d['void'],ensure_ascii=False));print('verdict:',d['current_evaluation']['verdict'],'reading_count:',d['current_evaluation']['reading_count'])"

echo "-- 14.2 相同作废修订号+相同原因重放 -> 幂等（不产生新修订）--"
api POST "/api/v1/windows/$WID/readings/r-03/void" \
  '{"revision":2,"reason":"误采：该时刻传感器未校准"}' \
  | python3 -c "import sys,json;print('effect:',json.load(sys.stdin)['effect'])"

echo "-- 14.3 拒绝路径：同修订号不同原因 409 / 低修订号 409 / 从未存在 404 / 已作废再以新修订号作废 409 / 空原因 422 --"
api POST "/api/v1/windows/$WID/readings/r-03/void" '{"revision":2,"reason":"另一个原因"}' || true
api POST "/api/v1/windows/$WID/readings/r-03/void" '{"revision":1,"reason":"x"}' || true
api POST "/api/v1/windows/$WID/readings/ghost/void" '{"revision":1,"reason":"x"}' || true
api POST "/api/v1/windows/$WID/readings/r-03/void" '{"revision":3,"reason":"再次作废"}' || true
api POST "/api/v1/windows/$WID/readings/r-03/void" '{"revision":3,"reason":"   "}' || true

echo "-- 14.4 修订历史：作废作为该 ID 的修订留存（原因/操作时间/前一版本）--"
api GET "/api/v1/windows/$WID/readings/r-03/revisions" | python3 -m json.tool

echo "-- 14.5 签发 v3 反映作废；修订影响报告 v2->v3 呈现 r-03 移除（removed）--"
api POST "/api/v1/windows/$WID/certificates/issue" '{"note":"作废 r-03 后签发"}' >/dev/null
CV=$(api GET "/api/v1/windows/$WID/certificates" \
  | python3 -c "import sys,json;cs=json.load(sys.stdin);print(cs[-2]['id'],cs[-1]['id'])")
read -r V2_ID V3_ID <<< "$CV"
api GET "/api/v1/certificates/revision-report?old_certificate_id=$V2_ID&new_certificate_id=$V3_ID" \
  | python3 -c "import sys,json;d=json.load(sys.stdin)['readings_diff'];print('removed:',[r['reading_id'] for r in d['removed']],'added:',[r['reading_id'] for r in d['added']])"

echo "-- 14.6 旧版提交不得复活：重放作废前 rev1 -> 409 --"
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-03","revision":1,"measured_at":"2026-10-01T00:30:00Z","temperature":20.5}' || true

echo "-- 14.7 恢复：提交更高修订号 rev3（effect=restored，立即重算）--"
api POST "/api/v1/windows/$WID/readings" \
  '{"reading_id":"r-03","revision":3,"measured_at":"2026-10-01T00:30:00Z","temperature":20.4}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('effect:',d['effect'],'rev:',d['reading']['revision'],'verdict:',d['current_evaluation']['verdict'])"

echo "-- 14.8 签发 v4 反映恢复；报告 v3->v4 呈现 r-03 再出现（added）；v3 快照保持原样 --"
api POST "/api/v1/windows/$WID/certificates/issue" '{"note":"恢复 r-03 后签发"}' >/dev/null
CV=$(api GET "/api/v1/windows/$WID/certificates" \
  | python3 -c "import sys,json;cs=json.load(sys.stdin);print(cs[-2]['id'],cs[-1]['id'])")
read -r V3_ID V4_ID <<< "$CV"
api GET "/api/v1/certificates/revision-report?old_certificate_id=$V3_ID&new_certificate_id=$V4_ID" \
  | python3 -c "import sys,json;d=json.load(sys.stdin)['readings_diff'];print('added:',[r['reading_id'] for r in d['added']],'removed:',[r['reading_id'] for r in d['removed']])"
api GET "/api/v1/certificates/$V3_ID" \
  | python3 -c "import sys,json;print('v3 快照读数（仍无 r-03）:',[r['reading_id'] for r in json.load(sys.stdin)['snapshot']['readings']])"

echo "-- 14.9 批量恢复：作废 r-40/r-50 后整批以更高修订号恢复（均 restored）--"
api POST "/api/v1/windows/$WID/readings/r-40/void" '{"revision":2,"reason":"误采"}' >/dev/null
api POST "/api/v1/windows/$WID/readings/r-50/void" '{"revision":2,"reason":"误采"}' >/dev/null
api POST "/api/v1/windows/$WID/readings/batch" '{
  "readings": [
    {"reading_id":"r-40","revision":3,"measured_at":"2026-10-01T00:40:00Z","temperature":20.0},
    {"reading_id":"r-50","revision":3,"measured_at":"2026-10-01T00:50:00Z","temperature":20.0}
  ]
}' | python3 -c "import sys,json;d=json.load(sys.stdin);print('effects:',[x['effect'] for x in d['results']],'reading_count:',d['current_evaluation']['reading_count'])"

echo "== 15. 窗口规则版本化修订（单独建窗口演示）=="
RW=$(api POST /api/v1/windows '{
  "name": "展柜C-规则修订",
  "start_at": "2026-10-01T00:00:00Z",
  "end_at":   "2026-10-01T00:10:00Z",
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 22.0,
  "max_continuous_exceed_seconds": 300,
  "max_sample_interval_seconds": 600
}')
RID=$(echo "$RW" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
for mt in 00 05 10; do
  api POST "/api/v1/windows/$RID/readings" \
    "{\"reading_id\":\"s-$mt\",\"revision\":1,\"measured_at\":\"2026-10-01T00:$mt:00Z\",\"temperature\":20.0}" >/dev/null
done
echo "当前规则修订号（初始规则为第 1 版）：$(echo "$RW" | python3 -c 'import sys,json;print(json.load(sys.stdin)["rule_revision"])')"

echo "-- 15.1 首签（规则第 1 版，结论 pass）--"
RC1=$(api POST "/api/v1/windows/$RID/certificates/issue" '{"note":"规则第1版"}')
echo "$RC1" | python3 -c "import sys,json;c=json.load(sys.stdin);print('证书',c['id'],'version',c['version'],c['verdict'],'规则版',c['snapshot']['window']['rule_revision'])"

echo "-- 15.2 提交第 2 版规则（温度上限 22 -> 19.5；20.0 全窗超限 600s > 300 -> fail），立即重算 --"
api POST "/api/v1/windows/$RID/rule-revisions" '{
  "revision": 2,
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 19.5,
  "max_continuous_exceed_seconds": 300,
  "max_sample_interval_seconds": 600,
  "reason": "换季：收窄允许温度带"
}' | python3 -c "import sys,json;d=json.load(sys.stdin);print('effect:',d['effect']);print('before:',d['rule_revision']['before']);print('after:',d['rule_revision']['after']);print('verdict:',d['current_evaluation']['verdict'])"

echo "-- 15.3 同号 + 同四项值 + 同原因重放 -> 幂等（不留新记录）--"
api POST "/api/v1/windows/$RID/rule-revisions" '{
  "revision": 2,
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 19.5,
  "max_continuous_exceed_seconds": 300,
  "max_sample_interval_seconds": 600,
  "reason": "换季：收窄允许温度带"
}' | python3 -c "import sys,json;print('effect:',json.load(sys.stdin)['effect'])"

echo "-- 15.4 拒绝路径：同号原因不同 409 / 旧号 409 / 跳号 409 / 下限大于上限 422 / 非正时长 422 / 空原因 422 / 窗口不存在 404 --"
api POST "/api/v1/windows/$RID/rule-revisions" '{"revision":2,"allowed_temp_min":18.0,"allowed_temp_max":19.5,"max_continuous_exceed_seconds":300,"max_sample_interval_seconds":600,"reason":"别的原因"}' || true
api POST "/api/v1/windows/$RID/rule-revisions" '{"revision":1,"allowed_temp_min":18.0,"allowed_temp_max":22.0,"max_continuous_exceed_seconds":300,"max_sample_interval_seconds":600,"reason":"旧号"}' || true
api POST "/api/v1/windows/$RID/rule-revisions" '{"revision":9,"allowed_temp_min":18.0,"allowed_temp_max":19.5,"max_continuous_exceed_seconds":300,"max_sample_interval_seconds":600,"reason":"跳号"}' || true
api POST "/api/v1/windows/$RID/rule-revisions" '{"revision":3,"allowed_temp_min":23.0,"allowed_temp_max":22.0,"max_continuous_exceed_seconds":300,"max_sample_interval_seconds":600,"reason":"区间倒置"}' || true
api POST "/api/v1/windows/$RID/rule-revisions" '{"revision":3,"allowed_temp_min":18.0,"allowed_temp_max":19.5,"max_continuous_exceed_seconds":300,"max_sample_interval_seconds":0,"reason":"非正时长"}' || true
api POST "/api/v1/windows/$RID/rule-revisions" '{"revision":3,"allowed_temp_min":18.0,"allowed_temp_max":19.5,"max_continuous_exceed_seconds":300,"max_sample_interval_seconds":600,"reason":"   "}' || true
api POST "/api/v1/windows/999/rule-revisions" '{"revision":2,"allowed_temp_min":18.0,"allowed_temp_max":19.5,"max_continuous_exceed_seconds":300,"max_sample_interval_seconds":600,"reason":"窗口不存在"}' || true

echo "-- 15.5 规则修订历史（第 1 版为初始规则，before 为 null）--"
api GET "/api/v1/windows/$RID/rule-revisions" | python3 -m json.tool

echo "-- 15.6 旧证书快照仍为第 1 版规则（22.0、pass）；用第 2 版规则再签发 --"
api GET "/api/v1/certificates/$(echo "$RC1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')" \
  | python3 -c "import sys,json;w=json.load(sys.stdin)['snapshot']['window'];print('旧证规则版:',w['rule_revision'],'上限:',w['allowed_temp_max'])"
RC2=$(api POST "/api/v1/windows/$RID/certificates/issue" '{"note":"规则第2版"}')
echo "$RC2" | python3 -c "import sys,json;c=json.load(sys.stdin);print('证书',c['id'],'version',c['version'],c['verdict'],'规则版',c['snapshot']['window']['rule_revision'])"

echo "-- 15.7 跨规则版本修订影响报告：分别依两证快照重建时间线 + 四项规则前后值 --"
OLD_RC=$(echo "$RC1" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
NEW_RC=$(echo "$RC2" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
api GET "/api/v1/certificates/revision-report?old_certificate_id=$OLD_RC&new_certificate_id=$NEW_RC" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('rules_diff:',d['rules_diff']['rule_revision_before'],'->',d['rules_diff']['rule_revision_after'],d['rules_diff']['changed_fields']);print('timeline_change_count:',d['timeline']['change_count']);print(d['summary']['conclusion_zh'])"

echo "== 15b. 仪器失准故障时段登记与撤回（单独建窗口演示）=="
FW=$(api POST /api/v1/windows '{
  "name": "展柜F-仪器故障",
  "start_at": "2026-10-01T00:00:00Z",
  "end_at":   "2026-10-01T01:00:00Z",
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 22.0,
  "max_continuous_exceed_seconds": 300,
  "max_sample_interval_seconds": 1800
}')
FID=$(echo "$FW" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
# 三条读数：[00:00]23 超限 -> [00:30]20 正常 -> [01:00]23（覆盖终止采样）
api POST "/api/v1/windows/$FID/readings" '{"reading_id":"a","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":23.0}' >/dev/null
api POST "/api/v1/windows/$FID/readings" '{"reading_id":"b","revision":1,"measured_at":"2026-10-01T00:30:00Z","temperature":20.0}' >/dev/null
api POST "/api/v1/windows/$FID/readings" '{"reading_id":"c","revision":1,"measured_at":"2026-10-01T01:00:00Z","temperature":23.0}' >/dev/null
echo "-- 15b.1 无故障时：[00:00,00:30) 超限 1800s > 300 -> fail --"
api GET "/api/v1/windows/$FID/evaluation" \
  | python3 -c "import sys,json;e=json.load(sys.stdin);print(e['verdict'],e['max_continuous_exceeded_seconds'])"

echo "-- 15b.2 登记故障 [00:10,00:20)：遮蔽段无论有无读数均未知，超限被切成 600s+600s --"
api POST "/api/v1/windows/$FID/fault-periods" '{
  "fault_id":"fault-01","start_at":"2026-10-01T00:10:00Z","end_at":"2026-10-01T00:20:00Z",
  "reason":"温感漂移：该时段读数不可信"}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);e=d['current_evaluation'];print(d['effect'],e['verdict'],'最长连续超限',e['max_continuous_exceeded_seconds'],'未知',[(u['start'][11:16],u['end'][11:16]) for u in e['unknown_segments']])"

echo "-- 15b.3 同标识同内容重放 -> 幂等；同标识改终点 -> 409；非法时段/空原因 -> 422 --"
api POST "/api/v1/windows/$FID/fault-periods" '{"fault_id":"fault-01","start_at":"2026-10-01T00:10:00Z","end_at":"2026-10-01T00:20:00Z","reason":"温感漂移：该时段读数不可信"}' \
  | python3 -c "import sys,json;print('effect =',json.load(sys.stdin)['effect'])"
api POST "/api/v1/windows/$FID/fault-periods" '{"fault_id":"fault-01","start_at":"2026-10-01T00:10:00Z","end_at":"2026-10-01T00:21:00Z","reason":"温感漂移：该时段读数不可信"}' || true
api POST "/api/v1/windows/$FID/fault-periods" '{"fault_id":"fault-x","start_at":"2026-10-01T00:20:00Z","end_at":"2026-10-01T00:10:00Z","reason":"倒置"}' || true
api POST "/api/v1/windows/$FID/fault-periods" '{"fault_id":"fault-x","start_at":"2026-10-01T00:59:00Z","end_at":"2026-10-01T01:01:00Z","reason":"越界"}' || true
api POST "/api/v1/windows/$FID/fault-periods" '{"fault_id":"fault-x","start_at":"2026-10-01T00:10:00Z","end_at":"2026-10-01T00:20:00Z","reason":"  "}' || true

echo "-- 15b.4 故障期间签发 v1：快照固化有效故障时段与遮蔽后结果（超限仍 >300 故 fail）--"
FC1=$(api POST "/api/v1/windows/$FID/certificates/issue" '{"note":"故障遮蔽中"}')
echo "$FC1" | python3 -c "import sys,json;c=json.load(sys.stdin);print('证书',c['id'],'version',c['version'],c['verdict'],'故障',[f['fault_id'] for f in c['snapshot']['fault_periods']])"

echo "-- 15b.5 撤回故障（非空原因）-> 立即恢复 fail；同因重放幂等、异因 409、未知标识 404 --"
api POST "/api/v1/windows/$FID/fault-periods/fault-01/withdrawal" '{"reason":"复核：仪器已校准恢复"}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['effect'],d['fault_period']['status'],d['current_evaluation']['verdict'])"
api POST "/api/v1/windows/$FID/fault-periods/fault-01/withdrawal" '{"reason":"复核：仪器已校准恢复"}' \
  | python3 -c "import sys,json;print('effect =',json.load(sys.stdin)['effect'])"
api POST "/api/v1/windows/$FID/fault-periods/fault-01/withdrawal" '{"reason":"换个原因"}' || true
api POST "/api/v1/windows/$FID/fault-periods/nope/withdrawal" '{"reason":"未知标识"}' || true
api POST "/api/v1/windows/$FID/fault-periods/fault-01/withdrawal" '{"reason":"   "}' || true

echo "-- 15b.6 撤回后签发 v2；跨证报告从各自快照还原故障时间线（遮蔽段 unknown -> exceeded）--"
FC2=$(api POST "/api/v1/windows/$FID/certificates/issue" '{"note":"撤回后重签"}')
F_OLD=$(echo "$FC1" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
F_NEW=$(echo "$FC2" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
api GET "/api/v1/certificates/revision-report?old_certificate_id=$F_OLD&new_certificate_id=$F_NEW" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('fault_removed:',d['fault_periods_diff']['removed_count']);print('timeline:',[(c['start'][11:16],c['before']['status'],'->',c['after']['status']) for c in d['timeline']['changes']]);print(d['summary']['verdict_before'],'->',d['summary']['verdict_after'])"

echo "-- 15b.7 已撤回标识不能再次登记 -> 409；列表仍保留撤回记录（active_only=true 为空）--"
api POST "/api/v1/windows/$FID/fault-periods" '{"fault_id":"fault-01","start_at":"2026-10-01T00:10:00Z","end_at":"2026-10-01T00:20:00Z","reason":"温感漂移：该时段读数不可信"}' || true
api GET "/api/v1/windows/$FID/fault-periods" | python3 -c "import sys,json;print([(f['fault_id'],f['status']) for f in json.load(sys.stdin)])"
api GET "/api/v1/windows/$FID/fault-periods?active_only=true" | python3 -c "import sys,json;print('active:',json.load(sys.stdin))"

echo "== 16. 已签发证书撤销（单独建窗口演示；撤销只改有效性，版本/结论/快照不动）=="
VW=$(api POST /api/v1/windows '{
  "name": "展柜G-证书撤销",
  "start_at": "2026-10-01T00:00:00Z",
  "end_at":   "2026-10-01T00:05:00Z",
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 22.0,
  "max_continuous_exceed_seconds": 300,
  "max_sample_interval_seconds": 120
}')
VID=$(echo "$VW" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
for m in 0 1 2 3 4 5; do
  api POST "/api/v1/windows/$VID/readings" \
    "{\"reading_id\":\"r-$m\",\"revision\":1,\"measured_at\":\"2026-10-01T00:0$m:00Z\",\"temperature\":20.0}" >/dev/null
done
echo "-- 16.1 尚无证书时查询当前可用证书 -> 409 --"
api GET "/api/v1/windows/$VID/certificates/current" || true

echo "-- 16.2 连续签发 v1、v2（v2 为当前可用证书）--"
VC1=$(api POST "/api/v1/windows/$VID/certificates/issue" '{"note":"v1"}')
VC2=$(api POST "/api/v1/windows/$VID/certificates/issue" '{"note":"v2"}')
V1=$(echo "$VC1" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
V2=$(echo "$VC2" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
api GET "/api/v1/windows/$VID/certificates/current" \
  | python3 -c "import sys,json;c=json.load(sys.stdin);print('当前可用: 证书',c['id'],'version',c['version'])"

echo "-- 16.3 撤销旧版 v1（非空原因）：不影响当前可用（仍是 v2）；v1 版本/结论/快照不变 --"
api POST "/api/v1/certificates/$V1/revocation" '{"reason":"签发依据有误：r-2 为误采"}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('effect:',d['effect']);print('v1 revoked:',d['certificate']['revoked'],'version/verdict:',d['certificate']['version'],d['certificate']['verdict'],'快照读数:',len(d['certificate']['snapshot']['readings']));print('revocation:',d['certificate']['revocation']);print('current_certificate:',d['current_certificate']['id'],'v'+str(d['current_certificate']['version']))"

echo "-- 16.4 相同原因重复撤销 v1 -> 幂等（返回原记录，撤销时间不变，不产生新事件）--"
api POST "/api/v1/certificates/$V1/revocation" '{"reason":"签发依据有误：r-2 为误采"}' \
  | python3 -c "import sys,json;print('effect:',json.load(sys.stdin)['effect'])"

echo "-- 16.5 拒绝路径：不同原因 409 / 空白原因 422 / 证书不存在 404 --"
api POST "/api/v1/certificates/$V1/revocation" '{"reason":"另一个原因"}' || true
api POST "/api/v1/certificates/$V1/revocation" '{"reason":"   "}' || true
api POST "/api/v1/certificates/99999/revocation" '{"reason":"x"}' || true

echo "-- 16.6 撤销最新版 v2：不自动退回旧版（v1 仍有效但不是当前可用），当前可用为空 --"
api POST "/api/v1/certificates/$V2/revocation" '{"reason":"v2 依据有误"}' >/dev/null
api GET "/api/v1/windows/$VID/certificates/current" || true
api GET "/api/v1/windows/$VID" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('latest_certificate_version:',d['latest_certificate_version'],'current_certificate_id:',d['current_certificate_id'])"

echo "-- 16.7 历史仍可读并标示撤销状态：证书列表 + 按主键获取 --"
api GET "/api/v1/windows/$VID/certificates" \
  | python3 -c "import sys,json;[print('v'+str(c['version']),c['verdict'],'revoked=',c['revoked'],'reason=',c['revocation']['reason'] if c['revocation'] else None) for c in json.load(sys.stdin)]"

echo "-- 16.8 两证修订影响报告仍可生成（只依快照），引用中带 revoked/revocation --"
api GET "/api/v1/certificates/revision-report?old_certificate_id=$V1&new_certificate_id=$V2" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('old/new revoked:',d['old_certificate']['revoked'],d['new_certificate']['revoked']);print(d['summary']['verdict_before'],'->',d['summary']['verdict_after'])"

echo "-- 16.9 再次签发：版本沿用递增（v3），成为当前可用证书 --"
VC3=$(api POST "/api/v1/windows/$VID/certificates/issue" '{"note":"v3 重新签发"}')
echo "$VC3" | python3 -c "import sys,json;c=json.load(sys.stdin);print('新证书',c['id'],'version',c['version'],'revoked',c['revoked'])"
api GET "/api/v1/windows/$VID/certificates/current" \
  | python3 -c "import sys,json;c=json.load(sys.stdin);print('当前可用: 证书',c['id'],'version',c['version'])"

echo "== 17. 展柜移交放行单（非空单号 + 非空窗口标识列表申请放行）=="
# 两个满覆盖合格窗口（5 分钟、间隔 60s、温度 20.0）
H1=$(api POST /api/v1/windows '{
  "name": "展柜H1-移交", "start_at": "2026-10-01T00:00:00Z", "end_at": "2026-10-01T00:05:00Z",
  "allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
  "max_continuous_exceed_seconds": 300, "max_sample_interval_seconds": 120
}' | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
H2=$(api POST /api/v1/windows '{
  "name": "展柜H2-移交", "start_at": "2026-10-01T00:00:00Z", "end_at": "2026-10-01T00:05:00Z",
  "allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
  "max_continuous_exceed_seconds": 300, "max_sample_interval_seconds": 120
}' | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
for W in $H1 $H2; do
  for m in 0 1 2 3 4 5; do
    api POST "/api/v1/windows/$W/readings" \
      "{\"reading_id\":\"r-$m\",\"revision\":1,\"measured_at\":\"2026-10-01T00:0$m:00Z\",\"temperature\":20.0}" >/dev/null
  done
done
HC1=$(api POST "/api/v1/windows/$H1/certificates/issue" '{"note":"移交前签发"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
HC2=$(api POST "/api/v1/windows/$H2/certificates/issue" '{"note":"移交前签发"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
# 移交单号全局唯一：用本 run 的窗口号构造，保证脚本可重复运行
HNO="HJ-2026-$H1$H2"

echo "-- 17.1 申请放行：每个窗口都有当前可用证书且结论合格 -> 一次登记，固化证书标识/版本/结论 --"
api POST /api/v1/handover-orders "{\"handover_no\":\"$HNO\",\"window_ids\":[$H1,$H2]}" \
  | python3 -m json.tool

echo "-- 17.2 同一单号按窗口集合（不计顺序）重放 -> 幂等返回原单及当前状态 --"
api POST /api/v1/handover-orders "{\"handover_no\":\"$HNO\",\"window_ids\":[$H2,$H1]}" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('effect:',d['effect'],'status:',d['order']['status'],'created_at 不变:',d['order']['created_at'])"

echo "-- 17.3 拒绝路径：空列表 422 / 重复标识 422 / 空白单号 422 / 窗口不存在 404 / 同号不同集合 409 --"
api POST /api/v1/handover-orders '{"handover_no":"HJ-X1","window_ids":[]}' || true
api POST /api/v1/handover-orders "{\"handover_no\":\"HJ-X2\",\"window_ids\":[$H1,$H1]}" || true
api POST /api/v1/handover-orders "{\"handover_no\":\"   \",\"window_ids\":[$H1]}" || true
api POST /api/v1/handover-orders '{"handover_no":"HJ-X3","window_ids":[99999]}' || true
api POST /api/v1/handover-orders "{\"handover_no\":\"$HNO\",\"window_ids\":[$H1]}" || true

echo "-- 17.4 缺当前可用证书或结论非合格 -> 409，整单不留记录（单号可再用）--"
# 无证书窗口
H3=$(api POST /api/v1/windows '{
  "name": "展柜H3-无证书", "start_at": "2026-10-01T00:00:00Z", "end_at": "2026-10-01T00:05:00Z",
  "allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
  "max_continuous_exceed_seconds": 300, "max_sample_interval_seconds": 120
}' | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
api POST /api/v1/handover-orders "{\"handover_no\":\"HJ-X4\",\"window_ids\":[$H3]}" || true
echo "整单不留记录（HJ-X4 查询应为 404）："
api GET /api/v1/handover-orders/HJ-X4 || true

echo "-- 17.5 撤销被引用证书后查询：登记明细保留，整单标为失效 --"
api POST "/api/v1/certificates/$HC1/revocation" '{"reason":"签发依据有误：r-2 为误采"}' >/dev/null
api GET /api/v1/handover-orders/$HNO | python3 -m json.tool

echo "-- 17.6 失效后同集合重放仍返回原单（当前状态=失效）；后续再签发不恢复原单 --"
api POST /api/v1/handover-orders "{\"handover_no\":\"$HNO\",\"window_ids\":[$H1,$H2]}" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('effect:',d['effect'],'status:',d['order']['status'],d['order']['status_label_zh'])"
api POST "/api/v1/windows/$H1/certificates/issue" '{"note":"撤销后重签"}' >/dev/null
api GET /api/v1/handover-orders/$HNO \
  | python3 -c "import sys,json;o=json.load(sys.stdin);print('再签发后 status:',o['status']);print('明细仍固化:',[(i['certificate_id'],i['certificate_version'],i['verdict']) for i in o['items']])"

echo "== 18. 跨审定窗口连续温度暴露复核（按时间排列的当前可用证书 + 正数全程超限上限）=="
# 三个首尾相接、温度范围一致的窗口：02:00-02:10 / 02:10-02:20 / 02:20-02:30
mk_cw_window() { # idx start end
  api POST /api/v1/windows "{
    \"name\":\"跨窗复核窗口-$1\",\"start_at\":\"2026-10-01T$2:00Z\",\"end_at\":\"2026-10-01T$3:00Z\",
    \"allowed_temp_min\":18.0,\"allowed_temp_max\":22.0,
    \"max_continuous_exceed_seconds\":300,\"max_sample_interval_seconds\":600
  }" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])"
}
CW1=$(mk_cw_window 1 02:00 02:10)
CW2=$(mk_cw_window 2 02:10 02:20)
CW3=$(mk_cw_window 3 02:20 02:30)
# CW1 末段 [02:05,02:10) 超限 300s；CW2 首段 [02:10,02:15) 超限 300s；CW3 全正常
post_cw() { api POST "/api/v1/windows/$1/readings" "{\"reading_id\":\"$2\",\"revision\":1,\"measured_at\":\"2026-10-01T$3:00Z\",\"temperature\":$4}" >/dev/null; }
post_cw $CW1 a 02:00 20.0; post_cw $CW1 b 02:05 24.0; post_cw $CW1 c 02:10 20.0
post_cw $CW2 d 02:10 24.0; post_cw $CW2 e 02:15 20.0; post_cw $CW2 f 02:20 20.0
post_cw $CW3 g 02:20 20.0; post_cw $CW3 h 02:25 20.0; post_cw $CW3 i 02:30 20.0
CC1=$(api POST "/api/v1/windows/$CW1/certificates/issue" '{"note":"跨窗复核-窗1"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
CC2=$(api POST "/api/v1/windows/$CW2/certificates/issue" '{"note":"跨窗复核-窗2"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
CC3=$(api POST "/api/v1/windows/$CW3/certificates/issue" '{"note":"跨窗复核-窗3"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

echo "-- 18.1 复核（上限 500s）：两窗各 300s 在边界连续累计为 600s > 500s -> 不合格 --"
api POST /api/v1/cross-window-reviews "{\"certificate_ids\":[$CC1,$CC2,$CC3],\"max_continuous_exceed_seconds\":500}" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('verdict:',d['verdict'],d['verdict_label_zh']);print('最长连续超限:',d['max_continuous_exceeded_seconds'],'s；未知总时长:',d['unknown_seconds'],'s');print('分段:',[(s['status'],s['duration_seconds'],s['source']['certificate_id']) for s in d['segments']]);print(d['conclusion_zh'])"

echo "-- 18.2 恰好等于上限（600s）不算严格超过且无未知 -> 合格 --"
api POST /api/v1/cross-window-reviews "{\"certificate_ids\":[$CC1,$CC2,$CC3],\"max_continuous_exceed_seconds\":600}" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('verdict:',d['verdict'],d['verdict_label_zh'],'最长连续超限:',d['max_continuous_exceeded_seconds'])"

echo "-- 18.3 拒绝路径：重复标识/不足两张/非正上限 422；不存在 404；已撤销或非最新版 409；窗口不接续/温度范围不一致/顺序错误 422 --"
api POST /api/v1/cross-window-reviews "{\"certificate_ids\":[$CC1,$CC1,$CC3],\"max_continuous_exceed_seconds\":500}" || true
api POST /api/v1/cross-window-reviews "{\"certificate_ids\":[$CC1],\"max_continuous_exceed_seconds\":500}" || true
api POST /api/v1/cross-window-reviews "{\"certificate_ids\":[$CC1,$CC2],\"max_continuous_exceed_seconds\":0}" || true
api POST /api/v1/cross-window-reviews "{\"certificate_ids\":[$CC1,99999],\"max_continuous_exceed_seconds\":500}" || true
api POST /api/v1/cross-window-reviews "{\"certificate_ids\":[$CC2,$CC1,$CC3],\"max_continuous_exceed_seconds\":500}" || true
api POST /api/v1/certificates/$CC2/revocation '{"reason":"签发依据有误：读数 d 不可信"}' >/dev/null
api POST /api/v1/cross-window-reviews "{\"certificate_ids\":[$CC1,$CC2,$CC3],\"max_continuous_exceed_seconds\":500}" || true
# 撤销后窗2重签 v2 恢复当前可用；旧版 CC2 仍为已撤销不可用
api POST "/api/v1/windows/$CW2/certificates/issue" '{"note":"撤销后重签 v2"}' >/dev/null
api POST /api/v1/cross-window-reviews "{\"certificate_ids\":[$CC1,$CC2,$CC3],\"max_continuous_exceed_seconds\":500}" || true

echo "== 19. 证书核验包（馆外接收方；可携带、可离线核验；不改原证书与快照）=="
# 单独建一个窗口并签发证书，演示出包/核验/被替代/撤销/本地密钥轮换
PW=$(api POST /api/v1/windows '{
  "name": "展柜P-核验包",
  "start_at": "2026-10-01T00:00:00Z",
  "end_at":   "2026-10-01T00:10:00Z",
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 22.0,
  "max_continuous_exceed_seconds": 300,
  "max_sample_interval_seconds": 120
}')
PID=$(echo "$PW" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
for m in 0 1 2 3 4 5 6 7 8 9; do
  api POST "/api/v1/windows/$PID/readings" \
    "{\"reading_id\":\"r-0$m\",\"revision\":1,\"measured_at\":\"2026-10-01T00:0$m:00Z\",\"temperature\":20.0}" >/dev/null
done
api POST "/api/v1/windows/$PID/readings" '{"reading_id":"r-10","revision":1,"measured_at":"2026-10-01T00:10:00Z","temperature":20.0}' >/dev/null
PC1=$(api POST "/api/v1/windows/$PID/certificates/issue" '{"note":"核验包-首签"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

echo "-- 19.1 出具核验包（证书标识/版本/结论/签发时间/快照 SHA-256 摘要/密钥标识/Ed25519 签名）--"
BUNDLE=$(api POST "/api/v1/certificates/$PC1/verification-package")
echo "$BUNDLE" | python3 -m json.tool

echo "-- 19.2 接收方提交核验：签名可信、快照可信、为所属窗口最新未撤销版本（当前可用）--"
api POST /api/v1/verification-packages/verify "$BUNDLE" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('签名可信:',d['signature_check']['trusted'],d['signature_check']['key_status']);print('快照可信:',d['snapshot_check']['trusted']);print('当前可用:',d['current']['is_current'],d['certificate']['state']);print(d['conclusion_zh'])"

echo "-- 19.3 同一证书重复出包内容确定（Ed25519 确定性签名，两次完全一致）--"
BUNDLE2=$(api POST "/api/v1/certificates/$PC1/verification-package")
[ "$BUNDLE" = "$BUNDLE2" ] && echo "两次出包完全一致"

echo "-- 19.4 字段篡改 -> 422（签名核验失败）；未知密钥 -> 422 --"
echo "$BUNDLE" | python3 -c "import sys,json;p=json.load(sys.stdin);p['verdict']='fail';print(json.dumps(p,ensure_ascii=False))" \
  | { read -r TAMPERED; api POST /api/v1/verification-packages/verify "$TAMPERED"; } || true
echo "$BUNDLE" | python3 -c "import sys,json;p=json.load(sys.stdin);p['key_id']='key-unknown-0000000000000000';print(json.dumps(p,ensure_ascii=False))" \
  | { read -r TAMPERED; api POST /api/v1/verification-packages/verify "$TAMPERED"; } || true

echo "-- 19.5 查看签名密钥（仅公钥与元数据；服务重启后沿用库内同一把密钥）--"
api GET /api/v1/signing-keys \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('active:',d['active']['key_id'],d['active']['algorithm']);print('保留密钥数:',len(d['keys']))"

echo "-- 19.6 本地轮换签名密钥：新包用新密钥，旧包凭保留的历史公钥仍可信（retired）--"
api POST /api/v1/signing-keys/rotate '{"reason":"年度密钥轮换：2026Q4"}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('effect:',d['effect'],'新 active:',d['active']['key_id']);print('全部密钥状态:',[(k['key_id'][:52],k['status']) for k in d['keys']])"
echo "旧包（轮换前出具）仍可信："
api POST /api/v1/verification-packages/verify "$BUNDLE" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('签名可信:',d['signature_check']['trusted'],'密钥状态:',d['signature_check']['key_status'])"
BUNDLENEW=$(api POST "/api/v1/certificates/$PC1/verification-package")
echo "新包使用新密钥："
api POST /api/v1/verification-packages/verify "$BUNDLENEW" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('签名可信:',d['signature_check']['trusted'],'密钥状态:',d['signature_check']['key_status'],'当前可用:',d['current']['is_current'])"
echo "空轮换原因 -> 422："
api POST /api/v1/signing-keys/rotate '{"reason":"   "}' || true

echo "-- 19.7 签发 v2：v1 的历史核验包仍可信，但已被新版本替代（superseded，非当前可用）--"
api POST "/api/v1/windows/$PID/certificates/issue" '{"note":"v2"}' >/dev/null
api POST /api/v1/verification-packages/verify "$BUNDLE" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);c=d['certificate'];print('包整体可信:',d['package_trusted'],'当前可用:',c['is_current'],c['state']);print(c['not_current_reason'])"

echo "-- 19.8 撤销 v2：v1 仍 superseded（最新版也已撤销，窗口无当前可用证书）--"
PC2=$(api GET "/api/v1/windows/$PID/certificates" | python3 -c "import sys,json;print(json.load(sys.stdin)[-1]['id'])")
api POST "/api/v1/certificates/$PC2/revocation" '{"reason":"v2 依据有误"}' >/dev/null
api POST /api/v1/verification-packages/verify "$BUNDLE" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('state:',d['certificate']['state'],'窗口有当前可用证书:',d['current']['window_has_current_certificate'])"

echo "-- 19.9 已撤销证书也可出包（可信历史包，非当前可用）；出包/核验均不改业务状态 --"
api POST "/api/v1/certificates/$PC1/verification-package" > /tmp/pkg-v1.json
BASE_URL="${BASE_URL:-http://localhost:8000}" python3 - <<'PY'
import json, os, urllib.request
p = json.load(open("/tmp/pkg-v1.json"))
req = urllib.request.Request(
    os.environ["BASE_URL"] + "/api/v1/verification-packages/verify",
    data=json.dumps(p).encode(), headers={"Content-Type": "application/json"}, method="POST")
d = json.loads(urllib.request.urlopen(req).read())
print("v1 核验包: 包可信 =", d["package_trusted"], " state =", d["certificate"]["state"])
PY

echo "-- 19.10 证书不存在出包 -> 404；包格式不受支持 -> 422（无对应证书核验 -> 404）--"
api POST /api/v1/certificates/99999/verification-package || true
echo "$BUNDLE" | python3 -c "import sys,json;p=json.load(sys.stdin);p['format']='bad/format';print(json.dumps(p,ensure_ascii=False))" \
  | { read -r BADFMT; api POST /api/v1/verification-packages/verify "$BADFMT"; } || true
