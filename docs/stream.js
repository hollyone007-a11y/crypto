(()=>{'use strict';let ws,key='',retry=1000,last=0;const quotes=new Map();
const status=document.createElement('p');status.id='public-stream';status.className='muted small';$('status').after(status);
function connect(symbols){const next=symbols.join(',');if(next===key&&ws&&ws.readyState<2)return;key=next;if(ws){ws.onclose=null;ws.close()}if(!symbols.length)return;
 try{ws=new WebSocket('wss://fstream.binance.com/market/stream?streams='+symbols.map(s=>s.toLowerCase()+'@markPrice@1s').join('/'));ws.onopen=()=>{retry=1000};ws.onmessage=e=>{try{const d=JSON.parse(e.data).data;const p=Number(d.p),t=Number(d.E);if(symbols.includes(d.s)&&Number.isFinite(p)&&p>0&&Number.isFinite(t)&&t<Date.now()+30000){quotes.set(d.s,{price:p,ts:t});last=Date.now()}}catch{}};ws.onclose=()=>setTimeout(()=>connect(symbols),Math.min(30000,retry*=2));ws.onerror=()=>ws.close()}catch{setTimeout(()=>connect(symbols),5000)}}
document.addEventListener('markets-ready',event=>connect(event.detail));
setInterval(()=>{const fresh=last&&Date.now()-last<10000;status.textContent=fresh?'LIVE · маркировочные цены Binance, поток 1 с. Оборот и изменение за 24ч — снимок раз в минуту.':'Поток недоступен · цены из последнего HTTP-снимка могут устареть.';status.className=fresh?'up small':'down small';
 $('markets').querySelectorAll('tr').forEach(row=>{const symbol=row.cells[1]?.textContent.trim(),q=quotes.get(symbol);if(q&&Date.now()-q.ts<10000&&row.cells[2])row.cells[2].textContent=price(q.price)});
},500);
})();
