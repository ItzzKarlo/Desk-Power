'use strict';
const $ = id => document.getElementById(id);
const els = {watts:$('watts'),today:$('today'),month:$('month'),cost:$('cost'),rate:$('rate'),status:$('status-text'),updated:$('update-time'),count:$('plug-count'),grid:$('plug-grid'),select:$('graph-plug'),canvas:$('chart'),empty:$('empty-chart'),graphDate:$('graph-date'),toast:$('toast')};
let state = null;
let history = [];
let selectedRange = '24h';
let selectedPlug = 'total';
let historyBucket = 300;
let lastToastTimer;
const busy = new Set();
const cards = new Map();

function prettyNumber(value, digits = 2){return typeof value === 'number' ? value.toLocaleString('en-GB',{maximumFractionDigits:digits,minimumFractionDigits:digits}) : '—';}
function notify(message){els.toast.textContent=message;els.toast.hidden=false;clearTimeout(lastToastTimer);lastToastTimer=setTimeout(()=>els.toast.hidden=true,6000);}
async function request(path, options){
  const response=await fetch(path,{cache:'no-store',...options});
  if(!response.ok)throw new Error((await response.text()).slice(0,220)||`HTTP ${response.status}`);
  return response.json();
}
function makeCard(plug){
  const article=document.createElement('article');article.className='plug-card';
  const info=document.createElement('div');info.className='plug-info';
  const name=document.createElement('div');name.className='plug-title';
  const dot=document.createElement('span');dot.className='plug-indicator';
  const title=document.createElement('span');name.append(dot,title);
  const draw=document.createElement('span');draw.className='plug-watts';
  const sub=document.createElement('p');sub.className='plug-meta';
  info.append(name,draw,sub);
  const button=document.createElement('button');button.className='power-btn';button.type='button';
  button.setAttribute('aria-label','Switch outlet');
  button.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M12 2v10"/><path d="M6.2 5.9a9 9 0 1 0 11.6 0"/></svg>';
  button.addEventListener('click',()=>togglePower(plug.id));
  article.append(info,button);
  els.grid.append(article);
  const card={article,dot,title,draw,sub,button};cards.set(plug.id,card);
  return card;
}
function syncSelector(plugs){
  const existing=Array.from(els.select.options).map(o=>o.value).join(',');
  const wanted=['total',...plugs.map(p=>p.id)].join(',');
  if(existing===wanted)return;
  els.select.replaceChildren();
  [['total','Combined'],...plugs.map(p=>[p.id,p.name])].forEach(([value,label])=>{
    const option=document.createElement('option');option.value=value;option.textContent=label;els.select.append(option);
  });
  if(!['total',...plugs.map(p=>p.id)].includes(selectedPlug))selectedPlug='total';
  els.select.value=selectedPlug;
}
function renderStatus(s){
  state=s;
  const plugs=s.plugs||[];
  const complete=s.online&&plugs.length>0;
  els.status.parentElement.classList.toggle('online',complete);
  els.status.parentElement.classList.toggle('offline',!complete);
  els.status.textContent=complete?'CONNECTED':s.online_count?'PARTIAL':'OFFLINE';
  els.watts.textContent=complete&&s.watts!=null?prettyNumber(s.watts,s.watts>=100?0:1):'—';
  els.today.textContent=complete?prettyNumber(s.today_kwh,3):'—';
  els.month.textContent=complete?prettyNumber(s.month_kwh,3):'—';
  els.cost.textContent=complete&&s.month_kwh!=null?'€'+prettyNumber(s.month_kwh*s.eur_per_kwh,2):'—';
  els.rate.textContent=`€${prettyNumber(s.eur_per_kwh,2)}/kWh configured`;
  els.count.textContent=`${s.online_count||0} / ${s.plug_count||plugs.length} PLUGS CONNECTED`;
  els.updated.textContent=complete?'Live total across both outlets':'Totals unavailable until all outlets are online';
  const visible=new Set(plugs.map(p=>p.id));
  for(const [id,card] of cards){if(!visible.has(id)){card.article.remove();cards.delete(id);}}
  plugs.forEach(plug=>{
    const card=cards.get(plug.id)||makeCard(plug);
    card.article.classList.toggle('offline',!plug.online);
    card.dot.classList.toggle('online',!!plug.online);
    card.title.textContent=plug.name;
    card.draw.textContent=plug.online&&plug.watts!=null?`${prettyNumber(plug.watts,1)} W`:'— W';
    card.sub.textContent=!plug.online?(plug.error||'Unreachable'):`${plug.on?'ON':'OFF'} · ${prettyNumber(plug.today_kwh,3)} kWh today`;
    card.button.disabled=!plug.online||busy.has(plug.id);
    card.button.classList.toggle('on',plug.online&&plug.on);
    card.button.setAttribute('aria-label',`${plug.on?'Turn off':'Turn on'} ${plug.name}`);
  });
  syncSelector(plugs);
}
async function loadState(){
  try{renderStatus(await request('/api/state'));}
  catch(err){renderStatus({online:false,online_count:0,plug_count:0,plugs:[],eur_per_kwh:0});}
}
async function togglePower(id){
  const plug=state?.plugs?.find(p=>p.id===id);
  if(!plug?.online||busy.has(id))return;
  const next=!plug.on;
  if(!next){
    const draw=plug.watts==null?'unknown':`${prettyNumber(plug.watts,1)} W`;
    if(!confirm(`Turn OFF ${plug.name}?\n\nCurrent draw: ${draw}\n\nEnsure any computer on this outlet is already shut down. The backend will block OFF above ${plug.off_max_watts} W.`))return;
  }
  busy.add(id);renderStatus(state);
  try{
    await request('/api/plugs/'+encodeURIComponent(id)+'/power',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on:next})
    });
    notify(`${plug.name} switched ${next?'ON':'OFF'}`);
  }catch(err){notify(err.message);}
  finally{busy.delete(id);await loadState();await loadHistory();}
}
function drawChart(){
  const canvas=els.canvas,box=canvas.getBoundingClientRect();
  if(box.width===0||box.height===0)return;
  const ratio=Math.min(window.devicePixelRatio||1,2);
  canvas.width=Math.round(box.width*ratio);canvas.height=Math.round(box.height*ratio);
  const ctx=canvas.getContext('2d');ctx.scale(ratio,ratio);
  const w=box.width,h=box.height,pad={left:47,right:8,top:13,bottom:27};
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
  const labelFormat=selectedRange==='1h'||selectedRange==='24h'?{hour:'2-digit',minute:'2-digit'}:{month:'short',day:'numeric'};
  for(let i=0;i<=3;i++){
    const when=begin+seconds*i/3;
    ctx.fillText(new Intl.DateTimeFormat('en-GB',labelFormat).format(new Date(when*1000)),pad.left+(cw*i/3),h-4);
  }
  if(!history.length){els.empty.hidden=false;return;}
  els.empty.hidden=true;
  const pts=history.filter(p=>p.ts>=begin-60).map(p=>({x:X(p.ts),y:Y(p.watts),ts:p.ts}));
  if(!pts.length){els.empty.hidden=false;return;}
  let segments=[],segment=[];
  pts.forEach((point,i)=>{if(i&&point.ts-pts[i-1].ts>historyBucket*3){if(segment.length)segments.push(segment);segment=[];}segment.push(point);});
  if(segment.length)segments.push(segment);
  segments.forEach(points=>{
    if(points.length>1){
      const fill=ctx.createLinearGradient(0,pad.top,0,pad.top+ch);fill.addColorStop(0,'rgba(140,174,255,.18)');fill.addColorStop(1,'rgba(140,174,255,0)');
      ctx.beginPath();ctx.moveTo(points[0].x,pad.top+ch);points.forEach(p=>ctx.lineTo(p.x,p.y));ctx.lineTo(points[points.length-1].x,pad.top+ch);ctx.closePath();ctx.fillStyle=fill;ctx.fill();
      ctx.beginPath();points.forEach((p,i)=>i?ctx.lineTo(p.x,p.y):ctx.moveTo(p.x,p.y));ctx.strokeStyle='#8caeff';ctx.lineWidth=2.3;ctx.lineJoin='round';ctx.stroke();
    }else{const p=points[0];ctx.beginPath();ctx.arc(p.x,p.y,2.8,0,2*Math.PI);ctx.fillStyle='#8caeff';ctx.fill();}
  });
}
async function loadHistory(){
  try{
    const result=await request('/api/history?range='+selectedRange+'&plug='+encodeURIComponent(selectedPlug));
    history=result.points;historyBucket=result.bucket_seconds;drawChart();
  }catch(err){history=[];drawChart();}
}
document.querySelectorAll('[data-range]').forEach(button=>button.addEventListener('click',()=>{
  selectedRange=button.dataset.range;
  document.querySelectorAll('[data-range]').forEach(b=>b.classList.toggle('active',b===button));
  els.graphDate.textContent={'1h':'LAST HOUR','24h':'LAST 24 HOURS','7d':'LAST 7 DAYS','30d':'LAST 30 DAYS'}[selectedRange];
  loadHistory();
}));
els.select.addEventListener('change',()=>{selectedPlug=els.select.value;loadHistory();});
window.addEventListener('resize',drawChart);
loadState();loadHistory();
setInterval(loadState,5000);setInterval(loadHistory,60000);
if('serviceWorker'in navigator&&window.isSecureContext){navigator.serviceWorker.register('/service-worker.js').catch(()=>{});}
