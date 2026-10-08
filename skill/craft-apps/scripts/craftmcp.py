#!/usr/bin/env python
"""craftmcp: drive the Crafting Apps (DesignCraft, VectorCraft, ...) through craft-gateway from the command line.

The gateway exposes each app's MCP server as Streamable HTTP on one loopback port; this client speaks that
protocol directly, so no Claude Code MCP registration and no session restart are needed.

  craftmcp.py status                         gateway and app states (exit 2 if the gateway is down)
  craftmcp.py gateway-start | gateway-stop   start/stop the gateway (via gatewayctl)
  craftmcp.py ensure <slot>                  make sure the gateway runs and the app is started
  craftmcp.py port <slot>                    control port of the running app (for scripts that use the control channel)
  craftmcp.py tools <slot> [--filter TEXT]   list the app's MCP tools
  craftmcp.py describe <slot> <tool>         description and input schema of one tool
  craftmcp.py call <slot> <tool> [ARGS]      call a tool; ARGS = JSON object | @file.json | - (stdin) | key=value ...
  craftmcp.py run <slot> <command> [JSON]    run an engine command through the app's own runner (old name: exec)
  craftmcp.py commands <slot> [filter=x]     the app's engine commands (large; prefer find)
  craftmcp.py find <slot> "task"             semantic search: the best tools and commands (offline; --kind, --top, --json)
  craftmcp.py notes <slot> [query]           list the traps and recipes of an app, or search them (find shows them as tips)
  craftmcp.py new|inspect|render|export|save <slot> [key=value ...]   common verbs mapped per app (see `verbs <slot>`)
  craftmcp.py verbs <slot>                   how the common verbs map to that app's tools
  craftmcp.py check designcraft|photocraft   pre-delivery checks of the open document
  craftmcp.py cleanup <slot> [--clean] [--dirty-match RE] [--yes]   close throwaway documents (dry run without --yes)
  craftmcp.py index status|doctor|sync [slot ...]   the search index: state, comparison with the real apps, repair
  craftmcp.py stop <slot> [--force]          stop an app; refuses when it has unsaved documents unless --force
  craftmcp.py rpc <slot> <method> [JSON]     raw JSON-RPC request

Images in results are saved as files (default %TEMP%\\craft-mcp) and their paths printed.
Environment: CRAFT_GATEWAY_PORT, CRAFT_GATEWAY_HOME, CRAFT_GATEWAY_DIR (folder of gatewayctl.py), CRAFT_INDEX_DIR.
Details: reference/commands.md next to SKILL.md.
"""
import argparse
import base64
import http.client
import json
import os
import subprocess
import sys
import tempfile
import time
import tomllib

SLOTS = ("designcraft", "vectorcraft", "photocraft", "lightcraft", "filmcraft", "printcraft", "effectcraft")
# The skill lives in <repo>/skill/craft-apps/scripts next to <repo>/gateway; it is installed into the skills folder
# as a link (junction), and realpath follows the link back into the repository.
GATEWAY_DIR = os.environ.get("CRAFT_GATEWAY_DIR") or os.path.normpath(
    os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "..", "gateway"))
DEFAULT_TIMEOUT = 600.0

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class Fail(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def gateway_home():
    return os.environ.get("CRAFT_GATEWAY_HOME") or os.path.join(os.environ.get("APPDATA", ""), "craft-gateway")


def gateway_port():
    if os.environ.get("CRAFT_GATEWAY_PORT"):
        return int(os.environ["CRAFT_GATEWAY_PORT"])
    try:
        with open(os.path.join(gateway_home(), "config.toml"), "rb") as fh:
            return int(tomllib.load(fh).get("port", 7970))
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return 7970


def request(method, path, body=None, timeout=DEFAULT_TIMEOUT):
    """(status, headers, bytes). Raises Fail(code=2) when the gateway is not reachable."""
    conn = http.client.HTTPConnection("127.0.0.1", gateway_port(), timeout=timeout)
    headers = {"Accept": "application/json, text/event-stream"}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, payload, headers)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
        raise Fail("gateway is not reachable on 127.0.0.1:%d (%s); start it with: craftmcp.py gateway-start" % (gateway_port(), exc), 2)
    finally:
        conn.close()


