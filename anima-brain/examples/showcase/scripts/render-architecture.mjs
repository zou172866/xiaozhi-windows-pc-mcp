/** Capture all four atlas chapters. Static public page; no private API or model calls. */
import { chromium } from '@playwright/test';
import { mkdir, readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
const repo=resolve(dirname(fileURLToPath(import.meta.url)),'../../..');
const args=process.argv.slice(2);
const languageIndex=args.indexOf('--lang');
const language=languageIndex>=0?args[languageIndex+1]:'en';
if(!['en','zh-CN'].includes(language))throw new Error('--lang must be en or zh-CN');
if(languageIndex>=0)args.splice(languageIndex,2);
if(args.length>1)throw new Error('Usage: render-architecture.mjs [showcase URL] [--lang en|zh-CN]');
const suffix=language==='zh-CN'?'.zh-CN':'';
const output=resolve(repo,`output/brain-animation-${language}`);
const flow=JSON.parse(await readFile(resolve(repo,'docs/assets/brain-flow.json'),'utf8'));
await mkdir(output,{recursive:true});
const chrome='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const browser=await chromium.launch({headless:true,...(existsSync(chrome)?{executablePath:chrome}:{})});
let frame=0;
try {
 const page=await browser.newPage({viewport:{width:1440,height:1200}});
 const url=new URL(args[0]||'http://127.0.0.1:8787');url.search=`?view=architecture&lang=${language}`;url.hash='';
 await page.goto(url.href);await page.evaluate(()=>document.fonts.ready);
 await page.screenshot({path:resolve(output,'architecture.png'),fullPage:true});
 for(const [index,view] of flow.views.entries()){
  await page.getByTestId('atlas-chapters').getByRole('button').nth(index).click();
  for(let step=0;step<view.steps.length;step++){
   if(step)await page.getByTestId('atlas-next').click();
   for(let j=0;j<4;j++){
    await page.locator('.arch-map').screenshot({path:resolve(output,`${String(frame++).padStart(3,'0')}.png`)});
    await page.waitForTimeout(220);
   }
  }
 }
}finally{await browser.close();}
const result=spawnSync('ffmpeg',['-v','error','-y','-framerate','3','-i',resolve(output,'%03d.png'),'-frames:v',String(frame),
 '-filter_complex','[0:v]scale=960:-1:flags=lanczos,pad=960:ceil(ih/2)*2:color=0xf7f5ee,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=3',
 '-loop','0',resolve(repo,`docs/assets/brain-flow${suffix}.gif`)],{stdio:'inherit'});
if(result.status!==0)throw new Error(`GIF render failed. Install ffmpeg; PNG frames remain in ${output}.`);
console.log(`Saved docs/assets/brain-flow${suffix}.gif (${frame} frames; ${(frame/3).toFixed(1)}s)`);
