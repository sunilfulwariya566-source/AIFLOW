// Headless test of the canvas graph logic against the real seeded workflows.
const fs=require('fs'), http=require('http');
const html=fs.readFileSync('static/index.html','utf8');
let js=html.slice(html.lastIndexOf('<script>')+8, html.lastIndexOf('</script>'));

// neutralise browser-only top-level declarations
js = js
  .replace(/const \$=s=>document\.querySelector\(s\);/, '')
  .replace(/^const esc=.*$/m, '')
  .replace(/^async function api\(u,o=\{\}\)\{[\s\S]*?^\}$/m, 'async function api(){return {};}')
  .replace(/^boot\(\);$/m, '')
  .replace(/^\$\('#payload'\)\.addEventListener.*$/m, '');

const STUBS = [
  "var __el=()=>({innerHTML:'',style:{},dataset:{},querySelector:__el,",
  "  querySelectorAll:()=>[],setAttribute(){},appendChild(){},remove(){},",
  "  addEventListener(){},getBoundingClientRect:()=>({left:0,top:0}),",
  "  scrollLeft:0,scrollTop:0});",
  "var document={querySelector:__el,querySelectorAll:()=>[],",
  "  createElementNS:()=>({setAttribute(){},addEventListener(){},innerHTML:''})};",
  "var $=__el; var esc=s=>String(s===undefined||s===null?'':s);",
  "var location={origin:''}; var EventSource=function(){}; var CSS={escape:s=>s};",
  "var window={};",
].join("\n");

const EXPORTS = `
  return {depMap,NODE_TYPES,NEW_PARAMS,TYPE_COLOR,paramHint,canvasDoc,autoLayout,
          addNode,connect,delNode,deleteEdge,
          setCur:c=>{CUR=c}, getCur:()=>CUR, getPos:()=>POS, setPos:p=>{POS=p}};`;

let API;
try { API = new Function(STUBS + "\n" + js + EXPORTS)(); }
catch(e){ console.log('LOAD FAIL:', e.message); process.exit(1); }

let P=0,F=0;
const chk=(n,c,x='')=>{ if(c){console.log('  ok   '+n);P++;} else {console.log('  FAIL '+n+'  '+x);F++;} };
const K={'X-API-Key':'aiflow-dev-key'};
const get=p=>new Promise((res,rej)=>http.get({host:'localhost',port:8000,path:p,headers:K},
  r=>{let d='';r.on('data',c=>d+=c);r.on('end',()=>res(JSON.parse(d)));}).on('error',rej));
const post=(p,body)=>new Promise((res,rej)=>{
  const b=JSON.stringify(body);
  const r=http.request({host:'localhost',port:8000,path:p,method:'POST',
    headers:{...K,'Content-Type':'application/json','Content-Length':Buffer.byteLength(b)}},
    x=>{let d='';x.on('data',c=>d+=c);x.on('end',()=>res(JSON.parse(d)));});
  r.on('error',rej); r.write(b); r.end();});
const clone=o=>JSON.parse(JSON.stringify(o));

