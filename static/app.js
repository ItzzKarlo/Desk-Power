'use strict';
const $ = id => document.getElementById(id);
const els = {watts:$('watts'),today:$('today'),month:$('month'),cost:$('cost'),rate:$('rate'),dot:$('status-dot'),status:$('status-text'),updated:$('update-time'),powerState:$('power-state'),toggle:$('power-toggle'),canvas:$('chart'),empty:$('empty-chart'),graphDate:$('graph-date'),toast:$('toast')};
let state = null;
let history = [];
let selectedRange = '24h';
let busy = false;
let lastToastTimer;
let historyBucket = 300;

function prettyNumber(value, digits = 2) { return typeof value === 'number' ? value.toLocaleString('en-GB',{maximumFractionDigits:digits, minimumFractionDigits:digits}) : '—'; }
function notify(message){els.toast.textContent=message;els.toast.hidden=false;clearTimeout(lastToastTimer);lastToastTimer=setTimeout(()=>els.toast.hidden=true,5000);}
async function request(path, options) {
  const response = await fetch(path,{cache:'no-store',...options});
  if(!response.ok) throw new Error((await response.text()).slice(0,180) || `HTTP ${response.status}`);
  return response.json();
}
function renderStatus(s){
  state=s;
  const online=!!s.online;
  els.status.parentElement.classList.toggle('online',online);
  els.status.parentElement.classList.toggle('offline',!online);
  els.status.textContent=online?'CONNECTED':'OFFLINE';
  els.watts.textContent=online&&s.watts!=null?prettyNumber(s.watts,s.watts>=100?0:1):'—';
  els.today.textContent=online?prettyNumber(s.today_kwh,3):'—';
  els.month.textContent=online?prettyNumber(s.month_kwh,3):'—';
  els.cost.textContent=online&&s.month_kwh!=null?'€'+prettyNumber(s.month_kwh*s.eur_per_kwh,2):'—';
  els.rate.textContent=`€${prettyNumber(s.eur_per_kwh,2)}/kWh configured`;
  els.powerState.textContent=!online?'Unavailable':s.on?'Powered on':'Powered off';
  els.toggle.classList.toggle('on',online&&s.on);
  els.toggle.disabled=!online||busy;
  els.toggle.setAttribute('aria-label',online?(s.on?'Turn socket off':'Turn socket on'):'Plug unavailable');
  els.updated.textContent=online&&s.updated_at?'Updated '+new Date(s.updated_at*1000).toLocaleTimeString('en-GB'):(s.error||'Connecting to plug');
}
async function loadState(){
  try{renderStatus(await request('/api/state'));}
  catch(err){renderStatus({online:false,error:'Backend unreachable',eur_per_kwh:0});}
}
async function togglePower(){
  if(!state?.online||busy)return;
  const next=!state.on;
  if(!next){
    const consumption = state.watts == null ? 'unknown' : `${prettyNumber(state.watts,1)} W`;
    if(!confirm(`Turn OFF the entire desk outlet?\n\nCurrent draw: ${consumption}\n\nOnly proceed after Zeus is shut down. The server will also block OFF above ${state.off_max_watts} W.`)) return;
  }
  busy=true;renderStatus(state);
  try { renderStatus(await request('/api/power',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on:next})}));notify(`Socket turned ${next?'ON':'OFF'}`);}
  catch(err){notify(err.message);await loadState();}
  finally{busy=false;if(state)renderStatus(state);}
}

