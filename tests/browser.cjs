const {chromium}=require("playwright");
const assert=require("node:assert/strict");
const {spawn}=require("node:child_process");
const {mkdtempSync,rmSync}=require("node:fs");
const {tmpdir}=require("node:os");
const {join}=require("node:path");
(async()=>{
 const root=mkdtempSync(join(tmpdir(),"scanbed-ui-"));
 const child=spawn("python",["server.py","--port","18766","--data",root],{stdio:"inherit"});
 let browser;
 try{
  for(let i=0;i<100;i++){try{if((await fetch("http://127.0.0.1:18766/api/config")).ok)break}catch{}await new Promise(r=>setTimeout(r,100))}
  browser=await chromium.launch({headless:true});
  const page=await browser.newPage({viewport:{width:1280,height:1000},acceptDownloads:true});
  await page.goto("http://127.0.0.1:18766/");
  await page.waitForFunction(()=>document.querySelector("#backend").textContent.includes("MOCK"));
  await page.selectOption("#dpi","2400");
  await page.waitForFunction(()=>document.querySelector("#hint").textContent.includes("分割"));
  assert.equal(await page.inputValue("#width"),"80.0");
  assert.equal(await page.inputValue("#height"),"60.0");
  assert.ok(await page.locator("#tiles rect").count()>1);
  await page.click("#fit");
  await page.waitForFunction(()=>document.querySelector("#hint").textContent.includes("一括可能"));
  await page.selectOption("#dpi","300");
  await page.fill("#width","10");await page.locator("#width").press("Tab");
  await page.fill("#height","12");await page.locator("#height").press("Tab");
  await page.waitForFunction(()=>!document.querySelector("#scan").disabled);
  assert.equal(await page.locator("#roi").getAttribute("width"),"10");
  await page.locator("#bed").focus();await page.keyboard.press("ArrowRight");
  assert.equal(await page.inputValue("#left"),"5.1");
  await page.selectOption("#edit","fixed");
  assert.equal(await page.locator("#handles rect").count(),0);
  const coords=await page.locator("#bed").evaluate(el=>{const m=el.getScreenCTM();const p=new DOMPoint(50,50).matrixTransform(m);return [p.x,p.y]});
  await page.mouse.click(...coords);
  assert.equal(await page.inputValue("#left"),"45.0");
  assert.equal(await page.inputValue("#width"),"10.0");
  await page.waitForFunction(()=>!document.querySelector("#scan").disabled);
  await page.click("#scan");
  await page.waitForFunction(()=>!document.querySelector("#downloadJpeg").disabled&&document.querySelector("#result").naturalWidth>0);
  assert.equal(await page.locator("#result").evaluate(el=>el.naturalWidth),118);
  const download=page.waitForEvent("download");await page.click("#downloadJpeg");
  const d=await download;assert.equal(d.suggestedFilename(),"tile-001.jpg");assert.equal(await d.failure(),null);
  const selected=await page.inputValue("#history");
  await page.waitForTimeout(1100);
  assert.equal(await page.inputValue("#history"),selected);
  console.log("Browser ROI editing, split visualization, keyboard, fixed position, scan and JPEG download passed");
 }finally{if(browser)await browser.close();child.kill();await new Promise(r=>child.once("exit",r));rmSync(root,{recursive:true,force:true})}
})().catch(e=>{console.error(e);process.exitCode=1});