def get_json(path, timeout=15.0):
    status, _, data = request("GET", path, timeout=timeout)
    if status != 200:
        raise Fail("GET %s -> HTTP %d" % (path, status))
    return json.loads(data)


def check_slot(slot):
    if slot not in SLOTS:
        raise Fail("unknown slot %r; slots are: %s" % (slot, ", ".join(SLOTS)))


_ids = iter(range(1, 10**9))


def rpc(slot, method, params=None, timeout=DEFAULT_TIMEOUT, notify=False):
    check_slot(slot)
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    if not notify:
        msg["id"] = next(_ids)
    status, _, data = request("POST", "/" + slot, msg, timeout=timeout)
    if status == 403:
        raise Fail("the gateway refused the request (HTTP 403: bad Host/Origin)")
    if notify:
        return None
    if status != 200:
        raise Fail("gateway answered HTTP %d: %s" % (status, data[:200].decode("utf-8", "replace")))
    reply = json.loads(data)
    if "error" in reply:
        message = str(reply["error"].get("message", reply["error"]))
        raise Fail(message if message.startswith(slot) else "%s: %s" % (slot, message))
    return reply.get("result")


def session(slot, timeout=DEFAULT_TIMEOUT):
    """Initialize; the gateway answers repeats from its cache, so doing it per command is cheap."""
    rpc(slot, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "craftmcp", "version": "1"}}, timeout)
    rpc(slot, "notifications/initialized", notify=True)


def parse_args_list(tokens):
    """JSON object, @file, '-', or key=value pairs (values parsed as JSON when possible)."""
    if not tokens:
        return {}
    if len(tokens) == 1:
        t = tokens[0]
        if t == "-":
            return json.loads(sys.stdin.read())
        if t.startswith("@"):
            with open(t[1:], "r", encoding="utf-8-sig") as fh:
                return json.load(fh)
        if t.lstrip().startswith("{"):
            return json.loads(t)
    out = {}
    for t in tokens:
        if "=" not in t:
            raise Fail("argument %r is not key=value (or give one JSON object)" % t)
        k, v = t.split("=", 1)
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v
    return out


def render_result(slot, tool, result, out_dir, max_chars):
    """Print a tools/call result; images become files. Returns True when the tool reported an error."""
    is_error = bool(result.get("isError"))
    printed = False
    n = 0
    for item in result.get("content", []):
        kind = item.get("type")
        if kind == "text":
            text = item.get("text", "")
            if len(text) > max_chars:
                print(text[:max_chars] + "\n...[%d more characters; call again with --max-chars %d or --out FILE]" % (len(text) - max_chars, len(text)))
            else:
                print(text)
            printed = True
        elif kind == "image":
            ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(item.get("mimeType", "image/png"), "bin")
            os.makedirs(out_dir, exist_ok=True)
            n += 1
            path = os.path.join(out_dir, "%s-%s-%d-%d.%s" % (slot, tool, int(time.time()), n, ext))
            with open(path, "wb") as fh:
                fh.write(base64.b64decode(item.get("data", "")))
            print("[image saved: %s]" % path)
            printed = True
        else:
            print(json.dumps(item, ensure_ascii=False)[:max_chars])
            printed = True
    if not printed and "structuredContent" in result:
        print(json.dumps(result["structuredContent"], ensure_ascii=False)[:max_chars])
    return is_error


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_status(args):
    st = get_json("/status" + ("?dirty=1" if args.dirty else ""))
    print("gateway pid=%s port=%s uptime=%ss" % (st["pid"], st["port"], st["uptime_s"]))
    for slot, a in st["apps"].items():
        extra = []
        if a.get("control_port"):
            extra.append("control=%s" % a["control_port"])
        if a.get("dirty_documents"):
            extra.append("unsaved: " + ", ".join(a["dirty_documents"]))
        if a.get("last_error"):
            extra.append("error: %s" % a["last_error"])
        if not a["valid"] and a["state"] == "disabled":
            extra.append(a.get("reason") or "")
        print("  %-12s %-9s %s" % (slot, a["state"], "  ".join(x for x in extra if x)))


