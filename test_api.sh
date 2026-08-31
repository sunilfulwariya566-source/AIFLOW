#!/usr/bin/env bash
# End-to-end API tests against a running server. Usage: bash test_api.sh
H=${H:-http://localhost:8000}
KEY=${KEY:-aiflow-dev-key}
P=0; F=0

# The suite now makes ~90 calls; give it headroom over the default 120/min so a
# back-to-back re-run can't trip the limiter and report phantom failures.
if [ -z "${SKIP_RL_CHECK:-}" ]; then
  RL=$(curl -s $H/api/health | python3 -c 'import sys,json;print(json.load(sys.stdin)["rate_limit_per_min"])' 2>/dev/null || echo 120)
  if [ "${RL:-120}" -lt 400 ]; then
    echo "note: server rate limit is ${RL}/min — start it with AIFLOW_RATE_LIMIT=1000 to"
    echo "      run this suite repeatedly without hitting 429s."
    echo ""
  fi
fi

chk(){ if [ "$2" = "$3" ]; then echo "  ok   $1"; P=$((P+1));
       else echo "  FAIL $1 (got '$2' want '$3')"; F=$((F+1)); fi; }
# authenticated JSON request -> body
req(){ local m=$1 p=$2 d=${3:-}; local k=${KEYOVER:-$KEY}
  if [ -n "$d" ]; then curl -s -X "$m" "$H$p" -H "X-API-Key: $k" -H 'Content-Type: application/json' -d "$d"
  else curl -s -X "$m" "$H$p" -H "X-API-Key: $k"; fi; }
# authenticated JSON request -> status code
sc(){ local m=$1 p=$2 d=${3:-}; local k=${KEYOVER:-$KEY}
  if [ -n "$d" ]; then curl -s -o /dev/null -w '%{http_code}' -X "$m" "$H$p" -H "X-API-Key: $k" -H 'Content-Type: application/json' -d "$d"
  else curl -s -o /dev/null -w '%{http_code}' -X "$m" "$H$p" -H "X-API-Key: $k"; fi; }
jq_(){ python3 -c "import sys,json
try:
    d=json.load(sys.stdin)
    for k in '''$1'''.split('.'):
        d = d[int(k)] if k.lstrip('-').isdigit() else d[k]
    print(d)
except Exception as e: print('ERR:'+type(e).__name__)"; }

echo "auth"
chk "health is public"       "$(curl -s -o /dev/null -w '%{http_code}' $H/api/health)" 200
chk "no key -> 401"          "$(curl -s -o /dev/null -w '%{http_code}' $H/api/workflows)" 401
chk "bad key -> 403"         "$(KEYOVER=nope sc GET /api/workflows)" 403
chk "good key -> 200"        "$(sc GET /api/workflows)" 200

echo "scopes"
LIMITED=$(req POST /api/keys '{"label":"ro","scopes":["run"]}' | jq_ key)
chk "run scope can run"      "$(KEYOVER=$LIMITED sc POST /api/workflows/doc-summarize-chain/run '{"payload":{"document":"hello"}}')" 200
chk "run scope cannot write" "$(KEYOVER=$LIMITED sc PUT /api/workflows/x '{"name":"x","nodes":[]}')" 403
chk "run scope cannot admin" "$(KEYOVER=$LIMITED sc GET /api/keys)" 403
req DELETE "/api/keys/$LIMITED" >/dev/null

echo "webhooks (HMAC)"
BODY='{"document":"Ship SSO by Nov 15."}'
SIG=$(python3 -c "import hmac,hashlib,os
print(hmac.new(os.environ.get('AIFLOW_WEBHOOK_SECRET','dev-webhook-secret').encode(),
      '''$BODY'''.encode(),hashlib.sha256).hexdigest())")
hook(){ curl -s -o /dev/null -w '%{http_code}' -X POST "$H/hooks/$1" \
        -H 'Content-Type: application/json' ${2:+-H "X-Signature: $2"} -d "$BODY"; }
chk "unsigned -> 401"        "$(hook doc-summarize-chain)" 401
chk "bad signature -> 403"   "$(hook doc-summarize-chain deadbeef)" 403
chk "valid signature -> 200" "$(hook doc-summarize-chain $SIG)" 200
chk "unknown workflow -> 404" "$(hook ghost $SIG)" 404

echo "runs"
R=$(req POST /api/workflows/batch-review-miner/run \
   '{"payload":{"reviews":["crashed twice, awful","love it","billed twice broken"]}}')
chk "batch run succeeds"     "$(echo "$R" | jq_ status)" success
chk "negatives filtered"     "$(echo "$R" | jq_ outputs.report.counts.negative)" 2
chk "cost tracked"           "$(echo "$R" | jq_ usage.cost_usd)" 0.0
chk "tokens tracked"         "$(echo "$R" | python3 -c 'import sys,json;print(json.load(sys.stdin)["usage"]["tokens_in"]>0)')" True

echo "async jobs"
JOB=$(req POST /api/workflows/doc-summarize-chain/run \
     '{"payload":{"document":"async test"},"async_mode":true}' | jq_ job_id)
sleep 2
chk "async job completes"    "$(req GET /api/jobs/$JOB | jq_ state)" done

echo "approvals + resume"
AR=$(req POST /api/workflows/content-pipeline/run '{"payload":{"topic":"testing approvals"}}')
chk "run pauses at gate"     "$(echo "$AR" | jq_ status)" paused
AID=$(req GET '/api/approvals?status=pending' | jq_ 0.id)
RES=$(req POST /api/approvals/$AID '{"approved":true,"comment":"ship it"}')
chk "approve resumes run"    "$(echo "$RES" | jq_ run.status)" success
chk "gate recorded approval" "$(echo "$RES" | jq_ run.outputs.bundle.approved)" True
chk "resume kept run id"     "$(echo "$RES" | jq_ run.run_id)" "$(echo "$AR" | jq_ run_id)"

echo "validation + versions"
chk "bad workflow invalid"   "$(req POST /api/validate '{"name":"b","nodes":[{"id":"a","type":"weird","params":{}}]}' | jq_ valid)" False
chk "cycle invalid"          "$(req POST /api/validate '{"name":"c","nodes":[{"id":"a","type":"python","params":{"expr":"b"}},{"id":"b","type":"python","params":{"expr":"a"}}]}' | jq_ valid)" False
req PUT /api/workflows/_apitest '{"name":"_apitest","description":"one","nodes":[]}' >/dev/null
req PUT /api/workflows/_apitest '{"name":"_apitest","description":"two","nodes":[]}' >/dev/null
chk "version bumped"         "$(req GET /api/workflows/_apitest | jq_ version)" 2
req POST /api/workflows/_apitest/rollback/1 >/dev/null
chk "rollback works"         "$(req GET /api/workflows/_apitest | jq_ description)" one
req DELETE /api/workflows/_apitest >/dev/null

echo "schedules"
SID=$(req POST /api/schedules '{"workflow":"doc-summarize-chain","every_seconds":5,"payload":{"document":"scheduled"}}' | jq_ id)
chk "schedule created"       "$(req GET /api/schedules | jq_ 0.workflow)" doc-summarize-chain
req DELETE /api/schedules/$SID >/dev/null
chk "schedule deleted"       "$(req GET /api/schedules | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')" 0

echo "conditional edges + sub-workflows"
SR=$(req POST /api/workflows/smart-router/run '{"payload":{"message":"URGENT: production is down, 500 errors"}}')
chk "smart-router runs"      "$(echo "$SR" | jq_ status)" success
chk "urgent branch taken"    "$(echo "$SR" | jq_ outputs.result.priority)" high
chk "normal branch skipped"  "$(echo "$SR" | python3 -c 'import sys,json;print("normal" in json.load(sys.stdin)["skipped"])')" True
SR2=$(req POST /api/workflows/smart-router/run '{"payload":{"message":"hi, how do I change my avatar?"}}')
chk "normal branch taken"    "$(echo "$SR2" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["outputs"]["result"]["reply"] is not None)')" True
chk "urgent branch skipped"  "$(echo "$SR2" | python3 -c 'import sys,json;print("urgent" in json.load(sys.stdin)["skipped"])')" True
chk "sub-workflow nested"    "$(echo "$SR" | python3 -c 'import sys,json;print(json.load(sys.stdin)["context"]["triage"]["status"])')" success
chk "self-call rejected"     "$(req POST /api/validate '{"name":"selfie","nodes":[{"id":"a","type":"workflow","params":{"workflow":"selfie"}}]}' | jq_ valid)" False
chk "missing sub rejected"   "$(req POST /api/validate '{"name":"z","nodes":[{"id":"a","type":"workflow","params":{"workflow":"ghost-wf"}}]}' | jq_ valid)" False
chk "bad when rejected"      "$(req POST /api/validate '{"name":"z","nodes":[{"id":"a","type":"template","params":{"text":"x"},"when":"not valid python ="}]}' | jq_ valid)" False
chk "good when accepted"     "$(req POST /api/validate '{"name":"z","nodes":[{"id":"a","type":"input","params":{"key":"k"}},{"id":"b","type":"template","params":{"text":"x"},"when":"a > 1"}]}' | jq_ valid)" True

echo "budget caps"
BR=$(req POST /api/workflows/batch-review-miner/run '{"payload":{"reviews":["a","b","c","d","e"]},"budget":{"max_llm_calls":2}}')
chk "budget stops the run"   "$(echo "$BR" | jq_ status)" budget_exceeded
chk "breach explained"       "$(echo "$BR" | python3 -c 'import sys,json;print("exceeds budget" in (json.load(sys.stdin)["error"] or ""))')" True
chk "budget echoed"          "$(echo "$BR" | jq_ budget.max_llm_calls)" 2
chk "no budget runs free"    "$(req POST /api/workflows/batch-review-miner/run '{"payload":{"reviews":["a","b","c","d","e"]}}' | jq_ status)" success
chk "bad budget key warned"  "$(req POST /api/validate '{"name":"z","nodes":[],"budget":{"nonsense":1}}' | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["warnings"])>0)')" True

echo "alerting"
AL=$(req POST /api/alerts '{"name":"api-test","metric":"error_count","op":">=","threshold":0,"window_runs":5,"cooldown_s":0}')
AL_ID=$(echo "$AL" | jq_ id)
chk "alert created"          "$(echo "$AL" | jq_ metric)" error_count
chk "alert listed"           "$(req GET /api/alerts | python3 -c 'import sys,json;print(any(a["name"]=="api-test" for a in json.load(sys.stdin)))')" True
chk "evaluate fires"         "$(req POST /api/alerts/evaluate | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["fired"])>0)')" True
chk "event feed populated"   "$(req GET '/api/alerts/events?limit=5' | python3 -c 'import sys,json;print(len(json.load(sys.stdin))>0)')" True
chk "metrics endpoint"       "$(req GET '/api/alerts/metrics?window=10' | python3 -c 'import sys,json;print("failure_rate" in json.load(sys.stdin))')" True
chk "bad metric rejected"    "$(sc POST /api/alerts '{"name":"x","metric":"nonsense","threshold":1}')" 400
chk "bad op rejected"        "$(sc POST /api/alerts '{"name":"x","metric":"p95_ms","op":"~","threshold":1}')" 400
chk "webhook needs target"   "$(sc POST /api/alerts '{"name":"x","metric":"p95_ms","threshold":1,"channel":"webhook"}')" 400
chk "unknown workflow 404"   "$(sc POST /api/alerts '{"name":"x","metric":"p95_ms","threshold":1,"workflow":"ghost-wf"}')" 404
req DELETE /api/alerts/$AL_ID >/dev/null
chk "alert deleted"          "$(req GET /api/alerts | python3 -c 'import sys,json;print(any(a["name"]=="api-test" for a in json.load(sys.stdin)))')" False

echo "import / export"
EX=$(req GET '/api/export?names=smart-router')
chk "export marker"          "$(echo "$EX" | jq_ aiflow_bundle)" 1
chk "deps included"          "$(echo "$EX" | jq_ count)" 2
chk "dep ordered first"      "$(echo "$EX" | jq_ workflows.0.name)" lib-classify
chk "export all"             "$(req GET /api/export | python3 -c 'import sys,json;print(json.load(sys.stdin)["count"]>=8)')" True
python3 -c "
import json,sys
b=json.load(open('/dev/stdin'))
json.dump({'bundle':b,'mode':'rename','dry_run':True},open('/tmp/imp1.json','w'))" <<<"$EX"
chk "dry run predicts"       "$(curl -s -X POST $H/api/import -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d @/tmp/imp1.json | jq_ would_import)" 2
chk "dry run wrote nothing"  "$(sc GET /api/workflows/smart-router-imported)" 404
python3 -c "
import json
b=json.load(open('/dev/stdin'))
json.dump({'bundle':b,'mode':'rename'},open('/tmp/imp2.json','w'))" <<<"$EX"
IMP=$(curl -s -X POST $H/api/import -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d @/tmp/imp2.json)
chk "import writes"          "$(echo "$IMP" | jq_ imported)" 2
chk "renamed copy exists"    "$(sc GET /api/workflows/smart-router-imported)" 200
chk "sub-ref rewired"        "$(req GET /api/workflows/smart-router-imported | python3 -c 'import sys,json;d=json.load(sys.stdin);print([n["params"]["workflow"] for n in d["nodes"] if n["type"]=="workflow"][0])')" lib-classify-imported
chk "imported copy runs"     "$(req POST /api/workflows/smart-router-imported/run '{"payload":{"message":"hello there"}}' | jq_ status)" success
req DELETE /api/workflows/smart-router-imported >/dev/null
req DELETE /api/workflows/lib-classify-imported >/dev/null
chk "bad bundle 400"         "$(sc POST /api/import '{"bundle":{"nope":1}}')" 400
chk "bad mode 400"           "$(sc POST /api/import '{"bundle":{"aiflow_bundle":1,"workflows":[{"name":"q","nodes":[]}]},"mode":"weird"}')" 400

echo "run comparison"
CA=$(req POST /api/workflows/smart-router/run '{"payload":{"message":"URGENT: everything is broken"}}' | jq_ run_id)
CB=$(req POST /api/workflows/smart-router/run '{"payload":{"message":"how do I change my avatar"}}' | jq_ run_id)
CMP=$(req GET "/api/runs/compare?a=$CA&b=$CB")
chk "compare same workflow"  "$(echo "$CMP" | jq_ same_workflow)" True
chk "compare finds changes"  "$(echo "$CMP" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["changed_nodes"])>0)')" True
chk "compare tracks skips"   "$(echo "$CMP" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["skipped_a"]!=d["skipped_b"])')" True
chk "self-compare identical" "$(req GET "/api/runs/compare?a=$CA&b=$CA" | jq_ identical)" True
chk "missing run 404"        "$(sc GET '/api/runs/compare?a=ghost&b=ghost2')" 404
chk "run detail still works" "$(req GET /api/runs/$CA | jq_ run_id)" "$CA"

echo "parallel + timeout"
req PUT /api/workflows/_par '{"name":"_par","nodes":[
  {"id":"src","type":"input","params":{"key":"x"}},
  {"id":"a","type":"llm","params":{"prompt":"Summarize: a {{src}}"}},
  {"id":"b","type":"llm","params":{"prompt":"Summarize: b {{src}}"}},
  {"id":"j","type":"output","params":{"value":{"a":"{{a}}","b":"{{b}}"}}}]}' >/dev/null
req POST /api/workflows/_par/run '{"payload":{"x":"d"}}' > /tmp/ser.json
req POST /api/workflows/_par/run '{"payload":{"x":"d"},"parallel":4}' > /tmp/par.json
chk "parallel run succeeds"   "$(jq_ status < /tmp/par.json)" success
chk "parallel output matches" "$(python3 -c 'import json;print(json.load(open("/tmp/ser.json"))["outputs"]==json.load(open("/tmp/par.json"))["outputs"])')" True
chk "parallel logs all nodes" "$(python3 -c 'import json;print(len(json.load(open("/tmp/par.json"))["logs"]))')" 4
chk "parallel saved on wf"    "$(req PUT /api/workflows/_par '{"name":"_par","parallel":4,"nodes":[{"id":"a","type":"template","params":{"text":"x"}}]}' | jq_ parallel)" 4
chk "parallel range checked"  "$(req POST /api/validate '{"name":"_par","parallel":99,"nodes":[]}' | jq_ valid)" False
chk "bad timeout rejected"    "$(req POST /api/validate '{"name":"z","nodes":[{"id":"a","type":"template","params":{},"timeout":-5}]}' | jq_ valid)" False
chk "good timeout accepted"   "$(req POST /api/validate '{"name":"z","nodes":[{"id":"a","type":"template","params":{},"timeout":30}]}' | jq_ valid)" True
req DELETE /api/workflows/_par >/dev/null
echo "batch runs + version diff"
BR=$(req POST /api/workflows/smart-router/batch '{"payloads":[{"message":"URGENT: down"},{"message":"how do I reset"},{"message":"love it"}],"concurrency":3}')
chk "batch runs all payloads"  "$(echo "$BR" | jq_ total)" 3
chk "batch all succeed"        "$(echo "$BR" | jq_ succeeded)" 3
chk "batch reports cost"       "$(echo "$BR" | python3 -c 'import sys,json;print("cost_usd" in json.load(sys.stdin))')" True
chk "batch results ordered"    "$(echo "$BR" | python3 -c 'import sys,json;r=json.load(sys.stdin)["results"];print([x["index"] for x in r]==[0,1,2])')" True
chk "batch rows have run ids"  "$(echo "$BR" | python3 -c 'import sys,json;print(all(x["run_id"] for x in json.load(sys.stdin)["results"]))')" True
BE=$(req POST /api/workflows/doc-summarize-chain/batch '{"payloads":[{"document":"ok"},{},{"document":"fine"}],"concurrency":3}')
chk "bad payload isolated"     "$(echo "$BE" | jq_ succeeded)" 2
chk "batch keeps going"        "$(echo "$BE" | jq_ total)" 3
chk "empty batch 400"          "$(sc POST /api/workflows/smart-router/batch '{"payloads":[]}')" 400
chk "unknown wf batch 404"     "$(sc POST /api/workflows/ghost/batch '{"payloads":[{}]}')" 404
chk "batch concurrency capped" "$(req POST /api/workflows/doc-summarize-chain/batch '{"payloads":[{"document":"x"}],"concurrency":99}' | jq_ concurrency)" 8
req PUT /api/workflows/_vd '{"name":"_vd","description":"one","nodes":[{"id":"a","type":"template","params":{"text":"x"}}]}' >/dev/null
req PUT /api/workflows/_vd '{"name":"_vd","description":"two","nodes":[{"id":"a","type":"template","params":{"text":"CHANGED"}},{"id":"b","type":"output","params":{"value":"{{a}}"}}]}' >/dev/null
VD=$(req GET '/api/workflows/_vd/versions/diff?a=1&b=2')
chk "version diff added"       "$(echo "$VD" | jq_ summary.added)" 1
chk "version diff changed"     "$(echo "$VD" | jq_ summary.changed)" 1
chk "version diff meta"        "$(echo "$VD" | python3 -c 'import sys,json;print(any(m["field"]=="description" for m in json.load(sys.stdin)["meta"]))')" True
chk "missing version 404"      "$(sc GET '/api/workflows/_vd/versions/diff?a=1&b=99')" 404
req DELETE /api/workflows/_vd >/dev/null
echo "cron + audit"
chk "cron check valid"       "$(req GET '/api/cron/check?expr=0%209%20*%20*%20mon-fri' | jq_ valid)" True
chk "cron describes"         "$(req GET '/api/cron/check?expr=@daily' | jq_ describes)" "every day at midnight"
chk "cron preview count"     "$(req GET '/api/cron/check?expr=0%20*%20*%20*%20*' | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["next"]))')" 5
chk "cron check invalid"     "$(req GET '/api/cron/check?expr=99%20*%20*%20*%20*' | jq_ valid)" False
CS=$(req POST /api/schedules '{"workflow":"doc-summarize-chain","cron":"0 9 * * mon-fri","payload":{"document":"x"}}')
CSID=$(echo "$CS" | jq_ id)
chk "cron schedule created"  "$(echo "$CS" | jq_ cron)" "0 9 * * mon-fri"
chk "cron schedule described" "$(echo "$CS" | python3 -c 'import sys,json;print("Mon" in json.load(sys.stdin)["describes"])')" True
chk "cron persisted in list"  "$(req GET /api/schedules | python3 -c 'import sys,json;print(any(s.get("cron")=="0 9 * * mon-fri" for s in json.load(sys.stdin)))')" True
chk "bad cron rejected"      "$(sc POST /api/schedules '{"workflow":"doc-summarize-chain","cron":"0 0 L * *"}')" 400
chk "interval still works"   "$(req POST /api/schedules '{"workflow":"doc-summarize-chain","every_seconds":300}' | jq_ every_seconds)" 300
req DELETE /api/schedules/$CSID >/dev/null
for s in $(req GET /api/schedules | python3 -c 'import sys,json;[print(x["id"]) for x in json.load(sys.stdin)]'); do req DELETE /api/schedules/$s >/dev/null; done
chk "audit records save"     "$(req PUT /api/workflows/_aud '{"name":"_aud","nodes":[{"id":"a","type":"template","params":{"text":"x"}}]}' >/dev/null; req GET '/api/audit?action=workflow.save&limit=1' | jq_ 0.target)" _aud
chk "audit records actor"    "$(req GET '/api/audit?limit=1' | jq_ 0.actor)" default-admin
chk "audit has detail"       "$(req GET '/api/audit?action=workflow.save&limit=1' | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["detail"] is not None)')" True
req DELETE /api/workflows/_aud >/dev/null
chk "audit records delete"   "$(req GET '/api/audit?action=workflow.delete&limit=1' | jq_ 0.target)" _aud
chk "audit summary"          "$(req GET /api/audit/summary | python3 -c 'import sys,json;print(json.load(sys.stdin)["total"]>0)')" True
AUKEY=$(req POST /api/keys '{"label":"auditro","scopes":["run"]}' | jq_ key)
chk "audit needs admin"      "$(KEYOVER=$AUKEY sc GET /api/audit)" 403
req DELETE /api/keys/$AUKEY >/dev/null
chk "key events audited"     "$(req GET '/api/audit?action=key.create&limit=1' | jq_ 0.target)" auditro
echo "templates"
TL=$(req GET /api/templates)
chk "templates listed"       "$(echo "$TL" | python3 -c 'import sys,json;print(len(json.load(sys.stdin))>=8)')" True
chk "listing has samples"    "$(echo "$TL" | python3 -c 'import sys,json;print(all("sample" in t for t in json.load(sys.stdin)))')" True
TI=$(req POST /api/templates '{"template":"classify-route","name":"_tpl_api"}')
chk "template instantiated"  "$(echo "$TI" | jq_ workflow.name)" _tpl_api
chk "sample returned"        "$(echo "$TI" | python3 -c 'import sys,json;print("message" in json.load(sys.stdin)["sample"])')" True
chk "created wf validates"   "$(req GET /api/workflows/_tpl_api | python3 -c '
import sys,json,urllib.request
d=json.load(sys.stdin)
b=json.dumps({"name":d["name"],"description":d["description"],"nodes":d["nodes"],"on_error":d["on_error"]}).encode()
r=urllib.request.Request("http://localhost:8000/api/validate",data=b,headers={"X-API-Key":"aiflow-dev-key","Content-Type":"application/json"})
print(json.loads(urllib.request.urlopen(r).read())["valid"])')" True
chk "created wf runs"        "$(req POST /api/workflows/_tpl_api/run '{"payload":{"message":"URGENT: everything down"}}' | jq_ status)" success
chk "collision renames"      "$(req POST /api/templates '{"template":"classify-route","name":"_tpl_api"}' | jq_ workflow.name)" _tpl_api-2
chk "unknown template 404"   "$(sc POST /api/templates '{"template":"nope"}')" 404
req DELETE /api/workflows/_tpl_api >/dev/null
req DELETE /api/workflows/_tpl_api-2 >/dev/null

echo "cache + retry"
req DELETE /api/cache >/dev/null
req PUT /api/workflows/_rt '{"name":"_rt","nodes":[
  {"id":"costly","type":"llm","params":{"prompt":"Summarize: pricey"},"cache":true},
  {"id":"gate","type":"input","params":{"key":"needed","required":true}},
  {"id":"tail","type":"template","params":{"text":"ok {{gate}}"}}]}' >/dev/null
