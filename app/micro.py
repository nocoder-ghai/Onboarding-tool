"""micro.py — a tiny zero-dependency web layer (routing, requests, sessions, templates).

Exists so the app runs on a bare Python 3.9 install with no pip packages, matching the
"standalone, no build system" convention in CLAUDE.md. It deliberately implements only
what this app needs; it is not a general-purpose framework.
"""

import base64
import builtins
import hashlib
import hmac
import html
import json
import mimetypes
import os
import re
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY_BYTES = 80 * 1024 * 1024  # hard ceiling before we even read the socket


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #

class Response:
    def __init__(self, body=b"", status=200, content_type="text/html; charset=utf-8",
                 headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.body = body
        self.status = status
        self.headers = [("Content-Type", content_type)]
        if headers:
            self.headers.extend(headers)

    def set_cookie(self, name, value, max_age=None, http_only=True, path="/",
                   same_site="Lax"):
        bits = ["%s=%s" % (name, value), "Path=%s" % path, "SameSite=%s" % same_site]
        if max_age is not None:
            bits.append("Max-Age=%d" % max_age)
        if http_only:
            bits.append("HttpOnly")
        self.headers.append(("Set-Cookie", "; ".join(bits)))
        return self


def redirect(location, flash_ok=None, flash_err=None, request=None):
    if request is not None and (flash_ok or flash_err):
        if flash_ok:
            request.flash(flash_ok, "ok")
        if flash_err:
            request.flash(flash_err, "error")
    return Response(b"", status=302, headers=[("Location", location)])


def json_response(payload, status=200):
    return Response(json.dumps(payload), status=status,
                    content_type="application/json; charset=utf-8")


def file_response(path, download_name=None, inline=True):
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as fh:
        data = fh.read()
    disp = "inline" if inline else "attachment"
    name = download_name or os.path.basename(path)
    safe = urllib.parse.quote(name)
    return Response(data, content_type=ctype, headers=[
        ("Content-Disposition", '%s; filename="%s"' % (disp, safe)),
    ])


class HttpError(Exception):
    def __init__(self, status, message=""):
        super().__init__(message or str(status))
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- #
# Signed cookie sessions
# --------------------------------------------------------------------------- #

class SessionSigner:
    """Signed, tamper-evident cookie payloads. Not encrypted — never store secrets."""

    def __init__(self, secret, max_age=60 * 60 * 24 * 14):
        self.secret = secret if isinstance(secret, bytes) else secret.encode()
        self.max_age = max_age

    def dumps(self, data):
        payload = json.dumps(data, separators=(",", ":")).encode()
        raw = base64.urlsafe_b64encode(payload).rstrip(b"=")
        sig = hmac.new(self.secret, raw, hashlib.sha256).digest()
        return (raw + b"." + base64.urlsafe_b64encode(sig).rstrip(b"=")).decode()

    def loads(self, token):
        try:
            raw, sig = token.encode().split(b".", 1)
            expect = hmac.new(self.secret, raw, hashlib.sha256).digest()
            given = base64.urlsafe_b64decode(sig + b"=" * (-len(sig) % 4))
            if not hmac.compare_digest(expect, given):
                return {}
            data = json.loads(base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4)))
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        issued = data.get("_iat", 0)
        if not isinstance(issued, (int, float)) or time.time() - issued > self.max_age:
            return {}
        return data


# --------------------------------------------------------------------------- #
# Multipart / form parsing
# --------------------------------------------------------------------------- #

class UploadedFile:
    def __init__(self, field, filename, content_type, data):
        self.field = field
        self.filename = filename
        self.content_type = content_type
        self.data = data

    @property
    def size(self):
        return len(self.data)

    def __bool__(self):
        return bool(self.filename and self.data)


def _parse_multipart(body, boundary):
    """Return (fields, files). Hand-rolled to avoid the deprecated cgi module."""
    fields, files = {}, {}
    delim = b"--" + boundary
    for chunk in body.split(delim):
        if not chunk or chunk in (b"--", b"--\r\n"):
            continue
        chunk = chunk[2:] if chunk.startswith(b"\r\n") else chunk
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        head, _, content = chunk.partition(b"\r\n\r\n")
        if not _:
            continue
        name = filename = None
        ctype = "application/octet-stream"
        for line in head.decode("utf-8", "replace").split("\r\n"):
            low = line.lower()
            if low.startswith("content-disposition:"):
                for m in re.finditer(r'(\w+)="([^"]*)"', line):
                    if m.group(1) == "name":
                        name = m.group(2)
                    elif m.group(1) == "filename":
                        filename = m.group(2)
            elif low.startswith("content-type:"):
                ctype = line.split(":", 1)[1].strip()
        if name is None:
            continue
        if filename is not None:
            files[name] = UploadedFile(name, os.path.basename(filename), ctype, content)
        else:
            fields.setdefault(name, []).append(content.decode("utf-8", "replace"))
    return fields, files


