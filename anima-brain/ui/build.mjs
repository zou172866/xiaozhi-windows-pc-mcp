import { build } from "esbuild";
import { readFile, writeFile } from "node:fs/promises";
const bundle = await build({
  entryPoints: [new URL("./voice-player.js", import.meta.url).pathname],
  bundle: true, minify: true, format: "esm", platform: "browser", write: false,
  metafile: true,
});
const hostTheme = await readFile(new URL("./host-theme.css", import.meta.url), "utf8");
const template = await readFile(new URL("./voice-player.html", import.meta.url), "utf8");
const script = bundle.outputFiles[0].text.replace(/<\/script/gi, "<\\/script");
await writeFile(new URL("../src/companion_engine/static/voice-player.html", import.meta.url),
  template.replace("/* HOST_THEME */", () => hostTheme).replace("/* BUNDLED_PLAYER */", () => script));
// Ship the licenses for bundled dependencies with the prebuilt Python asset.
const packages = new Set(Object.keys(bundle.metafile.inputs).flatMap((path) => {
  const match = path.match(/node_modules\/((?:@[^/]+\/)?[^/]+)\//);
  return match ? [match[1]] : [];
}));
const notices = [];
for (const name of [...packages].sort()) {
  const root = new URL(`./node_modules/${name}/`, import.meta.url);
  const info = JSON.parse(await readFile(new URL('package.json', root), 'utf8'));
  let license;
  for (const file of ['LICENSE', 'LICENSE.md', 'LICENSE.txt']) {
    try { license = await readFile(new URL(file, root), 'utf8'); break; } catch {}
  }
  if (!license) throw new Error(`Missing bundled license: ${name}`);
  notices.push(`${name} ${info.version}\n${'='.repeat(60)}\n${license}`);
}
await writeFile(new URL('../src/companion_engine/static/voice-player-notices.txt', import.meta.url),
  notices.join('\n\n'));

const memoryBundle = await build({
  entryPoints: [new URL('./memory-panel.js', import.meta.url).pathname],
  bundle: true, minify: true, format: 'esm', platform: 'browser', write: false,
});
const memoryTemplate = await readFile(new URL('./memory-panel.html', import.meta.url), 'utf8');
await writeFile(new URL('../src/companion_engine/static/memory-panel.html', import.meta.url),
  memoryTemplate.replace('/* HOST_THEME */', () => hostTheme).replace('/* BUNDLED_MEMORY */', () => memoryBundle.outputFiles[0].text.replace(/<\/script/gi, '<\\/script')));
const settingsBundle = await build({
  entryPoints: [new URL('./provider-settings.js', import.meta.url).pathname],
  bundle:true,minify:true,format:'esm',platform:'browser',write:false,
});
const settingsTemplate = await readFile(new URL('./provider-settings.html',import.meta.url),'utf8');
await writeFile(new URL('../src/companion_engine/static/provider-settings.html',import.meta.url),
  settingsTemplate.replace('/* HOST_THEME */',()=>hostTheme).replace('/* BUNDLED_SETTINGS */',()=>settingsBundle.outputFiles[0].text.replace(/<\/script/gi,'<\\/script')));
