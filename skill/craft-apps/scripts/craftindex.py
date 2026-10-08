"""Semantic search over the tools and commands of the Crafting Apps, and a doctor that checks the index against the apps.

    craftmcp.py find <slot> "what you want to do"     # the 5 best matches (offline: reads the index only)
    craftmcp.py index status [slot]                    # what is indexed (offline)
    craftmcp.py index doctor [slot ...]                # compare the index with the REAL apps (starts them if needed)
    craftmcp.py index sync [slot ...]                  # fix drift: drop removed, describe new and changed (via the `pi` CLI), re-embed

An item is an MCP tool (kind `tool`, from tools/list) or an engine command (kind `cmd`, from the app's
list_commands / command_list tool, run through `craftmcp.py run`). Each item has a fingerprint (`sig`) of the
text the app itself provides (tool: name, description and input schema; command: id, label, menu and parameter
doc; NOT the enabled flag or shortcut, which depend on the app state). Commands get a one-sentence description and
search keywords written once by an agent (Pi) and stored in the index; tools use their own MCP description.
The text `id: label. description. Keywords: ...` is embedded with the bge-small model (BAAI/bge-small-en-v1.5), a
query is matched by cosine similarity, and when a System One server is available the 20 nearest items are re-ranked
by one `choice` question. Needs numpy and an embedder: the knowledge-base skill's `kb` module if installed, else
fastembed (see _Backend for the environment variables).

Index files (default <gateway folder>\\index, override with CRAFT_INDEX_DIR):
    <slot>.desc.json   items by key (`cmd:<id>` / `tool:<name>`): kind, id, label, menu, params, sig, desc, keys, src
    <slot>.vec.npz     keys, sigs and vectors of the embedded items, with the embedder name and text version
"""
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

TEXT_VERSION = 3          # bump when the embedded text format changes: vectors are rebuilt
SHORTLIST = 20
SKIP_TOOL_PREFIX = {"lightcraft": ("cmd_",)}   # these tools only wrap a command that is indexed as `cmd`
LIST_TOOL = {
    "designcraft": "list_commands", "vectorcraft": "list_commands", "photocraft": "command_list",
    "lightcraft": "list_commands", "filmcraft": "command_list", "printcraft": "command_list",
    "effectcraft": "list_commands",
}
KB_SCRIPTS = os.path.expanduser(os.path.join("~", ".claude", "skills", "knowledge-base", "scripts"))
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "   # bge's query instruction
# Registries that are neither tools nor commands (an effects catalogue, say): (tool that lists them, item kind, how to use one)
REGISTRIES = {
    "effectcraft": ("list_effects", "effect", "craftmcp.py call effectcraft add_effect effect=<id> layer=<layer>"),
}
DESCRIBED_KINDS = ("cmd", "effect")   # these need a generated description; tools use their MCP text, notes are hand-written
CHOICE_QUESTION = "Which command or tool does exactly what the task asks? Pick the single best one."


# ----------------------------------------------------------------------------- paths and storage

def index_dir(cm):
    path = os.environ.get("CRAFT_INDEX_DIR") or os.path.join(cm.GATEWAY_DIR, "index")
    os.makedirs(path, exist_ok=True)
    return path


def desc_path(cm, slot):
    return os.path.join(index_dir(cm), slot + ".desc.json")


def vec_path(cm, slot):
    return os.path.join(index_dir(cm), slot + ".vec.npz")