FR=$(req POST /api/workflows/_rt/run '{"payload":{}}')
RID=$(echo "$FR" | jq_ run_id)
chk "run fails as expected"  "$(echo "$FR" | jq_ status)" error
RT=$(req POST /api/runs/$RID/retry '{"payload":{"needed":"now"}}')
chk "retry succeeds"         "$(echo "$RT" | jq_ status)" success
chk "retry starts at failure" "$(echo "$RT" | jq_ retried_from)" gate
chk "prior node reused"      "$(echo "$RT" | python3 -c 'import sys,json;print("costly" in json.load(sys.stdin)["reused_nodes"])')" True
chk "retry spends nothing"   "$(echo "$RT" | jq_ usage.llm_calls)" 0
chk "retry of clean run 400" "$(sc POST /api/runs/$(echo "$RT" | jq_ run_id)/retry '{}')" 400
chk "retry unknown run 404"  "$(sc POST /api/runs/ghostrun/retry '{}')" 404
chk "retry bad node 400"     "$(sc POST /api/runs/$RID/retry '{"from_node":"nope"}')" 400
req POST /api/workflows/_rt/run '{"payload":{"needed":"x"}}' >/dev/null
CACHED=$(req POST /api/workflows/_rt/run '{"payload":{"needed":"x"}}')
chk "cache hit recorded"     "$(echo "$CACHED" | python3 -c 'import sys,json;print(any(l["status"]=="cached" for l in json.load(sys.stdin)["logs"]))')" True
chk "no_cache bypasses"      "$(req POST /api/workflows/_rt/run '{"payload":{"needed":"x"},"no_cache":true}' | python3 -c 'import sys,json;print(any(l["status"]=="cached" for l in json.load(sys.stdin)["logs"]))')" False
chk "cache stats exposed"    "$(req GET /api/cache | python3 -c 'import sys,json;print(json.load(sys.stdin)["entries"]>0)')" True
chk "cache clear works"      "$(req DELETE /api/cache | python3 -c 'import sys,json;print(json.load(sys.stdin)["cleared"]>0)')" True
req DELETE /api/workflows/_rt >/dev/null

