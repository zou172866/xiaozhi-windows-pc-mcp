"""Local MCP Apps host simulator. Synthetic tone only; no keys or real-user DB.

Run: .venv/bin/python examples/player_preview.py
Open http://127.0.0.1:8877. Requires ffmpeg to create a 2-second fixture.
This verifies browser/bridge mechanics, NOT the actual ChatGPT host integration.
"""

import argparse
import json
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from companion_engine import CharacterSpec, Engine
from companion_engine.media import Media
from companion_engine.plugin_support import player_html, voice_card, voice_chunk
from companion_engine.runtime import Runtime, Settings

HOST_HTML = """<!doctype html><html lang="en"><meta charset="utf-8">
<title>Companion player · local QA</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font-family:system-ui;margin:24px;background:#fff;color:#27382b}
main{max-width:700px;margin:60px auto}iframe{width:100%;height:340px;border:0}
small{color:#60765f}h1{font-size:22px}button{padding:10px;margin:10px 4px}</style>
<main><small>LOCAL VERIFICATION · SYNTHETIC AUDIO ONLY</small>
<h1>A voice note, inside the conversation.</h1>
<p>This is a test host, not ChatGPT. Playback has no API cost.</p>
<iframe id="player" title="Voice message player" src="/player"></iframe>
<div><button id="theme">Dark / light</button><button id="locale">中文 / English</button>
<button id="width">Mobile / desktop</button><button id="replay">Re-render card</button>
<button id="failure">Fail next audio read</button></div>
<p id="counter">Audio reads: 0 · generation calls: 0</p></main><script>
const iframe=document.getElementById('player');let reads=0,fail=false;
const send=m=>iframe.contentWindow.postMessage({jsonrpc:'2.0',...m},location.origin);
const render=async()=>send({method:'ui/notifications/tool-result',
 params:await(await fetch('/card')).json()});
let dark=false,chinese=false,mobile=false;
document.getElementById('theme').onclick=()=>{dark=!dark;
 document.body.style.background=dark?'#181818':'#fff';document.body.style.color=dark?'#eee':'#27382b';
 send({method:'ui/notifications/host-context-changed',params:{theme:dark?'dark':'light'}})};
document.getElementById('locale').onclick=()=>{chinese=!chinese;
 send({method:'ui/notifications/host-context-changed',params:{locale:chinese?'zh-CN':'en-US'}})};
document.getElementById('width').onclick=()=>{mobile=!mobile;iframe.style.width=mobile?'280px':'100%'};
document.getElementById('replay').onclick=render;
document.getElementById('failure').onclick=()=>{fail=true};
window.addEventListener('message',async e=>{
 if(e.source!==iframe.contentWindow||e.origin!==location.origin)return;
 const m=e.data;if(m?.jsonrpc!=='2.0')return;
 if(m.method==='ui/initialize')send({id:m.id,result:{protocolVersion:m.params.protocolVersion,
  hostInfo:{name:'Local QA',version:'1.0'},hostCapabilities:{serverTools:{}},
  hostContext:{theme:'light',displayMode:'inline'}}});
 else if(m.method==='ui/notifications/initialized')render();
 else if(m.method==='ui/request-display-mode'){
  send({id:m.id,result:{mode:m.params.mode}});
  send({method:'ui/notifications/host-context-changed',params:{displayMode:m.params.mode}});
 }
 else if(m.method==='tools/call'){
  reads++;
  document.getElementById('counter').textContent=`Audio reads: ${reads} · generation calls: 0`;
  if(fail){fail=false;send({id:m.id,result:{
   content:[{type:'text',text:'Synthetic failure'}],isError:true}});return}
  const result=await(await fetch('/chunk',{method:'POST',
   headers:{'Content-Type':'application/json'},body:JSON.stringify(m.params)})).json();
  send({id:m.id,result});
 }
});</script></html>"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8877)
    args = parser.parse_args()
    tone = subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-f",
            "mp3",
            "-b:a",
            "64k",
            "pipe:1",
        ],
        timeout=20,
    )
    with tempfile.TemporaryDirectory(prefix="companion-player-qa-") as directory:
        with Engine(Path(directory) / "preview.sqlite3") as engine:
            engine.create_character(CharacterSpec(id="nova", name="Nova", persona="Synthetic QA"))
            runtime = Runtime(engine, Settings())
            media = Media(runtime)
            result = runtime.operation(
                "nova",
                "speech",
                "fixture",
                {},
                lambda: media._save(tone, "audio/mpeg", "mp3", {"network_calls": 0}),
            )

            class Handler(BaseHTTPRequestHandler):
                def reply(self, data, mime="application/json", code=200):
                    body = data.encode() if isinstance(data, str) else json.dumps(data).encode()
                    self.send_response(code)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)

                def do_GET(self):
                    if self.path == "/":
                        self.reply(HOST_HTML, "text/html; charset=utf-8")
                    elif self.path == "/player":
                        self.reply(player_html(), "text/html; charset=utf-8")
                    elif self.path == "/card":
                        self.reply(
                            voice_card(media, "nova", result["artifact"]).model_dump(
                                mode="json", by_alias=True, exclude_none=True
                            )
                        )
                    else:
                        self.reply({}, code=404)

                def do_POST(self):
                    try:
                        size = int(self.headers.get("Content-Length", "0"))
                        if self.path != "/chunk" or not 0 < size <= 8192:
                            raise ValueError()
                        data = json.loads(self.rfile.read(size))
                        if data["name"] != "read_voice_chunk":
                            raise ValueError()
                        chunk = voice_chunk(media, **data["arguments"])
                        self.reply(chunk.model_dump(mode="json", by_alias=True, exclude_none=True))
                    except (KeyError, TypeError, ValueError, OSError):
                        self.reply(
                            {"isError": True, "content": [{"type": "text", "text": "Invalid read"}]}
                        )

                def log_message(self, *_):
                    pass

            print(f"Synthetic player preview: http://127.0.0.1:{args.port}", flush=True)
            with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
                server.serve_forever()


if __name__ == "__main__":
    main()
