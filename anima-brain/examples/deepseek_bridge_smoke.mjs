// Optional integration check with the real Harness bridge; no model/API request.
// node examples/deepseek_bridge_smoke.mjs <npm-install-prefix> <companion.cordis.yml>
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [prefix, overlay] = process.argv.slice(2);
if (!prefix || !overlay) throw new Error('Expected npm prefix and exported overlay path');
const require = createRequire(join(resolve(prefix), 'package.json'));
const load = (name) => import(pathToFileURL(require.resolve(name)).href);
const { Context } = await load('@deepseek-ai/cordis');
const { default: SystemPrompt } = await load('@deepseek-ai/dsh-system-prompt');
const { default: ToolsService } = await load('@deepseek-ai/dsh-tools');
const bridge = await load('@deepseek-ai/dsh-mcp-client');
const config = JSON.parse(await readFile(overlay, 'utf8'))[0].insert[0].config;
const dir = await mkdtemp(join(tmpdir(), 'companion-harness-'));
config.args[config.args.indexOf('--db') + 1] = join(dir, 'brain.sqlite3');
config.env = { ...config.env, COMPANION_SETTINGS_FILE: join(dir, 'providers.json'),
    COMPANION_SECRET_STORE: 'file' };
config.failOnStartupError = true;
config.reconnect = { enabled: false };
// Do not inherit an optional paid provider during this offline check.
for (const key of Object.keys(process.env)) {
    if (key.startsWith('COMPANION_')) delete process.env[key];
}

async function connect() {
    const ctx = new Context();
    try {
        await ctx.plugin(SystemPrompt, {}).await();
        await ctx.plugin(ToolsService, {}).await();
        await ctx.plugin(bridge, config).await();
        return ctx;
    } catch (error) {
        await ctx.fiber.dispose();
        throw error;
    }
}
async function call(ctx, name, args) {
    const tool = ctx.tools.get(`mcp__companion_engine__${name}`);
    assert(tool, `Missing registered tool: ${name}`);
    const response = await tool.execute(args, { signal: AbortSignal.timeout(20000) });
    return response.structuredContent ?? JSON.parse(response.content[0].text);
}

let ctx;
try {
    ctx = await connect();
    const toolCount = ctx.tools.definitions().length;
    await call(ctx, 'get_companion_workflow', {});
    await call(ctx, 'create_character', { spec: {
        id: 'smoke', name: 'Smoke', persona: 'A fictional test companion',
    } });
    const turn = await call(ctx, 'prepare_turn', {
        character_id: 'smoke', user_message: 'Please remember I prefer tea', request_id: 'bridge-1',
    });
    const request = { character_id: 'smoke', turn_id: turn.turn_id, request: {
        assistant_reply: 'Tea, got it.',
        attunement: { intent: 'answer', user_cue: 'Memory request', source_quote: 'Please remember' },
        appraisal: { emotion: 'warm', warmth: 1, source_quote: 'Please remember' },
        memories: [{ key: 'drink', text: 'Prefers tea', category: 'preference',
            source_quote: 'I prefer tea' }],
    } };
    const saved = await call(ctx, 'commit_turn', request);
    assert.equal(saved.memory_ids.length, 1);
    await ctx.fiber.dispose();
    ctx = await connect();
    assert.equal((await call(ctx, 'commit_turn', request)).replayed, true);
    const recalled = await call(ctx, 'search_memory', { character_id: 'smoke', query: 'tea' });
    assert(JSON.stringify(recalled).includes('Prefers tea'));
    console.log(JSON.stringify({ toolCount, write: true, freshSessionRecall: true,
        idempotentReplay: true, modelInferenceTested: false }));
} finally {
    if (ctx) await ctx.fiber.dispose();
    await rm(dir, { recursive: true, force: true });
}
