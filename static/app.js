"use strict";
const $=id=>document.getElementById(id),keys=["left","top","width","height"],svgNS="http://www.w3.org/2000/svg";
let roi={left:5,top:5,width:80,height:60},cfg=null,planned=null,request=0,drag=null,active=null,owned=null,previewId=null,selected=null,history=[],angle=0,zoom=0;
let selectionVersion=0,ownedVersion=0,planTimer=null;
const round=v=>Math.round(v*10)/10,clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
function node(tag,attrs,parent){const n=document.createElementNS(svgNS,tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,v);parent.append(n);return n}
function options(id,items){$(id).replaceChildren(...items.map(([v,t])=>new Option(t,v)))}
async function api(path,data){const r=await fetch(path,data===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)});const j=await r.json();if(!r.ok)throw Error(j.error||r.status);return j}
function input(){return {...roi,dpi:+$("dpi").value,mode:$("mode").value,format:$("format").value,method:$("method").value}}
function normalized(r){let w=clamp(round(r.width),3,210),h=clamp(round(r.height),3,291);return {left:clamp(round(r.left),0,round(210-w)),top:clamp(round(r.top),0,round(291-h)),width:w,height:h}}
function sync(r){roi=normalized(r);for(const k of keys)$(k).value=roi[k].toFixed(1);render();schedulePlan()}
function render(){
 const {left:x,top:y,width:w,height:h}=roi;
 for(const [k,v] of Object.entries({x,y,width:w,height:h}))$("roi").setAttribute(k,v);
 $("handles").replaceChildren();
 for(const [name,a,b] of [["nw",x,y],["n",x+w/2,y],["ne",x+w,y],["e",x+w,y+h/2],["se",x+w,y+h],["s",x+w/2,y+h],["sw",x,y+h],["w",x,y+h/2]]){
  if($("edit").value!=="fixed")node("rect",{x:a-1.8,y:b-1.8,width:3.6,height:3.6,fill:"#2563eb","data-handle":name},$("handles"));
 }
}
function enabled(){ $("scan").disabled=!!active||!planned;$("preview").disabled=!!active;$("cancel").disabled=!owned||active!==owned}
function schedulePlan(){clearTimeout(planTimer);request++;planned=null;enabled();planTimer=setTimeout(refreshPlan,100)}
async function refreshPlan(){
 const seq=++request;planned=null;enabled();$("tiles").replaceChildren();
 const d=+$("dpi").value,pixels=Math.ceil(roi.width*d/25.4)*Math.ceil(roi.height*d/25.4);
 $("meter").value=pixels/1e6;$("dimensions").textContent=(pixels/1e6).toFixed(2)+" MP / "+(pixels*3/1e6).toFixed(1)+" MB RGB（概算） "+Math.ceil(roi.width*d/25.4)+" × "+Math.ceil(roi.height*d/25.4)+" px";
 try{const p=await api("/api/plan",input());if(seq!==request)return;planned=p;
 $("hint").textContent=(p.tiles.length===1?"計算上は一括可能":p.tiles.length+" 枚に分割")+"\n現高さで最大幅 "+p.maxWidth.toFixed(1)+" mm / 現幅で最大高さ "+p.maxHeight.toFixed(1)+" mm";
 for(const t of p.tiles)node("rect",{x:t.left,y:t.top,width:t.width,height:t.height,fill:"none",stroke:"#16834b","stroke-width":".55","stroke-dasharray":"2 1"},$("tiles"));
 }catch(e){if(seq!==request)return;$("hint").textContent=e.message}
 enabled();
}
function fit(){
 const d=+$("dpi").value;let w=roi.width,h=roi.height;
 const scale=Math.min(1,Math.sqrt(29_500_000/(w*h*(d/25.4)**2)));
 sync({...roi,width:Math.max(3,Math.floor(w*scale*10)/10),height:Math.max(3,Math.floor(h*scale*10)/10)})
}
function point(e){const p=new DOMPoint(e.clientX,e.clientY).matrixTransform($("bed").getScreenCTM().inverse());return {x:clamp(p.x,0,210),y:clamp(p.y,0,291)}}
$("bed").addEventListener("pointerdown",e=>{
 if(e.button!==0)return;
 const p=point(e);const handle=e.target.dataset.handle||"new";
 if($("edit").value==="fixed"&&handle==="new"){sync({...roi,left:p.x-roi.width/2,top:p.y-roi.height/2});return}
 drag={p,r:{...roi},handle};$("bed").setPointerCapture(e.pointerId);e.preventDefault()
});
$("bed").addEventListener("pointermove",e=>{
 const p=point(e);$("coords").textContent="X "+p.x.toFixed(1)+" / Y "+p.y.toFixed(1)+" mm";
 if(!drag)return;
 const {r,handle,p:a}=drag;const dx=p.x-a.x,dy=p.y-a.y;
 if(handle==="move"){sync({...r,left:r.left+dx,top:r.top+dy});return}
 let l=r.left,t=r.top,rr=l+r.width,b=t+r.height;
 if(handle==="new"){l=Math.min(a.x,p.x);rr=Math.max(a.x,p.x);t=Math.min(a.y,p.y);b=Math.max(a.y,p.y)}
 else {if(handle.includes("w"))l=Math.min(rr-3,l+dx);if(handle.includes("e"))rr=Math.max(l+3,rr+dx);if(handle.includes("n"))t=Math.min(b-3,t+dy);if(handle.includes("s"))b=Math.max(t+3,b+dy)}
 l=clamp(l,0,207);t=clamp(t,0,288);rr=clamp(rr,l+3,210);b=clamp(b,t+3,291);
 let w=rr-l,h=b-t;
 if($("edit").value==="ratio"){const ratio=+$("ratio").value;h=w/ratio;if(h>291-t){h=291-t;w=h*ratio}if(h<3){h=3;w=3*ratio}}
 if($("edit").value==="max"){const d=+$("dpi").value;const scale=Math.min((210-l)/w,(291-t)/h,Math.sqrt(29_500_000/(w*h*(d/25.4)**2)));w*=scale;h*=scale}
 sync({left:l,top:t,width:w,height:h})
});
for(const ev of ["pointerup","pointercancel"])$("bed").addEventListener(ev,()=>drag=null);
$("bed").addEventListener("keydown",e=>{const delta={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]}[e.key];if(!delta)return;e.preventDefault();const step=e.shiftKey?1:.1;sync({...roi,left:roi.left+delta[0]*step,top:roi.top+delta[1]*step})});
for(const k of keys)$(k).addEventListener("change",()=>{const raw=Object.fromEntries(keys.map(k=>[k,Number($(k).value)]));if(keys.some(k=>$(k).value===""||!Number.isFinite(raw[k]))){sync(roi);return}if(raw.left<0||raw.top<0||raw.width<3||raw.height<3||raw.left+raw.width>210.00001||raw.top+raw.height>291.00001){$("hint").textContent="原稿台の範囲外です。直前の値に戻しました。";for(const k of keys)$(k).value=roi[k].toFixed(1);return}sync(raw)});
for(const id of ["dpi","mode","format","method"])$(id).addEventListener("change",refreshPlan);
$("edit").onchange=render;$("full").onclick=()=>sync({left:0,top:0,width:210,height:291});$("fit").onclick=fit;
async function start(preview){try{const p=preview?{left:0,top:0,width:210,height:291,dpi:200,mode:"24bit Color",format:"png",method:"single"}:input();$("scan").disabled=true;$("preview").disabled=true;const j=await api("/api/scan",p);active=owned=j.id;ownedVersion=selectionVersion;if(preview)previewId=j.id;enabled()}catch(e){$("status").textContent=e.message;enabled()}}
$("scan").onclick=()=>start(false);$("preview").onclick=()=>start(true);$("cancel").onclick=async()=>{try{await api("/api/cancel",{id:owned})}catch(e){$("status").textContent=e.message}};
function tileInfo(){return history.find(j=>j.id===selected)}
function showTile(){const j=tileInfo(),t=j?.results.find(t=>String(t.index)===$("tile").value);for(const id of ["downloadOriginal","downloadJpeg","downloadPdf"])$(id).disabled=!t;$("downloadZip").disabled=!j?.results.length;if(!t){$("result").removeAttribute("src");$("resultmeta").textContent=j?.error||"";return}
 $("result").src="/api/image?id="+j.id+"&tile="+t.index;
 $("resultmeta").textContent=j.backend+" / "+j.plan.dpi+" dpi / "+t.widthPx+" × "+t.heightPx+" px / "+(t.bytes/1e6).toFixed(2)+" MB / "+JSON.stringify(t.region);
 angle=zoom=0;view();
}
function selectJob(id){selected=id;const j=tileInfo();options("tile",(j?.results||[]).map(t=>[t.index,"Tile "+t.index]));$("history").value=id;showTile()}
$("history").onchange=()=>{selectionVersion++;selectJob($("history").value)};$("tile").onchange=()=>{selectionVersion++;showTile()};
function view(){const img=$("result");img.style.maxWidth=zoom?"none":"100%";img.style.width=zoom?Math.round(img.naturalWidth*zoom)+"px":"";img.style.transform="rotate("+angle+"deg)"}
$("result").onload=view;$("zoomfit").onclick=()=>{angle=zoom=0;view()};$("zoomin").onclick=()=>{zoom=Math.min(10,(zoom||1)*1.25);view()};$("zoomout").onclick=()=>{zoom=Math.max(.05,(zoom||1)/1.25);view()};$("rotate").onclick=()=>{angle=(angle+90)%360;view()};
for(const [id,fmt] of [["downloadOriginal","original"],["downloadJpeg","jpeg"],["downloadPdf","pdf"]])$(id).onclick=()=>{const quality=+$("quality").value;if(fmt==="jpeg"&&(!Number.isInteger(quality)||quality<1||quality>100)){alert("品質は1〜100");return}location.href="/api/download?"+new URLSearchParams({id:selected,tile:$("tile").value,format:fmt,quality,sampling:$("sampling").value})};
$("downloadZip").onclick=()=>{location.href="/api/zip?id="+selected};
async function poll(){
 try{const status=await api("/api/status");active=status.active;
 history=await api("/api/history");const existing=$("history").value;
 options("history",[["","画像を選択"],...history.map(j=>[j.id,j.created.slice(0,19)+" / "+j.plan.dpi+" dpi / "+j.state+" / "+j.results.length+"枚"])]);
 $("history").value=selected||existing||"";
 if(owned){const j=history.find(j=>j.id===owned);if(j&&j.state!=="running"){if(j.id===previewId&&j.results.length===1){$("overview").setAttribute("href","/api/image?id="+j.id+"&tile=1");previewId=null}
 if(selectionVersion===ownedVersion)selectJob(j.id);$("status").textContent=j.state+(j.error?": "+j.error:"");owned=null}}
 if(active){const j=history.find(j=>j.id===active);$("status").textContent="取得中 "+(j?.results.length||0)+" / "+(j?.plan.tiles.length||"?")}
 enabled()
 }catch(e){$("status").textContent=e.message}
 setTimeout(poll,800)
}
async function init(){cfg=await api("/api/config");$("backend").textContent=cfg.backend==="mock"?"MOCK — 実機未使用":"SANE — 実機";options("dpi",cfg.dpi.map(d=>[d,d+" dpi"+(d>2400?"（補間）":"")]));$("dpi").value=300;options("mode",cfg.modes.map(m=>[m,m]));
 for(let v=0;v<=210;v+=20)node("text",{x:v,y:-3,"font-size":4,"text-anchor":"middle",fill:"#42566a"},$("ticks")).textContent=v;
 for(let v=0;v<=291;v+=20)node("text",{x:-3,y:v+1,"font-size":4,"text-anchor":"end",fill:"#42566a"},$("ticks")).textContent=v;
 sync(roi);poll()}
init().catch(e=>$("status").textContent=e.message);
