// Sites dispatch enforces owner-private authentication on all routes.
const headers={"Cache-Control":"no-store","X-Content-Type-Options":"nosniff","Referrer-Policy":"no-referrer"};
const json=(data,status=200)=>Response.json(data,{status,headers});
export default {async fetch(request,env){
 const url=new URL(request.url);
 if(!env.BUCKET)return json({error:"Storage unavailable"},503);
 if(url.pathname==="/api/snapshot"&&request.method==="POST"){
  const origin=request.headers.get("Origin");
  if(origin&&origin!==url.origin)return json({error:"Cross-origin write refused"},403);
  if(Number(request.headers.get("Content-Length")||0)>2000000)return json({error:"Too large"},413);
  const text=await request.text();
  if(new TextEncoder().encode(text).byteLength>2000000)return json({error:"Too large"},413);
  let body;try{body=JSON.parse(text)}catch{return json({error:"Invalid JSON"},400)}
  if(body.mode!=="paper"||typeof body.html!=="string"||!body.html.startsWith("<!doctype html>")||
   !Number.isFinite(body.generated_ts)||!Number.isFinite(body.heartbeat_ts)||body.generated_ts>Date.now()+300000||
   body.generated_ts<Date.now()-600000||body.heartbeat_ts>Date.now()+300000)return json({error:"Invalid paper snapshot"},400);
  const prev=await env.BUCKET.get("latest.json");
  if(prev&&(await prev.json()).generated_ts>body.generated_ts)return json({error:"Older snapshot refused"},409);
  await env.BUCKET.put("latest.json",JSON.stringify(body),{httpMetadata:{contentType:"application/json"}});
  return json({ok:true,generated_ts:body.generated_ts});
 }
 if(request.method!=="GET")return json({error:"Method not allowed"},405);
 if(!["/","/index.html","/health","/api/live","/research-report.txt"].includes(url.pathname))return json({error:"Not found"},404);
 const object=await env.BUCKET.get("latest.json");
 if(!object)return new Response('<!doctype html><html lang="ru"><meta charset="utf-8"><meta http-equiv="refresh" content="20"><title>Futures Agent</title><body style="background:#101214;color:#e8ecef;font:16px system-ui;padding:40px"><h1>Futures Agent · PAPER</h1><p>Хостинг готов. Ожидается первая синхронизация с ноутбуком.</p><p>Реальные средства не подключены.</p></body></html>',{headers:{...headers,"Content-Type":"text/html; charset=utf-8"}});
 const snapshot=await object.json();
 if(url.pathname==='/api/live'){
  if(Number(url.searchParams.get('since')||0)>=snapshot.generated_ts)return new Response(null,{status:204,headers});
  return json(snapshot);
 }
 if(url.pathname==='/research-report.txt')return new Response(snapshot.report_text||'Отчёт ещё не сформирован.',{headers:{...headers,'Content-Type':'text/plain; charset=utf-8','Content-Disposition':'attachment; filename="research-report.txt"'}});
 if(url.pathname==="/health")return json({mode:"paper",generated_ts:snapshot.generated_ts,heartbeat_ts:snapshot.heartbeat_ts,stale:Date.now()-snapshot.heartbeat_ts>180000});
 return new Response(snapshot.html,{headers:{...headers,"Content-Type":"text/html; charset=utf-8"}});
}};
