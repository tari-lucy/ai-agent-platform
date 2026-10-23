"""Dev stand: production host in-process + panel + OpenCode UI; Playwright capture."""
import asyncio, socket, sys, threading, time, os
from pathlib import Path
import httpx, uvicorn
from ai_agent_adapters.llm import OpenAICompatibleProvider
from playwright.sync_api import sync_playwright
from session_support import install
from test_activity import WorkModel, fast, prepared
from test_live import binding
from ai_agent_core.local_host import provider as providers
from ai_agent_core.local_host.layout import load_config, paths
from ai_agent_core.local_host.serve import build

ROOT = Path(sys.argv[1]); UI = Path(sys.argv[2]); TMP = Path(sys.argv[3]); WAIT = int(sys.argv[4])
class Patch:
    def setattr(self, obj, name, value): setattr(obj, name, value)
    def setenv(self, k, v): os.environ[k] = v
    def delenv(self, k, raising=False): os.environ.pop(k, None)
    def chdir(self, p): os.chdir(p)
mp = Patch()
model = WorkModel()
host = asyncio.run(prepared(TMP, mp, model))
wire = httpx.AsyncClient(transport=httpx.MockTransport(model))
providers.OpenAICompatibleProvider = lambda settings: OpenAICompatibleProvider(settings, wire)
listener = socket.socket(); listener.bind(("127.0.0.1", 0)); port = listener.getsockname()[1]
shared = {}
def worker():
    async def serve():
        app, close = build(paths(host.root), load_config(paths(host.root)), provider="live", port=port,
            panel_dir=ROOT / "apps/panel/dist", env_file=binding(TMP / "serve.env"), opencode_ui_dir=UI)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws="websockets"))
        shared.update(app=app, server=server, loop=asyncio.get_running_loop())
        try: await server.serve(sockets=[listener])
        finally: close()
    asyncio.run(serve())
threading.Thread(target=worker, daemon=True).start()
while not (shared.get("server") and shared["server"].started): time.sleep(0.05)
rt = shared["app"].state.runtime; fast(rt)
asyncio.run_coroutine_threadsafe(install(rt, host.scope), shared["loop"]).result(20)
origin = f"http://127.0.0.1:{port}"
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=os.environ["AI_PANEL_BROWSER_EXECUTABLE"])
    page = b.new_page()
    errors, net = [], []
    page.on("pageerror", lambda e: errors.append(str(e)[:300]))
    page.on("console", lambda m: errors.append("console:" + m.text[:300]) if m.type == "error" else None)
    page.on("response", lambda r: net.append(f"{r.status} {r.request.method} {r.url.replace(origin, '')} frame={r.frame.url.replace(origin, '')} t={time.time():.1f}") if r.status >= 400 else None)
    page.goto(origin + "/")
    page.get_by_label("Пароль", exact=True).fill(paths(host.root).password.read_text().strip())
    page.get_by_role("button", name="Войти", exact=True).click()
    page.get_by_role("button", name="START — запустить агента").wait_for(timeout=15000)
    print("--- auth after login", page.evaluate("async () => (await fetch('/api/v1/auth/session', {credentials:'same-origin'})).status"))
    print("--- goto opencode t=%.1f" % time.time())
    page.goto(origin + "/opencode/")
    page.wait_for_timeout(WAIT)
    print("--- from opencode page", page.evaluate("async () => [(await fetch('/api/v1/auth/session', {credentials:'same-origin'})).status, (await fetch('/api/v1/auth/session', {credentials:'same-origin', headers:{accept:'application/json'}})).status, document.cookie.length]"))
    for action in sys.argv[5:]:
        kind, _, arg = action.partition("=")
        if kind == "click": page.get_by_text(arg, exact=False).first.click(); page.wait_for_timeout(3000)
        if kind == "type": page.keyboard.type(arg); page.wait_for_timeout(500)
        if kind == "key": page.keyboard.press(arg); page.wait_for_timeout(4000)
        if kind == "shot": page.screenshot(path=arg)
        if kind == "reply": model.queues["activity"].append({"reply": arg})
        if kind == "panelstart":
            back = page.url
            page.goto(origin + "/")
            page.get_by_role("button", name="START — запустить агента").click()
            page.wait_for_timeout(2500)
            page.goto(back); page.wait_for_timeout(int(arg or 8000))
        if kind == "wait": page.wait_for_timeout(int(arg))
        if kind == "reload": page.reload(); page.wait_for_timeout(int(arg or 5000))
        if kind == "calls": print("--- model calls", model.count("activity"))
        if kind == "eval": print("--- eval", str(page.evaluate(arg))[:2500])
    seen = {}
    for row in page.evaluate("() => (globalThis.__awLog || [])"):
        k = row["method"] + " " + row["path"].split("?")[0]; seen[k] = seen.get(k, 0) + 1
    print("--- adapter non-200", sorted({(r["method"], r["path"].split("?")[0], r.get("status")) for r in page.evaluate("() => (globalThis.__awLog || [])") if r.get("status") not in (200, None)}))
    print("--- adapter routes"); [print(v, k) for k, v in sorted(seen.items())]
    print("--- http >= 400"); [print(n) for n in sorted(set(net))[:25]]
    print("--- errors"); [print(e) for e in errors[:12]]
    print("--- url", page.url)
    print("--- text", page.inner_text("body")[:900].replace("\n", " | "))
    print("--- storage", page.evaluate("() => [localStorage.length, sessionStorage.length]"))
    b.close()
shared["server"].should_exit = True
