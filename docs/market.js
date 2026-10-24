'use strict';
const API='https://fapi.binance.com';
const $=id=>document.getElementById(id);
const stable=new Set(['USDC','USDT','FDUSD','TUSD','DAI','USDP','BUSD','USD1','USDE','USDD','EUR','EURI']);
const fmt=(n,d=2)=>Number(n).toLocaleString('ru-RU',{minimumFractionDigits:d,maximumFractionDigits:d});
const price=n=>Number(n).toLocaleString('ru-RU',{maximumFractionDigits:8});
let info=null,selected=null,busy=false,chartRequest=0;
async function get(path){const r=await fetch(API+path,{signal:AbortSignal.timeout(15000)});if(!r.ok)throw Error('API HTTP '+r.status);return r.json()}
function ema(values,n){let last=values[0];return values.map(x=>(last=x*2/(n+1)+last*(1-2/(n+1))))}
function rsi(values,n=14){let up=0,down=0;for(let i=1;i<values.length;i++){const d=values[i]-values[i-1];if(i<=n){up+=Math.max(d,0)/n;down+=Math.max(-d,0)/n}else{up=(up*(n-1)+Math.max(d,0))/n;down=(down*(n-1)+Math.max(-d,0))/n}}return down===0?(up===0?50:100):100-100/(1+up/down)}
function path(values,lo,span){return values.map((v,i)=>`${i*1000/(values.length-1)},${245-(v-lo)/span*225}`).join(' ')}
async function chart(symbol){
 const request=++chartRequest;selected=symbol;$('chart-title').textContent=symbol+' · 1h';$('chart-status').textContent='Запрашиваю закрытые свечи…';
 $('tradingview').href='https://ru.tradingview.com/chart/?symbol='+encodeURIComponent('BINANCE:'+symbol+'.P');
 try{const raw=await get('/fapi/v1/klines?symbol='+encodeURIComponent(symbol)+'&interval=1h&limit=200');if(request!==chartRequest)return;
  const bars=raw.filter(b=>Number(b[0])+3600000<=Date.now());const values=bars.map(b=>Number(b[4]));
  if(values.length<50||values.some(v=>!Number.isFinite(v)))throw Error('Недостаточно закрытых свечей');
  const lines=[values,ema(values,20),ema(values,50)];const lo=Math.min(...lines.flat()),hi=Math.max(...lines.flat());
  const svg=$('chart');svg.replaceChildren();lines.forEach((line,i)=>{const p=document.createElementNS('http://www.w3.org/2000/svg','polyline');p.setAttribute('points',path(line,lo,hi-lo||1));p.setAttribute('fill','none');p.setAttribute('stroke',['#b5bfca','#f5a623','#57d5ad'][i]);p.setAttribute('stroke-width',i?2:1.5);svg.append(p)});
  $('last').textContent=price(values.at(-1));$('rsi').textContent=fmt(rsi(values),1);
  $('chart-status').textContent='Последняя закрытая свеча: '+new Date(Number(bars.at(-1)[0])+3600000).toLocaleString('ru-RU')+'.';
 }catch(error){if(request!==chartRequest)return;$('chart').replaceChildren();$('last').textContent='—';$('rsi').textContent='—';$('chart-status').textContent='Свечи недоступны: '+error.message}
}
async function refresh(){if(busy)return;busy=true;$('refresh').disabled=true;
 try{if(!info)info=await get('/fapi/v1/exchangeInfo');const tickers=await get('/fapi/v1/ticker/24hr');const byId=new Map(tickers.map(t=>[t.symbol,t]));
 const rows=info.symbols.filter(m=>m.status==='TRADING'&&m.contractType==='PERPETUAL'&&m.underlyingType==='COIN'&&m.quoteAsset==='USDT'&&m.marginAsset==='USDT'&&!stable.has(m.baseAsset)&&m.onboardDate&&Date.now()-Number(m.onboardDate)>=30*86400000&&/^[A-Z0-9]+USDT$/.test(m.symbol)).map(m=>byId.get(m.symbol)).filter(t=>t&&Number.isFinite(Number(t.quoteVolume))&&Number(t.quoteVolume)>=50000000&&Number(t.lastPrice)>0).sort((a,b)=>Number(b.quoteVolume)-Number(a.quoteVolume)).slice(0,20);
 if(!rows.length)throw Error('Нет подходящих контрактов');
 $('markets').replaceChildren();rows.forEach((t,i)=>{const tr=document.createElement('tr');const cells=[i+1,t.symbol,price(t.lastPrice),fmt(t.priceChangePercent)+'%',fmt(Number(t.quoteVolume)/1e6)];cells.forEach((value,j)=>{const td=document.createElement('td');if(j===1){const b=document.createElement('button');b.textContent=value;b.onclick=()=>chart(t.symbol);td.append(b)}else{td.textContent=String(value);if(j===3)td.className=Number(t.priceChangePercent)>=0?'up':'down'}tr.append(td)});$('markets').append(tr)});
 $('status').textContent='Обновлено '+new Date().toLocaleTimeString('ru-RU')+' · '+rows.length+' контрактов · фактические данные Binance';
 document.dispatchEvent(new CustomEvent('markets-ready',{detail:rows.map(t=>t.symbol)})); await chart(rows.some(t=>t.symbol===selected)?selected:rows[0].symbol);
 }catch(error){$('status').textContent='Данные недоступны: '+error.message+'. Последние показанные значения могут быть устаревшими.';if(!$('markets').querySelector('button'))$('markets').textContent='Проверь доступность Binance API в своём регионе.'}
 finally{busy=false;$('refresh').disabled=false}}
$('refresh').onclick=refresh;refresh();setInterval(refresh,60000);
