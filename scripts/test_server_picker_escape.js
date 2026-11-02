const fs=require('fs'), vm=require('vm'), assert=require('assert');
const template=fs.readFileSync('dashboard/templates/server_select.html','utf8');
const js=template.match(/<script>\s*([\s\S]*?)\s*<\/script>/)[1];
async function test(servers, granted, fail=false) {
 const mount={innerHTML:''}; const data={textContent: JSON.stringify(granted)};
 const document={getElementById:id=>id==='unified-servers-mount'?mount:(id==='accessible-server-data'?data:null)};
 const ctx={window:{},document,console,fetch:async()=>{if(fail) throw Error('network'); return {ok:true,json:async()=>({servers})};}};
 vm.runInNewContext(js,ctx); await new Promise(r=>setImmediate(r)); return {html:mount.innerHTML,ctx};
}
(async()=>{
 let id='900719925474099123', bad='<img src=x onerror=alert(1)>';
 let {html,ctx}=await test([{id_str:id,name:bad,icon:'bad"',is_bot_member:true},{id_str:'123',name:'Invite',is_bot_member:false}], [{id,name:bad,level:'admin',icon:null}]);
 assert(html.includes('Active Servers')); assert(html.includes('Available to Setup')); assert(html.includes(id));
 assert(!html.includes(bad)); assert(html.includes('&lt;img')); assert(!html.includes('icons/'+id+'/bad"'));
 assert(html.includes("selectServerPost('"+id+"')")); assert(html.includes("inviteBot('123'"));
 ({html}=await test([], [{id,name:'Granted',level:'admin'}],true)); assert(html.includes('Granted')); assert(html.includes('Retry'));
 ({html}=await test([{id_str:'123" onclick="evil()',name:'Evil',is_bot_member:false}], [])); assert(!html.includes('onclick="evil()'));
 console.log('Unified picker XSS, grant preservation, network fallback and precise IDs pass');
})().catch(e=>{console.error(e);process.exit(1)});
