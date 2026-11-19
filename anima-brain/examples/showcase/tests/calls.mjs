import {checkArchitecture} from './architecture.mjs';
import assert from 'node:assert/strict';
import {resolve} from 'node:path';
import AxeBuilder from '@axe-core/playwright';

// Offline protocol fixture; real provider testing is opt-in and documented separately.
export async function checkCalls(browser,{privateUrl,output}) {
 const context=await browser.newContext({viewport:{width:390,height:844},permissions:['microphone','camera']});
 await context.addInitScript(()=>{
  sessionStorage.setItem('companion-character','rina-preview');
  window.__tracks=[];
  const get=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  navigator.mediaDevices.getUserMedia=async (...args)=>{const stream=await get(...args);window.__tracks.push(...stream.getTracks());return stream;};
 });
 await context.route('**/api/bootstrap',async route=>{
  const response=await route.fetch();const body=await response.json();
  body.capabilities.voice_call=true;body.capabilities.video_call=true;
  body.characters.forEach(c=>c.voice_id='fixture-voice');
  await route.fulfill({response,json:body});
 });
 const page=await context.newPage(),commands=[];let frames=0;
 await page.routeWebSocket('**/api/call',ws=>{
  ws.onMessage(raw=>{
   if(typeof raw!=='string'){frames++;return;}
   const msg=JSON.parse(raw);commands.push(msg.type||'auth');
   if(msg.token){
    assert(!ws.url().includes(msg.token));
    ws.send(JSON.stringify({type:'state',data:{phase:'listening'},sequence:0}));
    ws.send(JSON.stringify({type:'transcript',data:{speaker:'assistant',text:'Hey. I’m here.'},sequence:1}));
    ws.send(JSON.stringify({type:'state',data:{phase:'speaking'},sequence:1}));
    const audio=Buffer.alloc(4804);audio.writeUInt32LE(1,0);ws.send(audio);
   }
   if(msg.type==='hangup')ws.close();
  });
 });
 try {
  await page.goto(privateUrl);
  await page.getByRole('button',{name:'Start voice call',exact:true}).click();
  await page.getByRole('dialog').getByRole('button',{name:'Start voice call',exact:true}).click();
  await page.getByText('Hey. I’m here.',{exact:true}).waitFor();
  await page.waitForTimeout(500);
  assert(frames>0,'AudioWorklet must send real PCM frames');
  await page.getByRole('button',{name:'Mute microphone',exact:true}).click();
  await page.getByRole('button',{name:'Unmute microphone',exact:true}).click();
  await page.getByRole('button',{name:'Interrupt reply',exact:true}).click();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  const a11y=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
  assert.deepEqual(a11y.violations,[],'Call accessibility');
  await page.screenshot({path:resolve(output,'voice-call-mobile.png')});
  await page.getByRole('button',{name:'End call',exact:true}).click();
  await page.waitForFunction(()=>window.__tracks.length>0&&window.__tracks.every(t=>t.readyState==='ended'));
  assert(commands.includes('mute')&&commands.includes('interrupt')&&commands.includes('hangup'));
  await page.getByRole('button',{name:'Back to conversation'}).click();
  await page.getByRole('dialog').waitFor({state:'hidden'});
  // Permission rejection must not create another server call.
  await page.evaluate(()=>navigator.mediaDevices.getUserMedia=async()=>{throw new DOMException('Microphone permission denied','NotAllowedError');});
  await page.getByRole('button',{name:'Start voice call',exact:true}).click();
  await page.getByRole('dialog').getByRole('button',{name:'Start voice call',exact:true}).click();
  await page.getByRole('alert').filter({hasText:'Microphone permission denied'}).waitFor();
  assert.equal(commands.filter(c=>c==='auth').length,1);
 }catch(e){await page.screenshot({path:resolve(output,'call-fixture-failure.png')});console.error('Call fixture screen:',(await page.locator('body').innerText()).slice(0,1600));throw e;}finally{await context.close();}
 await checkArchitecture(browser,{privateUrl,output});
 return {pcmFrames:frames,mute:true,interrupt:true,hangup:true,permissionDenial:true,tracksStopped:true,architectureResponsive:true};
}