def gatewayctl(*argv):
    ctl = os.path.join(GATEWAY_DIR, "gatewayctl.py")
    if not os.path.isfile(ctl):
        raise Fail("gatewayctl.py not found in %s (set CRAFT_GATEWAY_DIR)" % GATEWAY_DIR)
    r = subprocess.run([sys.executable, ctl, *argv], capture_output=True, text=True, timeout=120)
    return r.returncode, (r.stdout + r.stderr).strip()


def cmd_gateway(args):
    code, out = gatewayctl(args.action)
    print(out)
    return code


def ensure_gateway():
    try:
        get_json("/status", timeout=3.0)
        return
    except Fail:
        pass
    code, out = gatewayctl("start")
    if code != 0:
        raise Fail("could not start the gateway: " + out)
    print(out)


def cmd_ensure(args):
    check_slot(args.slot)
    ensure_gateway()
    status, _, data = request("POST", "/admin/apps/%s/start" % args.slot, {}, timeout=120)
    reply = json.loads(data)
    if not reply.get("ok"):
        raise Fail(reply.get("error", "start failed"))
    st = get_json("/status")["apps"][args.slot]
    print("%s %s%s" % (args.slot, st["state"], " control=%s" % st["control_port"] if st.get("control_port") else ""))


def cmd_port(args):
    check_slot(args.slot)
    st = get_json("/status")["apps"][args.slot]
    if not st.get("control_port"):
        raise Fail("%s has no running app under the gateway (state %s); use: craftmcp.py ensure %s" % (args.slot, st["state"], args.slot))
    print(st["control_port"])


def cmd_tools(args):
    session(args.slot, args.timeout)
    tools = rpc(args.slot, "tools/list", {}, args.timeout)["tools"]
    shown = 0
    for t in sorted(tools, key=lambda t: t["name"]):
        if args.filter and args.filter.lower() not in (t["name"] + " " + t.get("description", "")).lower():
            continue
        first = (t.get("description") or "").strip().splitlines()[0:1]
        print("%-28s %s" % (t["name"], (first[0] if first else "")[:110]))
        shown += 1
    print("(%d of %d tools)" % (shown, len(tools)))


def cmd_describe(args):
    session(args.slot, args.timeout)
    for t in rpc(args.slot, "tools/list", {}, args.timeout)["tools"]:
        if t["name"] == args.tool:
            print(t.get("description", ""))
            print(json.dumps(t.get("inputSchema", {}), indent=1, ensure_ascii=False))
            return
    raise Fail("%s has no tool %r (list them with: craftmcp.py tools %s)" % (args.slot, args.tool, args.slot))


def do_call(args, tool, arguments):
    session(args.slot, args.timeout)
    result = rpc(args.slot, "tools/call", {"name": tool, "arguments": arguments}, args.timeout)
    return 1 if render_result(args.slot, tool, result, args.out_dir, args.max_chars) else 0


def cmd_call(args):
    return do_call(args, args.tool, parse_args_list(args.args))