echo "streaming + search"
chk "health exposes backend" "$(req GET /api/health | python3 -c 'import sys,json;print("search_backend" in json.load(sys.stdin))')" True
chk "tokens stream over sse"  "$(python3 - <<'PYEOF'
import json,threading,time,urllib.request
K={'X-API-Key':'aiflow-dev-key'}
ev=[]
def listen():
    r=urllib.request.urlopen(urllib.request.Request('http://localhost:8000/api/events',headers=K),timeout=20)
    for raw in r:
        l=raw.decode().strip()
        if l.startswith('data:'):
            try: ev.append(json.loads(l[5:].strip()))
            except: pass
        if any(e.get('event')=='run_end' for e in ev): break
t=threading.Thread(target=listen,daemon=True); t.start(); time.sleep(1.2)
b=json.dumps({"payload":{"document":"streaming check"}}).encode()
urllib.request.urlopen(urllib.request.Request(
  'http://localhost:8000/api/workflows/doc-summarize-chain/run',data=b,
  headers={**K,'Content-Type':'application/json'}),timeout=25).read()
t.join(timeout=12)
toks=[e for e in ev if e.get('event')=='token']
print(len(toks)>3 and all('seq' in x and 'node' in x for x in toks))
PYEOF
)" True

echo "stats + sse"
chk "stats has p95"          "$(req GET /api/stats | python3 -c 'import sys,json;print("p95_ms" in json.load(sys.stdin))')" True
chk "sse streams"            "$(curl -s --max-time 2 -N $H/api/events | head -c 6)" "retry:"

echo ""
echo "$P passed, $F failed"
[ "$F" = 0 ] || exit 1
