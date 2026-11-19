import { App } from '@modelcontextprotocol/ext-apps';
import { applyHostTheme } from './host-theme.js';
const app = new App({name:'Companion Settings',version:'1.0.0'});
const $=id=>document.getElementById(id);
let data, busy=false, ctx={};
const t=(en,zh)=>/^zh/i.test(ctx.locale||navigator.language)?zh:en;
function render(){
 $('title').textContent=t('Connections','服务连接');
 $('intro').textContent=t('Configure providers on the computer running your engine. No API keys are entered or stored in this card.','在运行 Engine 的电脑上配置服务。此卡片不输入或保存 API key。');
 $('open').textContent=t('Open local settings','打开本机设置');$('refresh').textContent=t('Refresh','刷新');
 $('open').disabled=$('refresh').disabled=busy||!data;
 $('providers').replaceChildren();
 if(!data)return;
 for(const [key,en,zh] of [['speech','Voice','语音'],['embedding','Embeddings','向量索引'],['images','Images','图片'],['reply_model','Standalone reply model','独立聊天模型']]){
  const row=document.createElement('div');row.className='provider';
  const label=document.createElement('span');label.textContent=t(en,zh);
  const state=document.createElement('span');state.textContent=data[key]?t('Configured','已配置'):t('Not configured','未配置');row.append(label,state);$('providers').append(row);
 }
 $('storage').textContent=data.storage==='macos_keychain'?t('Saved keys: macOS Keychain.','密钥保存位置：macOS 钥匙串。'):data.storage==='private_file'?t('Saved keys: private local file. Save again on macOS to use Keychain.','密钥保存位置：本地私有文件。macOS 重新保存后使用钥匙串。'):t('No saved keys.','尚无已保存密钥。');
}
async function call(name){const r=await app.callServerTool({name,arguments:{}});if(r.isError)throw Error();return r.structuredContent;}
$('open').onclick=async()=>{if(busy)return;busy=true;render();try{await call('open_local_provider_settings');$('status').textContent=t('Settings opened on the engine computer, even if you are chatting on a phone. Save there, then refresh here.','设置已在 Engine 所在电脑打开，即使你正在用手机聊天。在电脑保存后，点击这里的刷新。');}catch{$('status').textContent=t('Could not open settings. On the engine computer, run companion web --open.','无法打开设置。请在 Engine 所在电脑运行 companion web --open。');}finally{busy=false;render();}};
$('refresh').onclick=async()=>{if(busy)return;busy=true;render();try{data=await call('refresh_provider_settings');$('status').textContent=t('Updated.','已更新。');}catch{$('status').textContent=t('Could not refresh. Try again.','刷新失败，请重试。');}finally{busy=false;render();}};
app.ontoolresult=r=>{if(r.structuredContent?.opens_on){data=r.structuredContent;render();}};
function host(c){ctx={...ctx,...c};applyHostTheme(c);document.documentElement.lang=/^zh/i.test(ctx.locale||navigator.language)?'zh':'en';render();}
app.onhostcontextchanged=host;
await app.connect();host(app.getHostContext());
