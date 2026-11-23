function dialogueText(message) {
    if (!message || message.is_system || typeof message.mes !== 'string') return '';
    const extra = message.extra ?? {};
    if (extra.isSmallSys || extra.uses_system_ui || extra.type === 'narrator'
        || extra.tool_invocations?.length || extra.file || extra.files?.length || extra.fileLength > 0) return '';
    return message.mes.trim();
}

export async function exportHandoff(context, origin, count) {
    if (!Number.isInteger(count) || count < 1 || count > 3) throw new Error('请选择一至三轮对白。');
    const avatar = context.characters?.[context.characterId]?.avatar;
    const chatId = context.chatId || context.getCurrentChatId?.();
    if (context.groupId || typeof avatar !== 'string' || !avatar || typeof chatId !== 'string' || !chatId) {
        throw new Error('请先打开一个角色的单人聊天。');
    }

    // Copy the selected dialogue before hashing. The active chat can change during an await.
    const chat = context.chat;
    const turns = [];
    for (let i = chat.length - 2; i >= 0 && turns.length < count; i--) {
        const user = chat[i];
        const reply = chat[i + 1];
        if (user?.is_user !== true || !reply || reply.is_user === true) continue;
        const userText = dialogueText(user);
        const replyText = dialogueText(reply);
        if (!userText || !replyText) continue;
        if ([...userText].length > 2000 || [...replyText].length > 2000) {
            throw new Error('所选对白中有一条超过 2000 字。请减少导出轮数，或另选较短的对白。');
        }
        turns.unshift({ index: i, user_message: userText, assistant_reply: replyText });
    }
    if (!turns.length) throw new Error('当前聊天还没有可交接的完整文字问答。');
    if (!globalThis.crypto?.subtle) throw new Error('请通过本机 localhost 或 HTTPS 打开酒馆，再导出对白。');

    const identity = new TextEncoder().encode(JSON.stringify([origin, avatar, chatId]));
    const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', identity));
    const chatKey = Array.from(digest, byte => byte.toString(16).padStart(2, '0')).join('');
    return { format: 'xiaozhi-tavern-v1', chat_key: chatKey, turns };
}