def sha(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def load_desc(cm, slot):
    path = desc_path(cm, slot)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("schema") != 2:  # first format: {"entries": {id: {desc, keys}}}, commands only, no fingerprints
        items = {}
        for cid, e in (data.get("entries") or {}).items():
            items["cmd:" + cid] = {"kind": "cmd", "id": cid, "desc": e.get("desc", ""), "keys": e.get("keys", []), "src": "pi", "sig": None}
        data = {"schema": 2, "slot": slot, "items": items, "generated": data.get("generated"), "migrated": True}
    return data


def save_desc(cm, slot, data):
    data["schema"] = 2
    data["slot"] = slot
    data["updated"] = time.strftime("%Y-%m-%d")
    with open(desc_path(cm, slot), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)


# ----------------------------------------------------------------------------- the real corpus

def _params_of_schema(schema):
    props = (schema or {}).get("properties") or {}
    req = set((schema or {}).get("required") or [])
    parts = []
    for name, spec in props.items():
        typ = spec.get("type") if isinstance(spec, dict) else None
        if isinstance(typ, list):
            typ = "|".join(str(t) for t in typ if t != "null")
        parts.append("%s%s%s" % (name, "*" if name in req else "?", ":" + str(typ) if typ else ""))
    return "{" + ", ".join(parts) + "}"


def _menu_text(menu):
    if isinstance(menu, list):
        return " > ".join(str(m) for m in menu)
    return str(menu or "")


def live_corpus(cm, slot):
    """{key: item} for the running app: its MCP tools and engine commands. Raises cm.Fail when the app cannot answer."""
    cm.session(slot)
    tools = cm.rpc(slot, "tools/list", {}).get("tools", [])
    skip = SKIP_TOOL_PREFIX.get(slot, ())
    items = {}
    for t in tools:
        name = t["name"]
        if name.startswith(skip) if skip else False:
            continue
        schema = t.get("inputSchema") or {}
        description = (t.get("description") or "").strip()
        items["tool:" + name] = {
            "kind": "tool", "id": name, "label": "", "menu": "", "params": _params_of_schema(schema),
            "text0": description,
            "sig": sha("tool\n%s\n%s\n%s" % (name, description, json.dumps(schema, sort_keys=True))),
        }
    result = cm.rpc(slot, "tools/call", {"name": LIST_TOOL[slot], "arguments": {}}, cm.DEFAULT_TIMEOUT)
    text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
    if result.get("isError"):
        raise cm.Fail("%s: %s failed: %s" % (slot, LIST_TOOL[slot], text[:200]))
    try:
        data = json.loads(text)
    except ValueError:
        raise cm.Fail("%s: %s did not return JSON" % (slot, LIST_TOOL[slot]))
    commands = data.get("commands") if isinstance(data, dict) else data
    if not isinstance(commands, list):
        raise cm.Fail("%s: unexpected command list shape" % slot)
    for c in commands:
        cid = c.get("id")
        if not cid:
            continue
        label, menu, params = c.get("label", "") or "", _menu_text(c.get("menu")), (c.get("params") or "")
        items["cmd:" + cid] = {
            "kind": "cmd", "id": cid, "label": label, "menu": menu, "params": params, "tool": c.get("tool"),
            "sig": sha("cmd\n%s\n%s\n%s\n%s" % (cid, label, menu, params)),
        }
    if slot in REGISTRIES:
        tool_name, kind, use = REGISTRIES[slot]
        reply = cm.rpc(slot, "tools/call", {"name": tool_name, "arguments": {}}, cm.DEFAULT_TIMEOUT)
        text = "".join(c.get("text", "") for c in reply.get("content", []) if c.get("type") == "text")
        try:
            entries = json.loads(text)
        except ValueError:
            entries = None
        if reply.get("isError") or not isinstance(entries, list):
            raise cm.Fail("%s: %s did not return a list" % (slot, tool_name))
        for e in entries:
            eid = e.get("id")
            if not eid:
                continue
            names = [p.get("name") or p.get("id") for p in (e.get("params") or []) if isinstance(p, dict)]
            ids = [p.get("id") for p in (e.get("params") or []) if isinstance(p, dict)]
            items["%s:%s" % (kind, eid)] = {
                "kind": kind, "id": eid, "label": e.get("name", ""), "menu": e.get("category", ""),
                "params": "{" + ", ".join(str(n) for n in names if n) + "}", "use": use,
                "sig": sha("\n".join((kind, eid, e.get("name", ""), e.get("category", ""), ",".join(str(i) for i in ids)))),
            }
    return items


def notes_path(cm, slot):
    return os.path.join(index_dir(cm), slot + ".notes.json")


def notes_items(cm, slot):
    """Hand-written notes (traps, recipes, 'for bulk edits use run_script') from <index>/<slot>.notes.json, as items.

    Each note: {id, title, text, keys: [...], see: [command or tool ids]}. They are searched like everything else; the
    doctor treats the file as the source of truth for the `note` kind.
    """
    path = notes_path(cm, slot)
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        notes = json.load(fh)
    items = {}
    for n in notes:
        nid = n["id"]
        keys = [str(k).lower() for k in n.get("keys", [])]
        see = list(n.get("see", []))
        items["note:" + nid] = {
            "kind": "note", "id": nid, "label": n.get("title", ""), "menu": "", "params": "", "text0": n["text"], "desc": n["text"],
            "keys": keys, "see": see, "src": "note",
            "sig": sha("\n".join(("note", nid, n.get("title", ""), n["text"], ",".join(keys), ",".join(see)))),
        }
    return items


# ----------------------------------------------------------------------------- diff (pure, unit-tested)

def classify(live, stored):
    """Compare the live corpus with the stored items. Returns a dict of key lists.

    new         in the app, not in the index
    removed     in the index, not in the app
    changed     both, fingerprint differs
    unsigned    both, the stored fingerprint is unknown (first-format index): adopt, not a real change
    undescribed commands in the index without a description
    """
    stored = stored or {}
    out = {"new": [], "removed": [], "changed": [], "unsigned": [], "undescribed": []}
    for key in live:
        if key not in stored:
            out["new"].append(key)
        elif stored[key].get("sig") is None:
            out["unsigned"].append(key)
        elif stored[key]["sig"] != live[key]["sig"]:
            out["changed"].append(key)
    for key in stored:
        if key not in live:
            out["removed"].append(key)
    for key, it in stored.items():
        if key in live and it.get("kind") in DESCRIBED_KINDS and not it.get("desc"):
            out["undescribed"].append(key)
    return {k: sorted(v) for k, v in out.items()}


def broken_see(live):
    """{note key: [ids it points to that no longer exist]} for notes whose `see` list names a command, tool or effect
    that is not in the live corpus (the app renamed or removed it, or the note has a typo)."""
    known = {k.split(":", 1)[1] for k in live if not k.startswith("note:")}
    out = {}
    for key, it in live.items():
        if it.get("kind") != "note":
            continue
        missing = [i for i in it.get("see", []) if i not in known]
        if missing:
            out[key] = missing
    return out


def has_drift(diff):
    return bool(diff["new"] or diff["removed"] or diff["changed"])


# ----------------------------------------------------------------------------- embedding text and vectors

_NOISE_TERMS = {"true", "false", "null", "px", "pt", "deg", "bool", "str", "id", "ms", "number", "string"}


def option_terms(params, limit=300):
    """Parameter names and the values of `a|b|c` options of a parameter doc as plain words, so that a value such as
    `trim` or `wiggle` inside `kind: group|rect|trim|wiggle|...` can be found. Capped, camelCase split."""
    words, seen = [], set()

    def add(term):
        for w in re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", term).lower().split():
            if w not in seen and w not in _NOISE_TERMS and len(w) > 1:
                seen.add(w)
                words.append(w)
    for name in re.findall(r'[\{,]\s*"?([A-Za-z][A-Za-z0-9_]*)"?\??\s*:', params or ""):
        add(name)
    for group in re.findall(r"[A-Za-z][A-Za-z0-9_]*(?:\|[A-Za-z][A-Za-z0-9_]*)+", params or ""):
        for term in group.split("|"):
            add(term)
    text = " ".join(words)
    return text[:limit].rsplit(" ", 1)[0] if len(text) > limit else text


def embed_text(item):
    if item["kind"] == "note":
        return "%s: %s. %s Keywords: %s." % (item["id"], item.get("label", ""), item.get("desc", ""), ", ".join(item.get("keys") or []))
    if item["kind"] == "effect":
        base = "%s: %s effect (%s). %s" % (item["id"], item.get("label", ""), item.get("menu", ""), item.get("desc", ""))
        return base + ((" Keywords: " + ", ".join(item["keys"]) + ".") if item.get("keys") else "") + " Parameters: " + (item.get("params") or "").strip("{}")[:200]
    if item["kind"] == "tool":
        first = re.split(r"(?<=[.!?])\s", item.get("desc") or item.get("text0") or "", 1)[0][:300]
        return "%s: %s %s" % (item["id"], first, item.get("params", "")[:120]) + (
            (" Keywords: " + ", ".join(item["keys"]) + ".") if item.get("keys") else "")
    if item.get("desc"):
        text = "%s: %s. %s Keywords: %s." % (item["id"], item.get("label", ""), item["desc"], ", ".join(item.get("keys") or []))
        terms = option_terms(item.get("params"))
        return text + (" Options: " + terms + "." if terms else "")
    return "%s: %s. %s" % (item["id"], item.get("label", ""), (item.get("params") or "")[:140])


class _Backend:
    """Embedder and System One client.

    With the knowledge-base skill installed (and CRAFT_EMBEDDER not set to `fastembed`) both come from its `kb`
    module. Otherwise the same bge model runs through fastembed (`pip install fastembed`; the vectors match the
    knowledge-base embedder to a cosine of 0.999999, so one index serves both), and System One is the server at
    CRAFT_SYSTEM_ONE_URL with the model CRAFT_SYSTEM_ONE_MODEL. Without a System One server `find` keeps the
    vector order.
    """

    def __init__(self):
        self.kb = None
        self._fe = None
        if os.environ.get("CRAFT_EMBEDDER", "").lower() != "fastembed" and os.path.isfile(os.path.join(KB_SCRIPTS, "kb.py")):
            if KB_SCRIPTS not in sys.path:
                sys.path.insert(0, KB_SCRIPTS)
            import kb  # the knowledge-base skill: local bge embedder and System One client
            self.kb = kb

    def _fastembed(self):
        if self._fe is None:
            try:
                from fastembed import TextEmbedding
            except ImportError:
                raise RuntimeError("no embedder: install fastembed (pip install fastembed)") from None
            cache = os.environ.get("FASTEMBED_CACHE_PATH") or os.path.join(
                os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "craft-gateway", "fastembed")
            self._fe = TextEmbedding(EMBED_MODEL, cache_dir=cache)
        return self._fe

    def embed_documents(self, texts):
        if self.kb is not None:
            return self.kb.embed_documents(texts)
        return [[float(x) for x in v] for v in self._fastembed().embed(texts)]

    def embed_query(self, query):
        if self.kb is not None:
            return self.kb.embed_query(query)
        return self.embed_documents([QUERY_PREFIX + query])[0]

    def djev_ask(self, state, questions):
        """One System One request (a `choice` / `noul` question set); raises when no server is configured."""
        url = os.environ.get("CRAFT_SYSTEM_ONE_URL")
        if url:
            import urllib.request
            model = os.environ.get("CRAFT_SYSTEM_ONE_MODEL")
            if not model:
                raise RuntimeError("CRAFT_SYSTEM_ONE_MODEL is not set (the model name the server serves)")
            body = json.dumps({"model": model, "samples": 1, "state": state, "questions": questions}, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url.rstrip("/") + "/v1/systemone", data=body, headers={"Content-Type": "application/json; charset=utf-8"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                answers = json.loads(resp.read().decode("utf-8")).get("answers")
            if not isinstance(answers, dict):
                raise RuntimeError("System One returned an unexpected response")
            return answers
        if self.kb is not None:
            return self.kb.djev_ask(state, questions)
        raise RuntimeError("no System One server (set CRAFT_SYSTEM_ONE_URL and CRAFT_SYSTEM_ONE_MODEL)")


_BACKEND = None


def _kb():
    """The embedder and System One client (see _Backend); tests replace this function."""
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = _Backend()
    return _BACKEND


def build_vectors(cm, slot, data):
    import numpy as np
    kb = _kb()
    keys = sorted(data["items"])
    texts = [embed_text(data["items"][k]) for k in keys]
    vectors = np.array(kb.embed_documents(texts), dtype="float32")
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-9
    sigs = [data["items"][k].get("sig") or "" for k in keys]
    text_sigs = [sha(t) for t in texts]
    np.savez_compressed(vec_path(cm, slot), keys=np.array(keys), sigs=np.array(sigs), text_sigs=np.array(text_sigs),
                        vectors=vectors, meta=np.array(json.dumps({"text_version": TEXT_VERSION, "dim": int(vectors.shape[1]), "built": time.strftime("%Y-%m-%d %H:%M")})))
    return len(keys)


def vectors_state(cm, slot, data):
    """'ok', 'missing', or a reason string, comparing the vector file with the descriptions that should be embedded."""
    path = vec_path(cm, slot)
    if not os.path.isfile(path):
        return "missing"
    import numpy as np
    z = np.load(path, allow_pickle=False)
    meta = json.loads(str(z["meta"]))
    if meta.get("text_version") != TEXT_VERSION:
        return "text format changed (v%s, now v%s)" % (meta.get("text_version"), TEXT_VERSION)
    have = dict(zip([str(k) for k in z["keys"]], [str(s) for s in z["text_sigs"]]))
    want = {k: sha(embed_text(data["items"][k])) for k in data["items"]}
    stale = [k for k in want if have.get(k) != want[k]]
    extra = [k for k in have if k not in want]
    if stale or extra:
        return "%d item(s) not embedded or embedded from older text, %d vector(s) of removed items" % (len(stale), len(extra))
    return "ok"


# ----------------------------------------------------------------------------- find

def judge_option(it):
    """The short text the System One judge reads for one candidate: the label and the start of the description."""
    return ((it.get("label") or "") + (" - " if it.get("label") else "") + (it.get("desc") or it.get("text0") or ""))[:130] or it["id"]


TIP_MIN = 0.6            # a note is shown as a tip when the judge finds it useful for the task with at least this probability
TIP_VECTOR_ONLY = 0.70   # without the judge, a note is shown when its cosine similarity to the query is at least this
TIP_CANDIDATES = 3       # nearest notes put to the judge
TIP_MAX = 2


def tip_question(query, it):
    return {
        "type": "noul",
        "instructions": "Task: %s\nTip: %s. %s\nDoes this tip give advice that helps with this task (the right command or option to use, a trap to avoid, or a faster way)?" % (
            query, it.get("label", ""), (it.get("desc") or "")[:500]),
        "criteria": {"true": "The tip is relevant: it names what to use for this task or warns about a trap in it.",
                     "false": "The tip is about something else."},
    }


def find(cm, slot, query, top=5, shortlist=SHORTLIST, rerank=True, kind=None, tips=True):
    """Best matches for a task: (results, note). `results` are the top items (tools, commands, effects) followed by up
    to TIP_MAX notes that the judge found useful for the task (a note is advice next to the answer, not a competing
    answer, so it is judged by its own yes/no question and never takes a place in the ranking). `kind="note"` searches
    the notes alone. `tips=False` leaves the notes out (tools, commands and effects only)."""
    import numpy as np
    data = load_desc(cm, slot)
    if data is None or not os.path.isfile(vec_path(cm, slot)):
        raise cm.Fail("no index for %s yet: run `craftmcp.py index sync %s`" % (slot, slot))
    z = np.load(vec_path(cm, slot), allow_pickle=False)
    keys = [str(k) for k in z["keys"]]
    kb = _kb()
    q = np.array(kb.embed_query(query), dtype="float32")
    q /= np.linalg.norm(q) + 1e-9
    sims = z["vectors"] @ q
    order = np.argsort(-sims)
    cands, near_notes = [], []
    for i in order:
        key = keys[i]
        it = data["items"].get(key)
        if it is None:
            continue
        if it["kind"] == "note":
            if kind in (None, "note") and len(near_notes) < TIP_CANDIDATES:
                near_notes.append((key, float(sims[i])))
            if kind != "note":
                continue
        elif kind and it["kind"] != kind:
            continue
        if len(cands) < max(shortlist, top):
            cands.append((key, float(sims[i])))
        if len(cands) >= max(shortlist, top) and len(near_notes) >= TIP_CANDIDATES:
            break
    if kind == "note" or not tips:
        near_notes = []                       # the notes are the answers here (or not wanted), not tips
    note = None
    ranked = list(cands)
    found_tips = []
    questions = {}
    if rerank and len(cands) >= 2:
        criteria = {data["items"][k]["kind"][0] + ":" + data["items"][k]["id"]: judge_option(data["items"][k]) for k, _ in cands[:shortlist]}
        questions["f"] = {"type": "choice", "instructions": CHOICE_QUESTION, "criteria": criteria}
    if rerank:
        for n, (k, _) in enumerate(near_notes):
            questions["t%d" % n] = tip_question(query, data["items"][k])
    answer = None
    if questions:
        try:
            answer = kb.djev_ask({"task": query}, questions)
        except Exception as exc:  # System One is optional: fall back to the vector order
            note = "System One unavailable (%s): vector order only" % str(exc)[:80]
    if answer is not None and "f" in answer:
        lookup = {data["items"][k]["kind"][0] + ":" + data["items"][k]["id"]: k for k, _ in cands[:shortlist]}
        ranked = sorted(((lookup[c], p) for c, p in answer["f"]["probabilities"].items() if c in lookup), key=lambda x: -x[1])
    if answer is not None:
        for n, (k, _) in enumerate(near_notes):
            try:
                p = float(answer["t%d" % n]["noul"])
            except (KeyError, TypeError, ValueError):
                continue
            if p >= TIP_MIN:
                found_tips.append((k, p))
    elif not rerank or note:
        found_tips = [(k, s) for k, s in near_notes if s >= TIP_VECTOR_ONLY][:1]
    found_tips = sorted(found_tips, key=lambda x: -x[1])[:TIP_MAX]
    out = [(data["items"][k], p) for k, p in ranked[:top]] + [(data["items"][k], p) for k, p in found_tips]
    return out, note


def print_find(cm, slot, results, note):
    if not results:
        print("nothing found")
        return
    items = [r for r in results if r[0]["kind"] != "note"]
    tips = [r for r in results if r[0]["kind"] == "note"]
    if tips and not items:                      # a search among the notes themselves: a numbered list
        for n, (it, p) in enumerate(tips, 1):
            print("%d. note %s   %.2f\n     %s" % (n, it["id"], p, it.get("label", "")))
            print("     %s" % it.get("desc", "")[:700].replace("\n", "\n     "))
            if it.get("see"):
                print("     see: %s" % ", ".join(it["see"]))
        if note:
            print("\nnote: " + note)
        return
    for n, (it, p) in enumerate(items, 1):
        desc = it.get("desc") or (it.get("text0") or "")
        first = re.split(r"(?<=[.!?])\s", desc, 1)[0][:150]
        label = (" (%s)" % it["label"]) if it.get("label") else ""
        print("%d. %-4s %s%s   %.2f\n     %s" % (n, it["kind"], it["id"], label, p, first))
        if it.get("params"):
            print("     params: %s" % it["params"][:150])
        if it["kind"] == "cmd":
            print("     run:  craftmcp.py run %s %s '{...}'" % (slot, it["id"]))
        elif it["kind"] == "effect":
            print("     use:  %s" % (it.get("use") or REGISTRIES.get(slot, ("", "", ""))[2]))
        else:
            print("     call: craftmcp.py call %s %s key=value" % (slot, it["id"]))
    for it, p in tips:
        print("\ntip (%.2f): %s\n     %s" % (p, it.get("label") or it["id"], it.get("desc", "")[:700].replace("\n", "\n     ")))
        if it.get("see"):
            print("     see: %s" % ", ".join(it["see"]))
    if note:
        print("\nnote: " + note)


# ----------------------------------------------------------------------------- status, doctor, sync

def cmd_status(cm, args):
    slots = [args.slot] if args.slot else list(cm.SLOTS)
    for slot in slots:
        data = load_desc(cm, slot)
        if data is None:
            print("%-12s no index" % slot)
            continue
        items = data["items"]
        undesc = sum(1 for i in items.values() if i["kind"] in DESCRIBED_KINDS and not i.get("desc"))
        unsigned = sum(1 for i in items.values() if i.get("sig") is None)
        try:
            vs = vectors_state(cm, slot, data)
        except Exception as exc:
            vs = "unreadable (%s)" % exc
        print("%-12s %4d items (%s) | undescribed %d | unsigned %d | vectors: %s | updated %s" % (
            slot, len(items), kind_summary(items), undesc, unsigned, vs, data.get("updated") or data.get("generated")))
    return 0


def _slots_for(cm, args):
    slots = args.slots or list(cm.SLOTS)
    for s in slots:
        cm.check_slot(s)
    return slots


def _configured(cm, slot):
    status = cm.get_json("/status")["apps"].get(slot, {})
    return bool(status.get("valid")), status.get("state"), status.get("reason")


def with_app(cm, slot, fn):
    """Run fn() against the app, starting it through the gateway if needed and stopping it again if we started it."""
    valid, state, reason = _configured(cm, slot)
    if not valid:
        raise cm.Fail("%s is not usable (%s)" % (slot, reason))
    was_running = state == "running"
    if not was_running:
        subprocess.run([sys.executable, cm.__file__, "ensure", slot], capture_output=True)
    try:
        return fn()
    finally:
        if not was_running:
            try:
                cm.request("POST", "/admin/apps/%s/stop" % slot, {}, timeout=60)  # plain stop: refused if documents are unsaved
            except Exception:
                pass


def app_build(cm, slot):
    """First line of the app's BUILD_INFO.txt as the gateway reports it (the commit it was built from), or None."""
    try:
        text = cm.get_json("/status")["apps"][slot].get("build_info") or ""
    except Exception:
        return None
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    return first or None


def kind_summary(items):
    """'25 tools + 528 commands + 306 effects + 4 notes'"""
    names = (("tool", "tools"), ("cmd", "commands"), ("effect", "effects"), ("note", "notes"))
    counts = {k: sum(1 for i in items.values() if i["kind"] == k) for k, _ in names}
    return " + ".join("%d %s" % (counts[k], label) for k, label in names if counts[k])


def doctor_slot(cm, slot):
    """Returns (diff or None, state text, lines)."""
    stored = load_desc(cm, slot)
    try:
        live = with_app(cm, slot, lambda: live_corpus(cm, slot))
    except cm.Fail as exc:
        return None, "cannot check: %s" % exc, stored
    live.update(notes_items(cm, slot))      # the notes file is the source of truth for the `note` kind
    diff = classify(live, (stored or {}).get("items"))
    return (diff, live), None, stored


def cmd_doctor(cm, args):
    worst = 0
    for slot in _slots_for(cm, args):
        result, problem, stored = doctor_slot(cm, slot)
        if problem:
            print("%-12s %s" % (slot, problem))
            worst = max(worst, 2)
            continue
        diff, live = result
        issues = []
        if stored is None:
            issues.append("no index")
        for name in ("new", "removed", "changed"):
            if diff[name]:
                sample = ", ".join(k.split(":", 1)[1] for k in diff[name][:4]) + (" ..." if len(diff[name]) > 4 else "")
                issues.append("%d %s (%s)" % (len(diff[name]), name, sample))
        if diff["unsigned"]:
            issues.append("%d without a fingerprint (old index; `index sync` adopts them)" % len(diff["unsigned"]))
        if diff["undescribed"]:
            issues.append("%d commands without a description" % len(diff["undescribed"]))
        for key, missing in sorted(broken_see(live).items()):
            issues.append("note %s points to unknown id(s): %s (fix the notes file %s)" % (key.split(":", 1)[1], ", ".join(missing), os.path.basename(notes_path(cm, slot))))
        vs = vectors_state(cm, slot, stored) if stored else "missing"
        if vs != "ok":
            issues.append("vectors: " + vs)
        now = app_build(cm, slot)
        hint = None
        if stored and stored.get("build") and now and stored["build"] != now:
            hint = "the app was rebuilt since the index was made (indexed: %s | now: %s)" % (stored["build"][:60], now[:60])
        print("%-12s %s | app: %s | index: %d items" % (
            slot, "OK" if not issues else "DRIFT", kind_summary(live), len((stored or {}).get("items", {}))))
        for line in issues:
            print("    - " + line)
        if hint:
            print("    note: " + hint + (" but no item differs" if not issues else ""))
        if issues:
            worst = max(worst, 1)
    print("\n%s" % ("index matches the apps" if worst == 0 else ("fix with: craftmcp.py index sync <slot>" if worst == 1 else "some apps could not be checked")))
    return worst


PI_BRIEF = """Goal: write a plain-language description and search keywords for application commands, so a search engine can find a command from how a person phrases a task. Work only inside the current folder: do not read anything outside it, do not use the network, do not run GUI programs.

Input: `chunks/chunk_NN.json`, each a JSON list of objects {key, kind, id, label, menu, params}. `kind` is `cmd` (an engine command) or `effect` (a visual effect of a motion-graphics app: label is the effect's name, menu its category, params its parameter names; describe what the effect does to the picture or video). Output: for every chunk write `out/desc_NN.json`, a JSON list with ONE object per input object, same order: {"key": ..., "desc": "...", "keys": ["...", ...]}.
- desc: one plain sentence, at most 25 words, starting with a verb, saying what the command does for the user (not the id).
- keys: 8 to 14 short lowercase search phrases (1 to 4 words) a person might type for this task: task verbs, nouns, synonyms, the Adobe/InDesign/Photoshop/Premiere name of the feature when you know it, everyday wording. Add synonyms and task phrases, do not just repeat the id words. No duplicates.
Rules: ground every description in the id, label, menu and params; if you cannot tell what a command does, describe it literally from the id words and use the id words and obvious synonyms as keys; never invent features. Do not copy the params text. Keep `key` exactly as given. Valid JSON only. Skip a chunk whose out file already exists and parses. After writing each file check that it parses and has the same keys in the same order as its input. Use the edit/write tools for the files. Report in at most 8 lines: files written and any command you could not understand.
"""


def prepare_pending(cm, slot, live, keys, workdir, size=60):
    os.makedirs(os.path.join(workdir, "chunks"), exist_ok=True)
    os.makedirs(os.path.join(workdir, "out"), exist_ok=True)
    rows = [{"key": k, "kind": live[k]["kind"], "id": live[k]["id"], "label": live[k]["label"], "menu": live[k]["menu"], "params": (live[k]["params"] or "")[:260]} for k in keys]
    n = 0
    for i in range(0, len(rows), size):
        with open(os.path.join(workdir, "chunks", "chunk_%02d.json" % n), "w", encoding="utf-8") as fh:
            json.dump(rows[i:i + size], fh, ensure_ascii=False)
        n += 1
    with open(os.path.join(workdir, "BRIEF.md"), "w", encoding="utf-8") as fh:
        fh.write(PI_BRIEF)
    return n


def collect_pending(workdir):
    out = {}
    for f in sorted(glob.glob(os.path.join(workdir, "out", "desc_*.json"))):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                for r in json.load(fh):
                    if r.get("key") and r.get("desc"):
                        out[r["key"]] = {"desc": str(r["desc"]).strip(), "keys": [str(k).strip().lower() for k in r.get("keys", []) if str(k).strip()]}
        except (OSError, ValueError):
            continue
    return out


def run_pi(workdir, count):
    """Describe the prepared chunks with the `pi` CLI (non-interactive). Returns True when it exited normally.

    Provider and model default to the ones the pi-delegation wrapper uses (a bare `pi -p` waited forever on the
    machine's default model); override with CRAFT_PI_PROVIDER / CRAFT_PI_MODEL. About 2 s per command were
    measured for 1,276 commands, so the timeout allows 5 s per command plus 5 minutes.
    """
    pi = "pi.cmd" if os.name == "nt" else "pi"
    if not shutil.which(pi):
        print("the `pi` CLI is not installed: the new commands stay searchable by label and parameters (or use --no-describe)", flush=True)
        return False
    brief = os.path.join(workdir, "BRIEF.md")
    args = [pi, "-p", "--no-session", "--provider", os.environ.get("CRAFT_PI_PROVIDER", "zai"),
            "--model", os.environ.get("CRAFT_PI_MODEL", "glm-5.3-flash"), "@" + brief]
    try:
        with open(os.path.join(workdir, "pi.stdout.txt"), "w", encoding="utf-8") as fo, open(os.path.join(workdir, "pi.stderr.txt"), "w", encoding="utf-8") as fe:
            proc = subprocess.run(args, cwd=workdir, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe, timeout=min(7200, 300 + 5 * count))
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def sync_slot(cm, slot, args):
    live = with_app(cm, slot, lambda: live_corpus(cm, slot))
    live.update(notes_items(cm, slot))
    data = load_desc(cm, slot) or {"schema": 2, "items": {}}
    diff = classify(live, data["items"])
    items = data["items"]
    for key in diff["removed"]:
        del items[key]
    for key in diff["unsigned"]:      # adopt: the stored description was written for this item
        items[key]["sig"] = live[key]["sig"]
        for field in ("label", "menu", "params", "text0", "tool"):
            if field in live[key]:
                items[key][field] = live[key][field]
    need = list(diff["new"]) + list(diff["changed"])
    for key in need:                  # keep an old description for a changed item until a new one exists
        old = items.get(key, {})
        items[key] = dict(live[key], desc=old.get("desc", ""), keys=old.get("keys", []), src=old.get("src", ""))
        if live[key]["kind"] == "tool":
            items[key]["desc"], items[key]["keys"], items[key]["src"] = live[key]["text0"], [], "tool"
        elif live[key]["kind"] == "note":
            items[key]["desc"], items[key]["keys"], items[key]["src"] = live[key]["desc"], live[key]["keys"], "note"
    cmd_pending = [k for k in need if live[k]["kind"] in DESCRIBED_KINDS] + [k for k in diff["undescribed"] if k not in need]
    for key in diff["undescribed"]:
        items[key].update({f: live[key][f] for f in ("label", "menu", "params") if f in live[key]})
    described = 0
    if cmd_pending and not args.no_describe:
        workdir = os.path.join(index_dir(cm), "_work", slot)
        if os.path.isdir(workdir):
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)   # a folder held open by a shell is cleaned file by file below
            for sub in ("chunks", "out"):
                for f in glob.glob(os.path.join(workdir, sub, "*")):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
        chunks = prepare_pending(cm, slot, live, cmd_pending, workdir)
        print("%s: describing %d command(s) with pi (%d chunk(s)) ..." % (slot, len(cmd_pending), chunks), flush=True)
        run_pi(workdir, len(cmd_pending))
        got = collect_pending(workdir)
        for key, d in got.items():
            if key in items:
                items[key].update(desc=d["desc"], keys=d["keys"], src="pi")
                described += 1
        missing = len(cmd_pending) - described
        if missing:
            print("%s: %d command(s) still without a description (pi did not finish); they are searchable by label and params, run sync again (work folder kept: %s)" % (slot, missing, workdir))
        else:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)   # the descriptions are in the index now
    data["items"] = items
    build = app_build(cm, slot)
    if build:
        data["build"] = build
    save_desc(cm, slot, data)
    n = build_vectors(cm, slot, data)
    print("%s: index updated: %d items (+%d new, ~%d changed, -%d removed, %d adopted, %d described), vectors rebuilt" % (
        slot, n, len(diff["new"]), len(diff["changed"]), len(diff["removed"]), len(diff["unsigned"]), described))


def cmd_sync(cm, args):
    for slot in _slots_for(cm, args):
        sync_slot(cm, slot, args)
    return 0


# ----------------------------------------------------------------------------- CLI glue

def run_notes(cm, args):
    """`notes <slot>` lists the notes (id, title, task phrases); `notes <slot> "query"` searches them."""
    cm.check_slot(args.slot)
    if not args.query:
        data = load_desc(cm, args.slot)
        notes = sorted((i for i in (data or {}).get("items", {}).values() if i["kind"] == "note"), key=lambda i: i["id"])
        if not notes:
            print("no notes for %s (add them to %s)" % (args.slot, os.path.basename(notes_path(cm, args.slot))))
            return 0
        for it in notes:
            print("%-34s %s\n%-34s for: %s" % (it["id"], it.get("label", ""), "", ", ".join((it.get("keys") or [])[:8])))
        return 0
    results, note = find(cm, args.slot, " ".join(args.query), top=args.top, rerank=not args.no_rerank, kind="note")
    print_find(cm, args.slot, results, note)
    return 0


def run_find(cm, args):
    cm.check_slot(args.slot)
    results, note = find(cm, args.slot, " ".join(args.query), top=args.top, shortlist=args.shortlist, rerank=not args.no_rerank, kind=args.kind, tips=not args.no_tips)
    if args.json:
        print(json.dumps([{"kind": it["kind"], "id": it["id"], "label": it.get("label"), "p": round(p, 3), "desc": it.get("desc") or it.get("text0"), "params": it.get("params")} for it, p in results], ensure_ascii=False, indent=1))
    else:
        print_find(cm, args.slot, results, note)
    return 0


def cmd_eval(cm, args):
    """Quality regression: run the queries in <index>/eval_queries.json through find and report how often an accepted
    answer is first / in the top 3. Each query has `accept` regexes matched against the item id and `kind:id`."""
    path = os.path.join(index_dir(cm), "eval_queries.json")
    if not os.path.isfile(path):
        raise cm.Fail("no %s" % path)
    with open(path, "r", encoding="utf-8") as fh:
        queries = json.load(fh)
    if args.slot:
        queries = [q for q in queries if q["slot"] == args.slot]
    groups, misses = {}, []
    started = time.time()
    for q in queries:
        results, note = find(cm, q["slot"], q["query"], top=3, rerank=not args.no_rerank)

        def accepted(it):
            names = [it["id"], it["kind"] + ":" + it["id"]]
            return any(re.search(pat, n, re.I) for pat in q["accept"] for n in names)
        items = [it for it, _ in results if it["kind"] != "note"]
        tips = [it for it, _ in results if it["kind"] == "note"]
        rank = next((i for i, it in enumerate(items, 1) if accepted(it)), None)
        shown_as_tip = any(accepted(it) for it in tips)       # a note counts when it is shown as a tip
        g = groups.setdefault(q.get("set", "all"), [0, 0, 0])
        g[0] += 1
        g[1] += rank == 1
        g[2] += rank is not None or shown_as_tip
        if rank is None and shown_as_tip:
            rank = "tip"
        if rank != 1:
            misses.append((q, rank, results))
    for name, (n, first, top3) in groups.items():
        print("%-26s %2d queries: first %2d, found (top 3 or tip) %2d" % (name, n, first, top3))
    total = [sum(g[i] for g in groups.values()) for i in range(3)]
    print("%-26s %2d queries: first %2d, found (top 3 or tip) %2d   (%.1f s per query)" % ("all", total[0], total[1], total[2], (time.time() - started) / max(1, total[0])))
    if args.misses:
        for q, rank, results in misses:
            print("  #%s %-11s %-48s -> %s" % (rank, q["slot"], q["query"][:48], ", ".join("%s:%s" % (it["kind"], it["id"]) for it, _ in results)))
    return 0


def run_index(cm, args):
    return {"status": cmd_status, "doctor": cmd_doctor, "sync": cmd_sync, "eval": cmd_eval}[args.action](cm, args)