# Common verbs over the per-app MCP tool names (verified against the tool lists of the seven apps).
# An entry is (tool, fixed_args) or ("run", command, fixed_params): run a command through the app's runner.
# `path` is the file argument everywhere; every other key=value is passed to the app's tool unchanged
# (page=, comp=, seconds=, doc=, ...): see `describe <slot> <tool>` for what each app accepts.
RUNNER = {  # generic command runner: (tool, name of the command-id argument)
    "designcraft": ("execute", "command"),
    "vectorcraft": ("run_command", "command"),
    "photocraft": ("command_run", "id"),
    "lightcraft": ("run_command", "command"),
    "filmcraft": ("command_run", "id"),
    "printcraft": ("ui_command", "id"),  # desktop app registry only; takes no params
    "effectcraft": ("execute_command", "command"),
}
VERBS = {
    "commands": {
        "designcraft": ("list_commands",), "vectorcraft": ("list_commands",), "photocraft": ("command_list",),
        "lightcraft": ("list_commands",), "filmcraft": ("command_list",), "printcraft": ("command_list",),
        "effectcraft": ("list_commands",),
    },
    "new": {
        "designcraft": ("new_document",), "photocraft": ("doc_new",), "printcraft": ("doc_create",),
        "vectorcraft": ("run", "file.new"), "effectcraft": ("run", "comp.new"),
    },
    "inspect": {
        "designcraft": ("inspect_document",), "vectorcraft": ("inspect_document",), "photocraft": ("doc_inspect",),
        "lightcraft": ("inspect_ui",), "filmcraft": ("project_inspect",), "printcraft": ("doc_info",),
        "effectcraft": ("get_project",),
    },
    "render": {  # an image of the work, from the engine where the app offers it (see SKILL.md)
        "designcraft": ("render_page",), "vectorcraft": ("screenshot",), "photocraft": ("doc_render_preview",),
        "lightcraft": ("render_photo",), "filmcraft": ("render_frame",), "printcraft": ("page_render",),
        "effectcraft": ("render_frame",),
    },
    "export": {  # write the work to a file in another format (path=...; photocraft: see cmd_verb)
        "designcraft": ("export_png",), "vectorcraft": ("export",), "photocraft": ("doc_export",),
        "lightcraft": ("export",), "printcraft": ("doc_export_images",),
    },
    "save": {
        "designcraft": ("save_document",), "vectorcraft": ("save_file",), "photocraft": ("doc_save",),
        "printcraft": ("doc_save",), "effectcraft": ("save_project",),
    },
}


def run_arguments(slot, command, params):
    tool, key = RUNNER[slot]
    if slot == "printcraft" and params:
        raise Fail("printcraft's command runner (ui_command) takes no params; use `call printcraft control_call` or a doc_* tool")
    arguments = {key: command}
    if slot != "printcraft":
        arguments["params"] = params
    return tool, arguments


def cmd_run(args):
    check_slot(args.slot)
    params = parse_args_list(args.params) if args.params else {}
    tool, arguments = run_arguments(args.slot, args.command, params)
    return do_call(args, tool, arguments)


def export_photocraft(args, arguments):
    """PhotoCraft writes only below its automation write root (the gateway's exchange folder) and takes
    relative paths. An absolute `path=` is exported there under a temporary name and copied to it."""
    import shutil
    target = arguments.get("path")
    if not target or not os.path.isabs(target):
        return do_call(args, "doc_export", arguments)
    exchange = os.environ.get("CRAFT_EXCHANGE_DIR") or os.path.join(tempfile.gettempdir(), "craft-exchange")
    temp_name = "export-%d%s" % (os.getpid(), os.path.splitext(target)[1] or ".png")
    code = do_call(args, "doc_export", dict(arguments, path=temp_name))
    produced = os.path.join(exchange, temp_name)
    if code == 0 and os.path.isfile(produced):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.move(produced, target)
        print("exported to %s (%d bytes)" % (target, os.path.getsize(target)))
    elif code == 0:
        raise Fail("photocraft reported success but %s was not written (is the gateway exchange folder %s the app's write root? restart photocraft through the gateway)" % (temp_name, exchange))
    return code


