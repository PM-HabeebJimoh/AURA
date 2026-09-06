"""Flask JSON API + single-page web UI for the PM Engine.

Endpoints (all JSON):

    GET  /api/health                      engine status, provider, counts
    GET  /api/plugins                     all plugins
    GET  /api/plugins/<name>              one plugin (+ README)
    GET  /api/skills[?plugin=]            all skills (front matter only)
    GET  /api/skills/<ref>                one skill with body (ref = name or plugin:name)
    GET  /api/commands[?plugin=]          all commands
    GET  /api/commands/<ref>              one command with body + parsed workflow
    GET  /api/search?q=&kind=&limit=      BM25 search
    GET  /api/graph                       command→skill graph
    POST /api/resolve   {text}            routing preview (no model call)
    POST /api/prompt    {text, step?}     assembled prompt (no model call)
    POST /api/run       {text, session_id?, step?, attachments?: [{name,text}], skills?: []}
    POST /api/stream    same body as /api/run; Server-Sent Events: `chunk` events then one `result` event
    GET  /api/sessions                    saved sessions
    GET  /api/sessions/<id>               one session (turns)
    DELETE /api/sessions/<id>
    GET  /api/validate                    spec + engine validation report
    GET  /api/providers                   configured providers
    GET  /                                web UI

The UI is a static page (no build step) that talks to the API with relative
URLs so it works behind any reverse proxy / preview host.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

from . import __version__
from .prompt import Attachment
from .providers import ProviderError, available_providers
from .registry import RegistryError
from .runner import Engine
from .session import Session
from .validator import validate_all
from .workflow import parse_workflow, skill_graph

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(engine: Engine | None = None) -> Flask:
    engine = engine or Engine(artifacts_dir=os.environ.get("PM_ENGINE_ARTIFACTS"))
    app = Flask(__name__, static_folder=None)
    app.config["JSON_SORT_KEYS"] = False
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
    graph_cache: dict = {}

    # -- errors ----------------------------------------------------------

    @app.errorhandler(RegistryError)
    def _reg_err(e):  # type: ignore[no-untyped-def]
        return jsonify({"error": str(e)}), 404

    @app.errorhandler(ProviderError)
    def _prov_err(e):  # type: ignore[no-untyped-def]
        return jsonify({"error": str(e), "kind": "provider"}), 502

    @app.errorhandler(ValueError)
    def _val_err(e):  # type: ignore[no-untyped-def]
        return jsonify({"error": str(e)}), 400

    @app.after_request
    def _headers(resp: Response) -> Response:
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,DELETE,OPTIONS"
        resp.headers["X-PM-Engine"] = __version__
        return resp

    # -- meta ------------------------------------------------------------

    @app.get("/api/health")
    def health():  # type: ignore[no-untyped-def]
        st = engine.registry.stats()
        return jsonify({"ok": True, "version": __version__, "provider": engine.provider.name, "model": engine.provider.model, **st})

    @app.get("/api/providers")
    def providers():  # type: ignore[no-untyped-def]
        return jsonify({"active": {"provider": engine.provider.name, "model": engine.provider.model}, "available": available_providers()})

    @app.get("/api/validate")
    def validate():  # type: ignore[no-untyped-def]
        return jsonify(validate_all(engine.registry).to_dict())

    # -- catalogue -------------------------------------------------------

    @app.get("/api/plugins")
    def plugins():  # type: ignore[no-untyped-def]
        return jsonify([p.to_dict() for p in engine.registry.plugins.values()])

    @app.get("/api/plugins/<name>")
    def plugin(name: str):  # type: ignore[no-untyped-def]
        p = engine.registry.get_plugin(name)
        d = p.to_dict()
        d["readme"] = p.readme
        d["skills"] = [s.to_dict() for s in p.skills.values()]
        d["commands"] = [c.to_dict() for c in p.commands.values()]
        return jsonify(d)

    @app.get("/api/skills")
    def skills():  # type: ignore[no-untyped-def]
        plugin_f = request.args.get("plugin")
        return jsonify([s.to_dict() for s in engine.registry.skills if not plugin_f or s.plugin == plugin_f])

    @app.get("/api/skills/<path:ref>")
    def skill(ref: str):  # type: ignore[no-untyped-def]
        s = engine.registry.get_skill(ref)
        d = s.to_dict(include_body=True)
        from .workflow import skills_used_by

        d["used_by"] = [c.slash for c in skills_used_by(engine.registry, s)]
        return jsonify(d)

    @app.get("/api/commands")
    def commands():  # type: ignore[no-untyped-def]
        plugin_f = request.args.get("plugin")
        out = []
        for c in engine.registry.commands:
            if plugin_f and c.plugin != plugin_f:
                continue
            d = c.to_dict()
            wf = parse_workflow(c)
            d["steps"] = len(wf.steps) + sum(len(v) for v in wf.modes.values())
            d["skills"] = wf.skills
            out.append(d)
        return jsonify(out)

    @app.get("/api/commands/<path:ref>")
    def command(ref: str):  # type: ignore[no-untyped-def]
        c = engine.registry.get_command(ref)
        d = c.to_dict(include_body=True)
        d["workflow"] = parse_workflow(c).to_dict()
        return jsonify(d)

    @app.get("/api/search")
    def search():  # type: ignore[no-untyped-def]
        q = request.args.get("q", "")
        kind = request.args.get("kind") or None
        limit = int(request.args.get("limit", 10))
        return jsonify([h.to_dict() for h in engine.search(q, kind=kind, limit=limit, plugin=request.args.get("plugin") or None)])

    @app.get("/api/graph")
    def graph():  # type: ignore[no-untyped-def]
        if "g" not in graph_cache:
            graph_cache["g"] = skill_graph(engine.registry)
        return jsonify(graph_cache["g"])

    # -- execution -------------------------------------------------------

    def _attachments(payload: dict) -> list[Attachment]:
        out = []
        for a in payload.get("attachments") or []:
            if isinstance(a, dict) and a.get("text"):
                out.append(Attachment(name=str(a.get("name", "attachment")), text=str(a["text"]), kind=str(a.get("kind", "file"))))
        return out

    @app.post("/api/resolve")
    def resolve():  # type: ignore[no-untyped-def]
        payload = request.get_json(force=True, silent=True) or {}
        return jsonify(engine.resolve(str(payload.get("text", ""))))

    @app.post("/api/prompt")
    def prompt():  # type: ignore[no-untyped-def]
        payload = request.get_json(force=True, silent=True) or {}
        text = str(payload.get("text", ""))
        step = payload.get("step")
        from .prompt import parse_slash
        from .workflow import resolve_mode

        slash, rest = parse_slash(text)
        atts = _attachments(payload)
        if slash and engine.registry.has_command(slash.split(":")[-1]) or (slash and ":" in slash):
            try:
                cmd = engine.registry.get_command(slash)
                p = engine.prompts.for_command(cmd, rest, atts, step=int(step) if step else None)
                return jsonify(p.to_dict())
            except RegistryError:
                pass
        if slash:
            sk = engine.registry.get_skill(slash)
            return jsonify(engine.prompts.for_skills([sk], rest, atts).to_dict())
        auto = engine.index.auto_load(text)
        return jsonify(engine.prompts.for_free_text(text, auto, atts).to_dict())

    @app.post("/api/run")
    def run():  # type: ignore[no-untyped-def]
        payload = request.get_json(force=True, silent=True) or {}
        text = str(payload.get("text", ""))
        session_id = payload.get("session_id")
        session = Session.load(session_id) if session_id else (Session.new() if payload.get("save_session", True) else None)
        step = payload.get("step")
        r = engine.run(
            text,
            attachments=_attachments(payload),
            session=session,
            step=int(step) if step else None,
            extra_skills=payload.get("skills") or [],
            max_tokens=int(payload.get("max_tokens", 4096)),
            temperature=float(payload.get("temperature", 0.4)),
            save_artifact=bool(payload.get("save_artifact", True)),
        )
        return jsonify(r.to_dict(include_prompt=bool(payload.get("include_prompt"))))

    @app.post("/api/stream")
    def stream():  # type: ignore[no-untyped-def]
        payload = request.get_json(force=True, silent=True) or {}
        text = str(payload.get("text", ""))
        session_id = payload.get("session_id")
        session = Session.load(session_id) if session_id else (Session.new() if payload.get("save_session", True) else None)
        step = payload.get("step")

        def sse(event: str, data) -> str:
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        def generate():
            try:
                gen = engine.stream(
                    text,
                    attachments=_attachments(payload),
                    session=session,
                    step=int(step) if step else None,
                    extra_skills=payload.get("skills") or [],
                    max_tokens=int(payload.get("max_tokens", 4096)),
                    temperature=float(payload.get("temperature", 0.4)),
                    save_artifact=bool(payload.get("save_artifact", True)),
                )
                for item in gen:
                    if isinstance(item, str):
                        yield sse("chunk", {"text": item})
                    else:
                        yield sse("result", item.to_dict(include_prompt=bool(payload.get("include_prompt"))))
            except (RegistryError, ProviderError, ValueError, FileNotFoundError) as e:
                yield sse("error", {"error": str(e)})

        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
        return Response(stream_with_context(generate()), mimetype="text/event-stream", headers=headers)

    # -- sessions --------------------------------------------------------

    @app.get("/api/sessions")
    def sessions():  # type: ignore[no-untyped-def]
        return jsonify([s.summary() for s in Session.list()])

    @app.get("/api/sessions/<sid>")
    def session_get(sid: str):  # type: ignore[no-untyped-def]
        try:
            return jsonify(Session.load(sid).to_dict())
        except FileNotFoundError as e:
            return jsonify({"error": str(e)}), 404

    @app.delete("/api/sessions/<sid>")
    def session_delete(sid: str):  # type: ignore[no-untyped-def]
        try:
            Session.load(sid).delete()
        except FileNotFoundError as e:
            return jsonify({"error": str(e)}), 404
        return jsonify({"deleted": sid})

    # -- UI --------------------------------------------------------------

    @app.get("/")
    def index():  # type: ignore[no-untyped-def]
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/static/<path:name>")
    def static_file(name: str):  # type: ignore[no-untyped-def]
        return send_from_directory(STATIC_DIR, name)

    return app


def main() -> None:  # pragma: no cover - manual entry point
    app = create_app()
    app.run(host=os.environ.get("PM_ENGINE_HOST", "0.0.0.0"), port=int(os.environ.get("PM_ENGINE_PORT", "8080")), threaded=True)


if __name__ == "__main__":  # pragma: no cover
    main()