function drawChart(){
  const canvas=els.canvas,box=canvas.getBoundingClientRect();
  if(box.width===0||box.height===0)return;
  const ratio=Math.min(window.devicePixelRatio||1,2);
  canvas.width=Math.round(box.width*ratio);canvas.height=Math.round(box.height*ratio);
  const ctx=canvas.getContext('2d');ctx.scale(ratio,ratio);
  const w=box.width,h=box.height, pad={left:47,right:8,top:13,bottom:27};
  const cw=w-pad.left-pad.right,ch=h-pad.top-pad.bottom;
  const now=Math.floor(Date.now()/1000);
  const seconds={'1h':3600,'24h':86400,'7d':604800,'30d':2592000}[selectedRange];
  const begin=now-seconds;
  const maxWatts=Math.max(50,Math.ceil(Math.max(...history.map(x=>x.watts),0)*1.18/25)*25);
  const X=x=>pad.left+(x-begin)/seconds*cw;
  const Y=y=>pad.top+ch-(y/maxWatts)*ch;
  ctx.lineWidth=1;ctx.font='11px system-ui';ctx.textAlign='right';ctx.fillStyle='#7d8da7';
  for(let i=0;i<=4;i++){
    const value=(maxWatts/4)*i,y=Y(value);
    ctx.strokeStyle='#263448';ctx.setLineDash([3,5]);ctx.beginPath();ctx.moveTo(pad.left,y);ctx.lineTo(w-pad.right,y);ctx.stroke();ctx.setLineDash([]);
    ctx.fillText(Math.round(value)+' W',pad.left-9,y+4);
  }
  ctx.textAlign='center';
  const labelFormat=selectedRange==='1h'?{hour:'2-digit',minute:'2-digit'}:selectedRange==='24h'?{hour:'2-digit',minute:'2-digit'}:{month:'short',day:'numeric'};
  for(let i=0;i<=3;i++){
    const when=begin+seconds*i/3;
    ctx.fillText(new Intl.DateTimeFormat('en-GB',labelFormat).format(new Date(when*1000)),pad.left+(cw*i/3),h-4);
  }
  if(!history.length){els.empty.hidden=false;return;}
  els.empty.hidden=true;
  const pts=history.filter(p=>p.ts>=begin-60).map(p=>({x:X(p.ts),y:Y(p.watts),ts:p.ts}));
  if(!pts.length){els.empty.hidden=false;return;}
  // Break lines across gaps where the plug/backend wasn't providing data
  let segments=[],segment=[];
  pts.forEach((point,i)=>{if(i&&point.ts-pts[i-1].ts>historyBucket*3){if(segment.length)segments.push(segment);segment=[];}segment.push(point);});
  if(segment.length)segments.push(segment);
  segments.forEach(points=>{
    if(points.length>1){
      const fill=ctx.createLinearGradient(0,pad.top,0,pad.top+ch);fill.addColorStop(0,'rgba(140,174,255,.18)');fill.addColorStop(1,'rgba(140,174,255,0)');
      ctx.beginPath();ctx.moveTo(points[0].x,pad.top+ch);points.forEach(p=>ctx.lineTo(p.x,p.y));ctx.lineTo(points[points.length-1].x,pad.top+ch);ctx.closePath();ctx.fillStyle=fill;ctx.fill();
      ctx.beginPath();points.forEach((p,i)=>i?ctx.lineTo(p.x,p.y):ctx.moveTo(p.x,p.y));ctx.strokeStyle='#8caeff';ctx.lineWidth=2.3;ctx.lineJoin='round';ctx.stroke();
    } else {const p=points[0];ctx.beginPath();ctx.arc(p.x,p.y,2.8,0,2*Math.PI);ctx.fillStyle='#8caeff';ctx.fill();}
  });
}
async function loadHistory(){
  try{const result=await request('/api/history?range='+selectedRange);history=result.points;historyBucket=result.bucket_seconds;drawChart();}
  catch(err){history=[];drawChart();}
}

document.querySelectorAll('[data-range]').forEach(button=>button.addEventListener('click',()=>{
  selectedRange=button.dataset.range;
  document.querySelectorAll('[data-range]').forEach(b=>b.classList.toggle('active',b===button));
  els.graphDate.textContent={'1h':'LAST HOUR','24h':'LAST 24 HOURS','7d':'LAST 7 DAYS','30d':'LAST 30 DAYS'}[selectedRange];
  loadHistory();
}));
els.toggle.addEventListener('click',togglePower);
window.addEventListener('resize',drawChart);
loadState();loadHistory();
setInterval(loadState,5000);setInterval(loadHistory,60000);
if('serviceWorker'in navigator&&window.isSecureContext){navigator.serviceWorker.register('/service-worker.js').catch(()=>{});}