def cmd_verb(args):
    check_slot(args.slot)
    entry = VERBS[args.verb].get(args.slot)
    if entry is None:
        have = sorted(VERBS[args.verb])
        raise Fail("%s has no `%s` through MCP (apps that do: %s); use `tools %s` / `run`" % (args.slot, args.verb, ", ".join(have), args.slot))
    arguments = parse_args_list(args.args)
    if args.verb == "export" and args.slot == "photocraft":
        return export_photocraft(args, arguments)
    if entry[0] == "run":
        tool, arguments = run_arguments(args.slot, entry[1], arguments)
    else:
        tool = entry[0]
    return do_call(args, tool, arguments)


def cmd_verbs(args):
    check_slot(args.slot)
    print("%-9s -> %s" % ("run", "%s(%s=<command>, params)" % RUNNER[args.slot]))
    for verb, table in VERBS.items():
        entry = table.get(args.slot)
        what = "(not available)" if entry is None else ("run %s" % entry[1] if entry[0] == "run" else entry[0])
        print("%-9s -> %s" % (verb, what))
    return 0


cmd_exec = cmd_run  # old name


def cmd_find(args):
    import craftindex
    return craftindex.run_find(sys.modules[__name__], args)


def cmd_notes(args):
    import craftindex
    return craftindex.run_notes(sys.modules[__name__], args)


def cmd_index(args):
    import craftindex
    return craftindex.run_index(sys.modules[__name__], args)


def cmd_check(args):
    import craftcheck
    return craftcheck.run_check(sys.modules[__name__], args)


def cmd_cleanup(args):
    import craftcheck
    return craftcheck.run_cleanup(sys.modules[__name__], args)


def cmd_stop(args):
    check_slot(args.slot)
    status, _, data = request("POST", "/admin/apps/%s/stop%s" % (args.slot, "?force=1" if args.force else ""), {}, timeout=120)
    reply = json.loads(data)
    if reply.get("ok"):
        print("%s stopped%s" % (args.slot, " (forced)" if args.force else ""))
        return 0
    if reply.get("dirty_check_failed"):
        print("REFUSED: could not check %s for unsaved documents. Ask the user before using --force." % args.slot)
    elif reply.get("dirty_documents"):
        print("REFUSED: %s has unsaved documents: %s. Ask the user before using --force." % (args.slot, ", ".join(reply["dirty_documents"])))
    else:
        print("ERROR: %s" % reply.get("error", "stop failed"))
    return 1