# --------------------------------------------------------------------------- #
# Request
# --------------------------------------------------------------------------- #

class Request:
    def __init__(self, method, path, query_string, headers, body, app, client_ip):
        self.method = method
        self.path = path
        self.headers = headers
        self.app = app
        self.client_ip = client_ip
        self.params = {}
        self.query = urllib.parse.parse_qs(query_string, keep_blank_values=True)
        self.form = {}
        self.files = {}
        self.user = None
        self._new_session = None

        ctype = headers.get("Content-Type", "")
        if method == "POST":
            if ctype.startswith("multipart/form-data"):
                m = re.search(r"boundary=(?:\"([^\"]+)\"|([^;]+))", ctype)
                if m:
                    boundary = (m.group(1) or m.group(2)).strip().encode()
                    self.form, self.files = _parse_multipart(body, boundary)
            elif ctype.startswith("application/x-www-form-urlencoded"):
                self.form = urllib.parse.parse_qs(body.decode("utf-8", "replace"),
                                                  keep_blank_values=True)
            elif ctype.startswith("application/json"):
                try:
                    parsed = json.loads(body.decode("utf-8", "replace"))
                    if isinstance(parsed, dict):
                        self.form = {k: [v] for k, v in parsed.items()}
                except Exception:
                    pass

        self.cookies = {}
        for part in headers.get("Cookie", "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                self.cookies[k.strip()] = v.strip()
        self.session = app.signer.loads(self.cookies.get(app.cookie_name, ""))
        self._session_dirty = False

    # -- form access ------------------------------------------------------- #
    def get(self, key, default=""):
        vals = self.form.get(key) or self.query.get(key)
        if not vals:
            return default
        return vals[0]

    def get_all(self, key):
        return list(self.form.get(key) or self.query.get(key) or [])

    def get_int(self, key, default=None):
        raw = self.get(key, "")
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return default

    def checked(self, key):
        return self.get(key, "").lower() in ("1", "on", "true", "yes")

    def file(self, key):
        return self.files.get(key)

    # -- session ----------------------------------------------------------- #
    def session_set(self, key, value):
        self.session[key] = value
        self._session_dirty = True

    def session_pop(self, key, default=None):
        if key in self.session:
            self._session_dirty = True
            return self.session.pop(key)
        return default

    def session_clear(self):
        self.session = {}
        self._session_dirty = True

    def flash(self, message, level="ok"):
        msgs = self.session.get("_flash", [])
        msgs.append([level, message])
        self.session["_flash"] = msgs
        self._session_dirty = True

    def take_flashes(self):
        msgs = self.session.pop("_flash", [])
        if msgs:
            self._session_dirty = True
        return [{"level": lv, "message": m} for lv, m in msgs]

    @property
    def csrf_token(self):
        tok = self.session.get("_csrf")
        if not tok:
            tok = base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")
            self.session_set("_csrf", tok)
        return tok

    def verify_csrf(self):
        given = self.get("_csrf", "")
        expected = self.session.get("_csrf", "")
        if not expected or not hmac.compare_digest(str(given), str(expected)):
            raise HttpError(400, "Your form session expired. Please try again.")


# --------------------------------------------------------------------------- #
# Template engine
# --------------------------------------------------------------------------- #

_TOKEN_RE = re.compile(r"({%.*?%}|{{.*?}})", re.S)


class Template:
    """Compiles a template into a Python function.

    Supports {{ expr }}, {{ expr|safe }}, {% if/elif/else/endif %},
    {% for x in y %}...{% endfor %}, {% set x = expr %} and {% include "f.html" %}.
    Expressions are evaluated against the render context, so template authors get
    plain Python expressions with no new syntax to learn.
    """

    def __init__(self, source, name, env):
        self.name = name
        self.env = env
        self._fn = self._compile(source)

    def _compile(self, source):
        lines = ["def _render(_ctx, _w, _esc, _inc, _ev):"]
        depth = 1

        def emit(code):
            lines.append("    " * depth + code)

        emit("pass")
        for token in _TOKEN_RE.split(source):
            if not token:
                continue
            if token.startswith("{{"):
                expr = token[2:-2].strip()
                raw = False
                if expr.endswith("|safe"):
                    expr, raw = expr[:-5].strip(), True
                emit("_w(_ev(%r))" % expr if raw else "_w(_esc(_ev(%r)))" % expr)
            elif token.startswith("{%"):
                stmt = token[2:-2].strip()
                head = stmt.split(" ", 1)[0]
                rest = stmt[len(head):].strip()
                if head == "if":
                    emit("if _ev(%r):" % rest)
                    depth += 1
                    emit("pass")
                elif head == "elif":
                    depth -= 1
                    emit("elif _ev(%r):" % rest)
                    depth += 1
                    emit("pass")
                elif head == "else":
                    depth -= 1
                    emit("else:")
                    depth += 1
                    emit("pass")
                elif head in ("endif", "endfor"):
                    depth -= 1
                    if depth < 1:
                        raise SyntaxError("unbalanced {%% %s %%} in %s" % (head, self.name))
                elif head == "for":
                    targets, _, iterable = rest.partition(" in ")
                    names = [t.strip() for t in targets.split(",") if t.strip()]
                    emit("for _loop_item in _ev(%r):" % iterable.strip())
                    depth += 1
                    if len(names) == 1:
                        emit("_ctx[%r] = _loop_item" % names[0])
                    else:
                        emit("%s = _loop_item" % ", ".join("_ctx[%r]" % n for n in names))
                elif head == "set":
                    target, _, value = rest.partition("=")
                    emit("_ctx[%r] = _ev(%r)" % (target.strip(), value.strip()))
                elif head == "include":
                    emit("_w(_inc(%r, _ctx))" % rest.strip().strip("\"'"))
                elif head == "comment":
                    pass
                else:
                    raise SyntaxError("unknown tag {%% %s %%} in %s" % (head, self.name))
            else:
                emit("_w(%r)" % token)
        if depth != 1:
            raise SyntaxError("unclosed block in template %s" % self.name)
        scope = {}
        exec(compile("\n".join(lines), "<template:%s>" % self.name, "exec"), scope)
        return scope["_render"]

    def render(self, ctx):
        out = []
        cache = self.env.expr_cache

        def ev(expr):
            code = cache.get(expr)
            if code is None:
                code = compile(expr, "<expr:%s>" % self.name, "eval")
                cache[expr] = code
            try:
                return eval(code, self.env.globals, ctx)
            except Exception as exc:
                raise RuntimeError("template %s: `%s` -> %s: %s"
                                   % (self.name, expr, type(exc).__name__, exc))

        def write(value):
            out.append(value if isinstance(value, str) else str(value))

        def include(name, parent_ctx):
            return self.env.render(name, dict(parent_ctx))

        self._fn(ctx, write, _escape, include, ev)
        return "".join(out)


def _escape(value):
    if value is None or value is False:
        return ""
    if value is True:
        return "true"
    return html.escape(str(value), quote=True)


class TemplateEnv:
    def __init__(self, directory, auto_reload=True):
        self.directory = directory
        self.auto_reload = auto_reload
        self.globals = {"__builtins__": builtins}
        self.expr_cache = {}
        self._cache = {}

    def render(self, name, ctx):
        path = os.path.join(self.directory, name)
        mtime = os.path.getmtime(path)
        cached = self._cache.get(name)
        if cached is None or (self.auto_reload and cached[0] != mtime):
            with open(path, "r", encoding="utf-8") as fh:
                cached = (mtime, Template(fh.read(), name, self))
            self._cache[name] = cached
        return cached[1].render(ctx)


# --------------------------------------------------------------------------- #
# App / router
# --------------------------------------------------------------------------- #

_PARAM_RE = re.compile(r"<(?:(int|str):)?([a-zA-Z_][a-zA-Z0-9_]*)>")


class App:
    def __init__(self, template_dir, static_dir, secret, cookie_name="cuemath_session"):
        self.routes = []
        self.env = TemplateEnv(template_dir)
        self.static_dir = os.path.abspath(static_dir)
        self.signer = SessionSigner(secret)
        self.cookie_name = cookie_name
        self.context_providers = []
        self.before_hooks = []
        self.error_handler = None

    def route(self, pattern, methods=("GET",), name=None):
        regex, converters = self._compile_pattern(pattern)

        def decorator(fn):
            self.routes.append((regex, converters, tuple(methods), fn,
                                name or fn.__name__))
            return fn
        return decorator

    @staticmethod
    def _compile_pattern(pattern):
        converters = {}
        out = ["^"]
        idx = 0
        for m in _PARAM_RE.finditer(pattern):
            out.append(re.escape(pattern[idx:m.start()]))
            kind, var = m.group(1) or "str", m.group(2)
            converters[var] = int if kind == "int" else str
            out.append(r"(?P<%s>%s)" % (var, r"\d+" if kind == "int" else r"[^/]+"))
            idx = m.end()
        out.append(re.escape(pattern[idx:]))
        out.append("$")
        return re.compile("".join(out)), converters

    def context(self, fn):
        """Register a function(request) -> dict merged into every template render."""
        self.context_providers.append(fn)
        return fn

    def before(self, fn):
        """Register a function(request) run before routing (e.g. load the user).
        Returning a Response short-circuits the request."""
        self.before_hooks.append(fn)
        return fn

    def render(self, request, template, **ctx):
        base = {"request": request, "csrf": request.csrf_token,
                "flashes": request.take_flashes()}
        for provider in self.context_providers:
            base.update(provider(request) or {})
        base.update(ctx)
        return Response(self.env.render(template, base))

    # -- dispatch ---------------------------------------------------------- #
    def dispatch(self, request):
        allowed = set()
        for regex, converters, methods, fn, _ in self.routes:
            m = regex.match(request.path)
            if not m:
                continue
            if request.method not in methods:
                allowed.update(methods)
                continue
            kwargs = {k: converters[k](v) for k, v in m.groupdict().items()}
            request.params = kwargs
            return fn(request, **kwargs)
        if allowed:
            raise HttpError(405, "Method not allowed")
        raise HttpError(404, "Page not found")

    def handle(self, request):
        try:
            response = None
            for hook in self.before_hooks:
                response = hook(request)
                if response is not None:
                    break
            if response is None:
                response = self.dispatch(request)
        except HttpError as exc:
            response = self._render_error(request, exc.status, exc.message)
        except Exception:
            traceback.print_exc()
            response = self._render_error(request, 500, "Something went wrong.")
        if response is None:
            response = Response(b"", status=204)
        if request._session_dirty:
            payload = dict(request.session)
            payload["_iat"] = payload.get("_iat") or int(time.time())
            response.set_cookie(self.cookie_name, self.signer.dumps(payload),
                                max_age=self.signer.max_age)
        return response

    def _render_error(self, request, status, message):
        if self.error_handler:
            try:
                return self.error_handler(request, status, message)
            except Exception:
                traceback.print_exc()
        return Response("<h1>%d</h1><p>%s</p>" % (status, _escape(message)),
                        status=status)

    def serve_static(self, request):
        rel = request.path[len("/static/"):]
        target = os.path.normpath(os.path.join(self.static_dir, rel))
        if not target.startswith(os.path.abspath(self.static_dir)) or \
                not os.path.isfile(target):
            raise HttpError(404, "Not found")
        resp = file_response(target)
        resp.headers.append(("Cache-Control", "no-cache"))
        return resp


class _Handler(BaseHTTPRequestHandler):
    server_version = "CuemathOnboarding/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter, single-line access log
        print("%s %s" % (self.address_string(), fmt % args))

    def _run(self, method):
        app = self.server.app
        parsed = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._emit(Response("Upload too large.", status=413))
            return
        body = self.rfile.read(length) if length else b""
        request = Request(method, urllib.parse.unquote(parsed.path), parsed.query,
                          self.headers, body, app, self.client_address[0])
        try:
            if method == "GET" and request.path.startswith("/static/"):
                response = app.serve_static(request)
            else:
                response = app.handle(request)
        except HttpError as exc:
            response = app._render_error(request, exc.status, exc.message)
        except Exception:
            traceback.print_exc()
            response = Response("Internal error", status=500)
        self._emit(response)

    def _emit(self, response):
        self.send_response(response.status)
        for key, value in response.headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.body)

    def do_GET(self):
        self._run("GET")

    def do_POST(self):
        self._run("POST")

    def do_HEAD(self):
        self._run("GET")


def serve(app, host="127.0.0.1", port=8000):
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.app = app
    httpd.daemon_threads = True
    print("Cuemath Coach Onboarding running at http://%s:%d/" % (host, port))
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        httpd.server_close()
