import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
import {resolve} from 'node:path';
import AxeBuilder from '@axe-core/playwright';

export async function checkArchitecture(browser,{privateUrl,output}) {
 const source=JSON.parse(await readFile(new URL('../../../docs/assets/brain-flow.json',import.meta.url),'utf8'));
 const chinese=JSON.parse(await readFile(new URL('../../../docs/assets/brain-flow.zh-CN.json',import.meta.url),'utf8'));
 assert.equal(chinese.version,source.version,'Locale and graph schema must match');
 assert.deepEqual(Object.keys(chinese.views),source.views.map(v=>v.id));
 for(const view of source.views){
  const translation=chinese.views[view.id];
  assert.deepEqual(Object.keys(translation.nodes),view.nodes.map(n=>n.id));
  assert.equal(translation.steps.length,view.steps.length);
  for(const node of Object.values(translation.nodes))assert.deepEqual(Object.keys(node),['title','subtitle','tag']);
  for(const step of translation.steps)assert.deepEqual(Object.keys(step),['label','title','text','example','detail']);
 }
 for(const language of ['en','zh-CN']) {
 const t=text=>language==='zh-CN'?(chinese.ui[text]??text):text;
 const chapters=source.views.map(v=>language==='zh-CN'?chinese.views[v.id].label:v.label);
 for(const width of [390,768,1024,1440]) {
  const ctx=await browser.newContext({viewport:{width,height:1000},reducedMotion:'reduce'});
  const page=await ctx.newPage();const requests=[];
  page.on('request',r=>{if(new URL(r.url()).pathname.startsWith('/api/'))requests.push(r.url());});
  try {
   await page.goto(new URL(`/?view=architecture&lang=${language}`,privateUrl).href);
   await page.getByRole('heading',{name:language==='zh-CN'?/每一次交流，.*都接得上以后。/:'A moment becomes continuity.'}).waitFor();
   await page.evaluate(()=>document.fonts.ready);
   for(let i=0;i<chapters.length;i++) {
    await page.getByRole('navigation',{name:t('Architecture views')}).getByRole('button',{name:new RegExp(chapters[i].replace('&','&'))}).click();
    await page.getByRole('button',{name:t('Next step'),exact:true}).click();
    assert(await page.getByRole('button',{name:t('Play animation'),exact:true}).isDisabled());
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`overflow ${width} / ${chapters[i]}`);
    // Every label fits its node even before scaling; prevents small-screen clipped words.
    const clipped=await page.locator('.arch-svg-scroll svg').evaluate(svg=>Array.from(svg.querySelectorAll('.atlas-node-sub,.atlas-node-title,.atlas-node-tag')).flatMap(text=>{
     const box=text.getBBox(),rect=text.parentElement.querySelector('rect');
     return rect && box.x+box.width>Number(rect.getAttribute('x'))+Number(rect.getAttribute('width'))-6?[text.textContent]:[];
    }));
    assert.deepEqual(clipped,[],`Clipped diagram labels: ${chapters[i]}`);
    const result=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
    assert.deepEqual(result.violations.map(v=>({id:v.id,nodes:v.nodes.map(n=>({target:n.target,summary:n.failureSummary}))})),[],`Architecture a11y ${width}/${chapters[i]}`);
    if(i===1) {
     assert.equal(await page.getByTestId('rrf-score').innerText(),'0.03202');
     await page.getByLabel(t('Include vector match')).uncheck();
     assert.equal(await page.getByTestId('rrf-score').innerText(),'0.01639');
     const age=page.getByRole('slider',{name:t('Age since update')});
     await age.focus();await age.press('Home');
     assert.equal(await page.getByTestId('loop-score').innerText(),'11.50');
     await page.getByLabel(t('Has an event date')).uncheck();
     assert.equal(await page.getByTestId('loop-score').innerText(),'10.50');
     await age.press('End');
     assert.equal(await page.getByTestId('loop-score').innerText(),'0.47');
    }
    if(i===3) {
     assert.equal(await page.getByTestId('pad-remaining').innerText(),'50.0%');
     assert.equal(await page.getByTestId('tension-remaining').innerText(),'89.1%');
     const hours=page.getByRole('slider',{name:t('Hours since state update')});
     await hours.focus();await hours.press('Home');
     assert.equal(await page.getByTestId('pad-remaining').innerText(),'100.0%');
     for(let j=0;j<72;j++)await hours.press('ArrowRight');
     assert.equal(await page.getByTestId('pad-remaining').innerText(),'1.6%');
     assert.equal(await page.getByTestId('tension-remaining').innerText(),'50.0%');
    }
    await page.screenshot({path:resolve(output,`architecture-${language}-${width}-${i}.png`),fullPage:true});
   }
   assert.equal(await page.locator('html').getAttribute('lang'),language);
   // Switch in place, preserve the selected chapter, and restore via browser history.
   const other=language==='en'?'zh-CN':'en';
   await page.getByRole('navigation',{name:t('Architecture language')}).getByRole('link',{name:other==='en'?'English':'简体中文',exact:true}).click();
   assert.equal(new URL(page.url()).searchParams.get('lang'),other);
   assert.equal(await page.locator('html').getAttribute('lang'),other);
   assert.equal(await page.getByTestId('atlas-chapters').getByRole('button').nth(3).getAttribute('aria-pressed'),'true');
   await page.goBack();
   assert.equal(await page.locator('html').getAttribute('lang'),language);
   assert.deepEqual(requests,[],'Architecture must not access private API');
  }finally {await ctx.close();}
 }
 }
 const ctx=await browser.newContext({viewport:{width:1440,height:1000}});const page=await ctx.newPage();
 try {
  await page.goto(new URL('/?view=architecture',privateUrl).href);
  await page.getByRole('button',{name:'Play animation',exact:true}).click();
  await page.getByRole('heading',{name:'Retrieve context, with its sources.'}).waitFor({timeout:9000});
  await page.getByRole('button',{name:'Pause animation',exact:true}).click();
  await page.getByRole('button',{name:'Restart explanation',exact:true}).click();
  await page.getByRole('heading',{name:'Begin with what was actually said.'}).waitFor();
  await page.getByRole('button',{name:'Previous step',exact:true}).click();
  await page.getByRole('heading',{name:'Two loops feed the next reply.'}).waitFor();
 }finally {await ctx.close();}
 return {languages:2,views:4,viewports:4,a11y:true,keyboardFormulas:true,noPrivateAPI:true,reducedMotion:true,playback:true};
}
