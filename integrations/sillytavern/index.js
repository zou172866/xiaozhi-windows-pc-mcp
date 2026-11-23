import { exportHandoff } from './handoff.js';

function downloadHandoff(handoff) {
    const file = new Blob([JSON.stringify(handoff)], { type: 'application/json;charset=utf-8' });
    const url = URL.createObjectURL(file);
    const link = document.createElement('a');
    link.download = 'xiaozhi-tavern.json';
    link.href = url;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const panel = document.createElement('section');
panel.id = 'xiaozhi-tavern-handoff';
panel.innerHTML = `
    <h4>用小智继续</h4>
    <label>导出最近的完整文字问答
        <select><option value="1">1 轮</option><option value="2">2 轮</option><option value="3">3 轮</option></select>
    </label>
    <button type="button" class="menu_button">导出对白文件</button>
    <p>文件包含所选对白的原文。下载后，打开<a href="http://localhost:18080/" target="_blank" rel="noopener noreferrer">小智控制台</a>的「对话记忆存档 → 从酒馆接续聊天」。</p>
    <p role="status" aria-live="polite"></p>
`;
const button = panel.querySelector('button');
const status = panel.querySelector('[role="status"]');
button.addEventListener('click', async () => {
    button.disabled = true;
    try {
        if (document.querySelector('#mes_stop')?.getClientRects().length) {
            throw new Error('等当前回复生成完，再导出对白。');
        }
        const context = SillyTavern.getContext();
        const handoff = await exportHandoff(context, location.origin, Number(panel.querySelector('select').value));
        downloadHandoff(handoff);
        status.textContent = `已开始下载 ${handoff.turns.length} 轮对白。文件名为 xiaozhi-tavern.json。`;
    } catch (error) {
        status.textContent = error.message || '导出失败，请重新选择聊天。';
    } finally {
        button.disabled = false;
    }
});
document.querySelector('#extensions_settings2').append(panel);
