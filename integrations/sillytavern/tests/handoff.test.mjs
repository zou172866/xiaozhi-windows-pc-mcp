import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';
import { exportHandoff } from '../handoff.js';

const origin = 'http://localhost:18081';
const user = mes => ({ is_user: true, mes });
const reply = mes => ({ is_user: false, mes });
const chat = {
    characterId: 0, characters: [{ avatar: 'companion.png' }], chatId: '旧书店',
    chat: [user('几点见？'), reply('三点。')],
};

test('uses the same canonical chat key across exports, distinct for branches and characters', async () => {
    const handoff = await exportHandoff(chat, origin, 1);
    const expectedKey = createHash('sha256').update(JSON.stringify([origin, 'companion.png', '旧书店'])).digest('hex');
    assert.deepEqual(handoff, {
        format: 'xiaozhi-tavern-v1', chat_key: expectedKey,
        turns: [{ index: 0, user_message: '几点见？', assistant_reply: '三点。' }],
    });
    const changed = await exportHandoff({ ...chat, chatId: '分支' }, origin, 1);
    const anotherChar = await exportHandoff({ ...chat, characters: [{ avatar: 'cat.png' }] }, origin, 1);
    assert.notEqual(expectedKey, changed.chat_key);
    assert.notEqual(expectedKey, anotherChar.chat_key);
    const olderContext = { ...chat, chatId: undefined, getCurrentChatId: () => '旧书店' };
    assert.equal((await exportHandoff(olderContext, origin, 1)).chat_key, expectedKey);
});

test('exports only adjacent completed dialogue and the selected swipe, preserving original indices', async () => {
    const messages = [
        reply('开场白'), user('很早'), reply('很早的回答'),
        user('旁白'), { ...reply('旁白内容'), extra: { type: 'narrator' } },
        user('附件'), { ...reply('附件内容'), extra: { fileLength: 4 } },
        user('系统'), { ...reply('系统文字'), is_system: true },
        user('选择'), { ...reply('选中'), swipes: ['舍弃', '选中'], extra: { reasoning: '隐含推理' } },
        user('工具'), { ...reply('工具调用'), extra: { tool_invocations: [{}] } },
        user('再问'), reply('再答'), user('未答'),
    ];
    const { turns } = await exportHandoff({ ...chat, chat: messages }, origin, 2);
    assert.deepEqual(turns, [
        { index: 9, user_message: '选择', assistant_reply: '选中' },
        { index: 13, user_message: '再问', assistant_reply: '再答' },
    ]);
});

test('bounds text by Unicode code points and rejects unsupported or incomplete chats', async () => {
    const limit = '🌙'.repeat(2000);
    const result = await exportHandoff({ ...chat, chat: [user(limit), reply('好')] }, origin, 3);
    assert.equal(result.turns[0].user_message, limit);
    await assert.rejects(exportHandoff({ ...chat, chat: [user(limit + '🌙'), reply('好')] }, origin, 1), /2000/);
    await assert.rejects(exportHandoff({ ...chat, groupId: 'group' }, origin, 1), /单人/);
    await assert.rejects(exportHandoff({ ...chat, chat: [reply('开场白')] }, origin, 1), /完整/);
    await assert.rejects(exportHandoff(chat, origin, 4), /三轮/);
});