def cmd_rpc(args):
    session(args.slot, args.timeout)
    params = parse_args_list(args.params) if args.params else None
    print(json.dumps(rpc(args.slot, args.method, params, args.timeout), indent=1, ensure_ascii=False)[: args.max_chars])
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="craftmcp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
        sp.add_argument("--max-chars", type=int, default=6000)
        sp.add_argument("--out-dir", default=os.path.join(tempfile.gettempdir(), "craft-mcp"))

    s = sub.add_parser("status"); s.add_argument("--dirty", action="store_true"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("gateway-start"); s.set_defaults(fn=cmd_gateway, action="start")
    s = sub.add_parser("gateway-stop"); s.set_defaults(fn=cmd_gateway, action="stop")
    s = sub.add_parser("ensure"); s.add_argument("slot"); s.set_defaults(fn=cmd_ensure)
    s = sub.add_parser("port"); s.add_argument("slot"); s.set_defaults(fn=cmd_port)
    s = sub.add_parser("tools"); s.add_argument("slot"); s.add_argument("--filter"); common(s); s.set_defaults(fn=cmd_tools)
    s = sub.add_parser("describe"); s.add_argument("slot"); s.add_argument("tool"); common(s); s.set_defaults(fn=cmd_describe)
    s = sub.add_parser("call"); s.add_argument("slot"); s.add_argument("tool"); s.add_argument("args", nargs="*"); common(s); s.set_defaults(fn=cmd_call)
    for name in ("run", "exec"):  # exec = old name of run
        s = sub.add_parser(name); s.add_argument("slot"); s.add_argument("command"); s.add_argument("params", nargs="*"); common(s); s.set_defaults(fn=cmd_run)
    for verb in VERBS:
        s = sub.add_parser(verb, help="common verb, see `verbs <slot>`"); s.add_argument("slot"); s.add_argument("args", nargs="*"); common(s); s.set_defaults(fn=cmd_verb, verb=verb)
    s = sub.add_parser("verbs"); s.add_argument("slot"); s.set_defaults(fn=cmd_verbs)
    s = sub.add_parser("find", help="semantic search over the tools and commands of an app (offline, uses the index)")
    s.add_argument("slot"); s.add_argument("query", nargs="+"); s.add_argument("--top", type=int, default=5)
    s.add_argument("--shortlist", type=int, default=20, help="candidates re-ranked by System One"); s.add_argument("--no-rerank", action="store_true")
    s.add_argument("--kind", choices=("tool", "cmd", "effect", "note")); s.add_argument("--no-tips", action="store_true", help="leave the notes out: tools, commands and effects only")
    s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_find)
    s = sub.add_parser("notes", help="list the notes of an app, or search them: notes <slot> [query]")
    s.add_argument("slot"); s.add_argument("query", nargs="*"); s.add_argument("--top", type=int, default=3); s.add_argument("--no-rerank", action="store_true"); s.set_defaults(fn=cmd_notes)
    s = sub.add_parser("index", help="status / doctor / sync of the search index against the real apps")
    isub = s.add_subparsers(dest="action", required=True)
    t = isub.add_parser("status"); t.add_argument("slot", nargs="?"); t.set_defaults(fn=cmd_index)
    t = isub.add_parser("doctor", help="compare the index with the live apps; exit 1 on drift, 2 if an app could not be checked")
    t.add_argument("slots", nargs="*"); t.set_defaults(fn=cmd_index)
    t = isub.add_parser("eval", help="search quality regression over gateway/index/eval_queries.json")
    t.add_argument("slot", nargs="?"); t.add_argument("--misses", action="store_true", help="list the queries whose first result was not accepted"); t.add_argument("--no-rerank", action="store_true"); t.set_defaults(fn=cmd_index)
    t = isub.add_parser("sync", help="fix drift: drop removed, describe new and changed commands with pi, re-embed")
    t.add_argument("slots", nargs="*"); t.add_argument("--no-describe", action="store_true", help="do not call pi; undescribed commands are searched by label and params"); t.set_defaults(fn=cmd_index)
    s = sub.add_parser("check", help="pre-delivery checks of the open document (designcraft, photocraft)")
    s.add_argument("slot"); s.add_argument("--bleed", type=float, default=0.0, help="pt an item may extend past the page before it is reported")
    s.add_argument("--max-samples", type=int, default=300, help="non-ASCII text runs probed for glyph coverage"); s.set_defaults(fn=cmd_check)
    s = sub.add_parser("cleanup", help="close documents in the running app (dry run unless --yes)")
    s.add_argument("slot"); s.add_argument("--clean", action="store_true", help="close documents without unsaved changes")
    s.add_argument("--dirty-match", metavar="REGEX", help="also close UNSAVED documents whose title matches (discards their changes)")
    s.add_argument("--yes", action="store_true", help="actually close; without it only the plan is printed"); s.set_defaults(fn=cmd_cleanup)
    s = sub.add_parser("stop"); s.add_argument("slot"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_stop)
    s = sub.add_parser("rpc"); s.add_argument("slot"); s.add_argument("method"); s.add_argument("params", nargs="*"); common(s); s.set_defaults(fn=cmd_rpc)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args) or 0
    except Fail as exc:
        print("error: %s" % exc, file=sys.stderr)
        return exc.code
    except (ValueError, json.JSONDecodeError) as exc:
        print("error: bad JSON or value: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