(async()=>{
console.log('dependency inference (must mirror engine topo_order)');
const wfs=await get('/api/workflows');
const byName=n=>clone(wfs.find(w=>w.name===n));

const st=byName('support-triage');
API.setCur(st); let d=API.depMap();
chk('llm depends on input',      d['analysis'].has('ticket'), [...d['analysis']]);
chk('expr identifiers inferred', d['parsed'].has('analysis'), [...d['parsed']]);
chk('branch condition inferred', d['urgent'].has('parsed')&&d['urgent'].has('ticket'), [...d['urgent']]);
chk('nested {{a.b}} ref',        d['reply'].has('parsed'), [...d['reply']]);
chk('output collects refs',      d['result'].has('reply')&&d['result'].has('urgent'), [...d['result']]);
chk('root node has no deps',     d['ticket'].size===0);

API.setCur(byName('content-pipeline')); d=API.depMap();
chk('approval acts as barrier',  d['bundle'].has('signoff'), [...d['bundle']]);

API.setCur(byName('batch-review-miner')); d=API.depMap();
chk('map sees lazy step refs',   d['scored'].has('reviews'), [...d['scored']]);
chk('filter over ref',           d['negative'].has('objs'), [...d['negative']]);

API.setCur(byName('kb-rag-answer')); d=API.depMap();
chk('explicit depends_on kept',  d['hits'].has('indexed'), [...d['hits']]);

console.log('editing operations');
API.setCur(byName('support-triage')); API.setPos({});
const before=API.getCur().nodes.length;
API.addNode('llm',100,100);
const added=API.getCur().nodes[before];
chk('addNode appends',           API.getCur().nodes.length===before+1);
chk('unique id generated',       added.id==='llm'||/^llm_\d+$/.test(added.id), added.id);
chk('seed params applied',       JSON.stringify(added.params).includes('prompt'));
chk('drop position recorded',    API.getPos()[added.id].x===100);
API.connect('ticket',added.id);
chk('connect writes depends_on', added.depends_on.includes('ticket'));
API.connect('ticket',added.id);
chk('connect is idempotent',     added.depends_on.length===1, added.depends_on);
API.addNode('llm',10,10);
chk('id collision avoided',      API.getCur().nodes[before+1].id!==added.id,
                                 API.getCur().nodes[before+1].id);
API.delNode(added.id);
chk('delNode removes node',      !API.getCur().nodes.some(n=>n.id===added.id));
chk('dangling deps cleaned',     API.getCur().nodes.every(n=>!(n.depends_on||[]).includes(added.id)));

console.log('palette integrity');
const health=await get('/api/health');
chk('16 node types',             API.NODE_TYPES.length===16, API.NODE_TYPES.length);
chk('palette matches server',    API.NODE_TYPES.slice().sort().join()===health.node_types.slice().sort().join());
chk('every type has a colour',   API.NODE_TYPES.every(t=>API.TYPE_COLOR[t]));
chk('every type has seed params',API.NODE_TYPES.every(t=>API.NEW_PARAMS[t]));
chk('paramHint truncates',       API.paramHint({params:{prompt:'x'.repeat(300)}}).length<=42);
chk('paramHint tolerates empty', API.paramHint({params:{}})==='');

console.log('auto-layout');
API.setCur(byName('support-triage')); API.setPos({}); API.autoLayout();
const pos=API.getPos();
chk('every node positioned',     API.getCur().nodes.every(n=>pos[n.id]));
chk('deps sit left of dependents', pos['ticket'].x < pos['analysis'].x,
                                 JSON.stringify([pos['ticket'].x,pos['analysis'].x]));
chk('chain flows rightward',     pos['analysis'].x < pos['parsed'].x && pos['parsed'].x < pos['result'].x);
chk('siblings share a column',   pos['ticket'].x===pos['customer'].x);
chk('siblings do not overlap',   pos['ticket'].y!==pos['customer'].y);

console.log('edge deletion');
API.setCur(byName('support-triage')); API.setPos({});
API.addNode('template',10,10);
const tgt=API.getCur().nodes[API.getCur().nodes.length-1];
API.connect('ticket',tgt.id);
chk('explicit edge exists',     (tgt.depends_on||[]).includes('ticket'));
API.deleteEdge('ticket',tgt.id);
chk('explicit edge deleted',    !(tgt.depends_on||[]).includes('ticket'), JSON.stringify(tgt.depends_on));
chk('empty depends_on dropped', tgt.depends_on===undefined, JSON.stringify(tgt.depends_on));
API.connect('ticket',tgt.id); API.connect('customer',tgt.id);
API.deleteEdge('ticket',tgt.id);
chk('only target edge removed', !tgt.depends_on.includes('ticket')&&tgt.depends_on.includes('customer'),
                                JSON.stringify(tgt.depends_on));
// inferred edges must survive a delete attempt (they come from {{refs}})
API.setCur(byName('support-triage'));
const analysis=API.getCur().nodes.find(n=>n.id==='analysis');
const paramsBefore=JSON.stringify(analysis.params);
API.deleteEdge('ticket','analysis');
chk('inferred edge NOT silently removed', API.depMap()['analysis'].has('ticket'));
chk('inferred delete leaves params intact',
    JSON.stringify(API.getCur().nodes.find(n=>n.id==='analysis').params)===paramsBefore);

console.log('layout persistence');
API.setCur(byName('support-triage')); API.setPos({}); API.autoLayout();
const laid=JSON.parse(JSON.stringify(API.getPos()));
const lr=await new Promise((res,rej)=>{const b=JSON.stringify({layout:laid});
  const r=http.request({host:'localhost',port:8000,path:'/api/workflows/support-triage/layout',
    method:'PUT',headers:{...K,'Content-Type':'application/json','Content-Length':Buffer.byteLength(b)}},
    x=>{let d='';x.on('data',c=>d+=c);x.on('end',()=>res(JSON.parse(d)));});
  r.on('error',rej); r.write(b); r.end();});
chk('layout saved to server',    JSON.stringify(lr.layout)===JSON.stringify(laid));
const reread=await get('/api/workflows/support-triage');
chk('layout survives reload',    JSON.stringify(reread.layout)===JSON.stringify(laid));
chk('layout save did not bump version', reread.version===byName('support-triage').version,
                                 reread.version+' vs '+byName('support-triage').version);
API.setCur(reread);
API.setPos(reread.layout&&Object.keys(reread.layout).length?JSON.parse(JSON.stringify(reread.layout)):{});
chk('canvas restores saved positions', API.getPos()['ticket'].x===laid['ticket'].x);
chk('canvasDoc carries layout',  JSON.stringify(API.canvasDoc().layout)===JSON.stringify(laid));

console.log('conditional edges in canvas');
API.setCur(byName('smart-router'));
let dsr=API.depMap();
chk('when refs become edges',    dsr['urgent'].has('triage'), [...dsr['urgent']]);
chk('workflow node is a dep',    dsr['triage'].has('message'), [...dsr['triage']]);
chk('16 node types now',         API.NODE_TYPES.length===16, API.NODE_TYPES.length);
chk('workflow type in palette',  API.NODE_TYPES.includes('workflow'));
chk('workflow has colour+seed',  !!API.TYPE_COLOR['workflow']&&!!API.NEW_PARAMS['workflow']);
const h2=await get('/api/health');
chk('palette still matches server',
    API.NODE_TYPES.slice().sort().join()===h2.node_types.slice().sort().join());
API.setCur({name:'_when_t',description:'',on_error:'stop',nodes:[
  {id:'a',type:'input',params:{key:'n'}},
  {id:'b',type:'template',params:{text:'x'},when:'a > 5'}]});
chk('when creates an edge',      API.depMap()['b'].has('a'), [...API.depMap()['b']]);

console.log('server round-trip');
API.setCur(byName('support-triage'));
const doc=API.canvasDoc();
chk('canvasDoc shape',           doc.name==='support-triage'&&Array.isArray(doc.nodes));
const v=await post('/api/validate',doc);
chk('canvas output validates',   v.valid===true, JSON.stringify(v.errors));
const dm=API.depMap();
chk('edges agree with engine order',
    v.execution_order.every((id,i)=>[...(dm[id]||[])].every(p=>v.execution_order.indexOf(p)<i)),
    v.execution_order.join('→'));

// a canvas-built workflow must actually run
API.setCur({name:'_canvas_built',description:'built via canvas',on_error:'stop',nodes:[]});
API.setPos({});
API.addNode('input',40,40);  API.getCur().nodes[0].params={key:'text',required:true};
API.addNode('llm',300,40);   API.getCur().nodes[1].params={prompt:'Summarize: {{input}}'};
API.addNode('output',560,40);API.getCur().nodes[2].params={value:{summary:'{{llm}}'}};
const built=API.canvasDoc();
const bv=await post('/api/validate',built);
chk('built workflow validates',  bv.valid===true, JSON.stringify(bv.errors));
chk('inferred order correct',    bv.execution_order.join(',')==='input,llm,output', bv.execution_order.join(','));
await new Promise((res,rej)=>{const b=JSON.stringify(built);
  const r=http.request({host:'localhost',port:8000,path:'/api/workflows/_canvas_built',method:'PUT',
    headers:{...K,'Content-Type':'application/json','Content-Length':Buffer.byteLength(b)}},
    x=>{x.resume();x.on('end',res);}); r.on('error',rej); r.write(b); r.end();});
const runres=await post('/api/workflows/_canvas_built/run',{payload:{text:'hello world'}});
chk('built workflow runs',       runres.status==='success', runres.error||'');
chk('built workflow outputs',    !!(runres.outputs&&runres.outputs.output&&runres.outputs.output.summary),
                                 JSON.stringify(runres.outputs));
await new Promise(res=>http.request({host:'localhost',port:8000,
  path:'/api/workflows/_canvas_built',method:'DELETE',headers:K},x=>{x.resume();x.on('end',res);}).end());

console.log('');
console.log(P+' passed, '+F+' failed');
process.exit(F?1:0);
})();
