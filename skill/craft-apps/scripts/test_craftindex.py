"""Offline tests of craftindex (no apps, no servers, no Pi): python -m unittest test_craftindex   (run in this folder)."""
import hashlib
import json
import os
import sys
import tempfile
import unittest

import craftindex as ci


class Fail(Exception):
    pass


class FakeCM:
    """The slice of the craftmcp module craftindex uses, with canned app answers."""
    Fail = Fail
    DEFAULT_TIMEOUT = 1
    SLOTS = ("designcraft", "lightcraft")
    __file__ = "craftmcp.py"

    def __init__(self, folder, tools=None, commands=None, wrap=False):
        self.GATEWAY_DIR = folder
        self.tools = tools or []
        self.commands = commands or []
        self.wrap = wrap
        self.effects = []

    def session(self, slot):
        pass

    def check_slot(self, slot):
        pass

    def rpc(self, slot, method, params=None, timeout=None):
        if method == "tools/list":
            return {"tools": self.tools}
        if params and params.get("name") == "list_effects":
            return {"content": [{"type": "text", "text": json.dumps(self.effects)}]}
        body = {"commands": self.commands, "count": len(self.commands)} if self.wrap else self.commands
        return {"content": [{"type": "text", "text": json.dumps(body)}]}


class FakeKB:
    """Deterministic 16-dimensional 'embeddings' from word hashes, and a scripted System One."""
    def __init__(self, answer=None, fail=False):
        self.answer, self.fail, self.calls = answer, fail, 0

    @staticmethod
    def _vec(text):
        v = [0.0] * 16
        for w in text.lower().replace(":", " ").replace(".", " ").split():
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 16] += 1.0
        return v

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, q):
        return self._vec(q)

    def djev_ask(self, state, questions):
        self.calls += 1
        if self.fail:
            raise RuntimeError("down")
        return self.answer(state, questions)


def cmd(cid, label="", params="", menu=None, **extra):
    return dict({"id": cid, "label": label, "params": params, "menu": menu or [], "enabled": True, "shortcut": None}, **extra)


def tool(name, description="", props=None):
    return {"name": name, "description": description, "inputSchema": {"type": "object", "properties": props or {}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["CRAFT_INDEX_DIR"] = os.path.join(self.tmp.name, "index")
        self.addCleanup(os.environ.pop, "CRAFT_INDEX_DIR", None)
        self.kb = FakeKB()
        self._orig_kb = ci._kb
        ci._kb = lambda: self.kb
        self.addCleanup(setattr, ci, "_kb", self._orig_kb)

    def patch(self, name, value):
        """Replace a craftindex function for one test only."""
        original = getattr(ci, name)
        setattr(ci, name, value)
        self.addCleanup(setattr, ci, name, original)


class ClassifyTests(Base):
    def test_every_kind_of_drift_is_found(self):
        live = {"cmd:a": {"sig": "1", "kind": "cmd"}, "cmd:b": {"sig": "2", "kind": "cmd"}, "cmd:n": {"sig": "3", "kind": "cmd"},
                "cmd:u": {"sig": "4", "kind": "cmd"}, "cmd:d": {"sig": "5", "kind": "cmd"}}
        stored = {"cmd:a": {"sig": "1", "kind": "cmd", "desc": "x"}, "cmd:b": {"sig": "OLD", "kind": "cmd", "desc": "x"},
                  "cmd:gone": {"sig": "9", "kind": "cmd", "desc": "x"}, "cmd:u": {"sig": None, "kind": "cmd", "desc": "x"},
                  "cmd:d": {"sig": "5", "kind": "cmd", "desc": ""}}
        d = ci.classify(live, stored)
        self.assertEqual(d["new"], ["cmd:n"])
        self.assertEqual(d["removed"], ["cmd:gone"])
        self.assertEqual(d["changed"], ["cmd:b"])
        self.assertEqual(d["unsigned"], ["cmd:u"])
        self.assertEqual(d["undescribed"], ["cmd:d"])
        self.assertTrue(ci.has_drift(d))

    def test_identical_corpus_has_no_drift(self):
        live = {"cmd:a": {"sig": "1", "kind": "cmd"}, "tool:t": {"sig": "2", "kind": "tool"}}
        stored = {"cmd:a": {"sig": "1", "kind": "cmd", "desc": "d"}, "tool:t": {"sig": "2", "kind": "tool", "desc": ""}}
        d = ci.classify(live, stored)
        self.assertFalse(ci.has_drift(d))
        self.assertEqual(d["undescribed"], [])  # tools use their MCP text, only commands need a description

    def test_no_index_at_all_means_everything_is_new(self):
        d = ci.classify({"cmd:a": {"sig": "1", "kind": "cmd"}}, None)
        self.assertEqual(d["new"], ["cmd:a"])


class CorpusTests(Base):
    def test_fingerprint_ignores_enabled_and_shortcut_but_not_params(self):
        a = ci.live_corpus(FakeCM(self.tmp.name, commands=[cmd("x.y", "Label", "{a}", enabled=True, shortcut="Ctrl+A")]), "designcraft")
        b = ci.live_corpus(FakeCM(self.tmp.name, commands=[cmd("x.y", "Label", "{a}", enabled=False, shortcut="Ctrl+B", why="no doc")]), "designcraft")
        c = ci.live_corpus(FakeCM(self.tmp.name, commands=[cmd("x.y", "Label", "{a, b}")]), "designcraft")
        self.assertEqual(a["cmd:x.y"]["sig"], b["cmd:x.y"]["sig"])
        self.assertNotEqual(a["cmd:x.y"]["sig"], c["cmd:x.y"]["sig"])

    def test_tool_fingerprint_covers_description_and_schema(self):
        base = ci.live_corpus(FakeCM(self.tmp.name, tools=[tool("t", "does a", {"p": {"type": "string"}})]), "designcraft")["tool:t"]["sig"]
        desc = ci.live_corpus(FakeCM(self.tmp.name, tools=[tool("t", "does b", {"p": {"type": "string"}})]), "designcraft")["tool:t"]["sig"]
        schema = ci.live_corpus(FakeCM(self.tmp.name, tools=[tool("t", "does a", {"p": {"type": "integer"}})]), "designcraft")["tool:t"]["sig"]
        self.assertEqual(len({base, desc, schema}), 3)

    def test_command_list_may_be_a_list_or_a_wrapped_dict(self):
        cmds = [cmd("a.b", "A"), cmd("c.d", "C")]
        self.assertEqual(set(ci.live_corpus(FakeCM(self.tmp.name, commands=cmds), "designcraft")), {"cmd:a.b", "cmd:c.d"})
        self.assertEqual(set(ci.live_corpus(FakeCM(self.tmp.name, commands=cmds, wrap=True), "designcraft")), {"cmd:a.b", "cmd:c.d"})

    def test_lightcraft_command_wrapper_tools_are_not_indexed_twice(self):
        tools = [tool("cmd_app_gpu", "wrapper"), tool("render_photo", "real tool")]
        live = ci.live_corpus(FakeCM(self.tmp.name, tools=tools, commands=[cmd("app.gpu", "GPU")]), "lightcraft")
        self.assertIn("tool:render_photo", live)
        self.assertNotIn("tool:cmd_app_gpu", live)
        self.assertIn("cmd:app.gpu", live)


class StorageTests(Base):
    def test_first_format_is_migrated_with_unknown_fingerprints(self):
        cm = FakeCM(self.tmp.name)
        os.makedirs(ci.index_dir(cm), exist_ok=True)
        with open(ci.desc_path(cm, "designcraft"), "w", encoding="utf-8") as fh:
            json.dump({"slot": "designcraft", "entries": {"frame.thread": {"desc": "Links frames.", "keys": ["thread"]}}}, fh)
        data = ci.load_desc(cm, "designcraft")
        item = data["items"]["cmd:frame.thread"]
        self.assertEqual((item["kind"], item["desc"], item["sig"]), ("cmd", "Links frames.", None))

    def test_save_and_load_roundtrip(self):
        cm = FakeCM(self.tmp.name)
        ci.save_desc(cm, "designcraft", {"items": {"cmd:a": {"kind": "cmd", "id": "a", "sig": "1", "desc": "d", "keys": []}}})
        self.assertEqual(ci.load_desc(cm, "designcraft")["items"]["cmd:a"]["sig"], "1")
        self.assertIsNone(ci.load_desc(cm, "lightcraft"))


class VectorTests(Base):
    def _data(self):
        return {"items": {
            "cmd:frame.thread": {"kind": "cmd", "id": "frame.thread", "label": "Thread Frames", "desc": "Links text frames so text flows.", "keys": ["thread", "link"], "sig": "1", "params": "{}"},
            "cmd:file.close": {"kind": "cmd", "id": "file.close", "label": "Close", "desc": "Closes the document.", "keys": ["close"], "sig": "2", "params": "{}"},
            "tool:render_page": {"kind": "tool", "id": "render_page", "label": "", "desc": "Render a page as an image.", "keys": [], "sig": "3", "params": "{page?:integer}", "text0": "Render a page as an image."},
        }}

    def test_vectors_state_follows_the_descriptions(self):
        cm, data = FakeCM(self.tmp.name), self._data()
        self.assertEqual(ci.vectors_state(cm, "designcraft", data), "missing")
        ci.build_vectors(cm, "designcraft", data)
        self.assertEqual(ci.vectors_state(cm, "designcraft", data), "ok")
        data["items"]["cmd:file.close"]["desc"] = "Shuts the window."          # a new description makes the stored vector stale
        self.assertIn("not embedded", ci.vectors_state(cm, "designcraft", data))
        ci.build_vectors(cm, "designcraft", data)
        del data["items"]["cmd:frame.thread"]                                  # a removed item leaves an orphan vector
        self.assertIn("removed items", ci.vectors_state(cm, "designcraft", data))

    def test_text_format_change_invalidates_vectors(self):
        cm, data = FakeCM(self.tmp.name), self._data()
        ci.build_vectors(cm, "designcraft", data)
        ci.TEXT_VERSION += 1
        self.addCleanup(setattr, ci, "TEXT_VERSION", ci.TEXT_VERSION - 1)
        self.assertIn("text format changed", ci.vectors_state(cm, "designcraft", data))

    def test_embed_text_uses_description_keywords_or_falls_back_to_params(self):
        d = self._data()["items"]
        self.assertIn("Keywords: thread, link", ci.embed_text(d["cmd:frame.thread"]))
        plain = {"kind": "cmd", "id": "x.y", "label": "Label", "desc": "", "keys": [], "params": "{a?: int}"}
        self.assertEqual(ci.embed_text(plain), "x.y: Label. {a?: int}")


class FindTests(Base):
    def _build(self):
        cm = FakeCM(self.tmp.name)
        data = VectorTests._data(self)
        ci.save_desc(cm, "designcraft", data)
        ci.build_vectors(cm, "designcraft", data)
        return cm

    def test_vector_order_is_used_when_system_one_is_down(self):
        cm = self._build()
        self.kb.fail = True
        results, note = ci.find(cm, "designcraft", "thread frames link text flows", top=2)
        self.assertEqual(results[0][0]["id"], "frame.thread")
        self.assertIn("System One unavailable", note)

    def test_system_one_probabilities_decide_the_final_order(self):
        cm = self._build()

        def answer(state, questions):
            options = list(questions["f"]["criteria"])
            self.assertTrue(all(o[1] == ":" for o in options))      # "c:<id>" for commands, "t:<name>" for tools
            probs = {o: (0.9 if o == "t:render_page" else 0.05) for o in options}
            return {"f": {"type": "choice", "choice": "t:render_page", "probabilities": probs}}
        self.kb.answer = answer
        results, note = ci.find(cm, "designcraft", "close the document", top=3)
        self.assertEqual(results[0][0]["id"], "render_page")
        self.assertIsNone(note)

    def test_kind_filter_and_missing_index(self):
        cm = self._build()
        self.kb.fail = True
        results, _ = ci.find(cm, "designcraft", "anything", top=5, kind="tool")
        self.assertEqual([r[0]["kind"] for r in results], ["tool"])
        with self.assertRaises(Fail):
            ci.find(FakeCM(self.tmp.name), "lightcraft", "x")


class NotesAreTipsTests(Base):
    """A note is advice next to the answer: judged by its own yes/no question, shown after the ranked items."""

    def _build(self):
        cm = FakeCM(self.tmp.name)
        items = {}
        for i in range(30):
            items["cmd:layer.op%d" % i] = {"kind": "cmd", "id": "layer.op%d" % i, "label": "Layer op %d" % i, "desc": "Changes every layer %d." % i, "keys": ["layer"], "sig": str(i), "params": ""}
        items["note:scripts"] = {"kind": "note", "id": "scripts", "label": "Bulk work", "desc": "Write one script.", "keys": ["script", "loop"], "sig": "n1", "params": "", "see": []}
        items["note:other"] = {"kind": "note", "id": "other", "label": "Rendering", "desc": "Pass comp.", "keys": ["render"], "sig": "n2", "params": "", "see": []}
        ci.save_desc(cm, "designcraft", {"items": items})
        ci.build_vectors(cm, "designcraft", {"items": items})
        return cm, items

    def _answer(self, tip_probs, seen):
        def answer(state, questions):
            seen["questions"] = questions
            out = {"f": {"type": "choice", "choice": "c:layer.op3", "probabilities": {o: (0.9 if o == "c:layer.op3" else 0.001) for o in questions["f"]["criteria"]}}}
            for name in questions:
                if name.startswith("t"):
                    text = questions[name]["instructions"]
                    p = next((v for k, v in tip_probs.items() if k in text), 0.01)
                    out[name] = {"type": "noul", "noul": p}
            return out
        return answer

    def test_notes_are_not_options_of_the_choice_and_relevant_ones_follow_the_items(self):
        cm, _ = self._build()
        seen = {}
        self.kb.answer = self._answer({"Bulk work": 0.93, "Rendering": 0.1}, seen)
        results, note = ci.find(cm, "designcraft", "change every layer in the comp with a loop", top=3, shortlist=8)
        self.assertTrue(all(o.startswith("c:") for o in seen["questions"]["f"]["criteria"]))   # only commands compete
        self.assertEqual([it["kind"] for it, _ in results], ["cmd", "cmd", "cmd", "note"])
        self.assertEqual(results[-1][0]["id"], "scripts")
        self.assertAlmostEqual(results[-1][1], 0.93)
        self.assertEqual(sum(1 for it, _ in results if it["kind"] == "note"), 1)               # the irrelevant note is absent
        self.assertIsNone(note)

    def test_a_tip_below_the_threshold_is_not_shown(self):
        cm, _ = self._build()
        self.kb.answer = self._answer({"Bulk work": ci.TIP_MIN - 0.01, "Rendering": 0.0}, {})
        results, _ = ci.find(cm, "designcraft", "change every layer", top=3, shortlist=8)
        self.assertEqual([it["kind"] for it, _ in results], ["cmd", "cmd", "cmd"])

    def test_without_the_judge_only_a_note_that_is_almost_the_query_is_shown(self):
        cm, items = self._build()
        self.kb.fail = True
        close_query = ci.embed_text(items["note:scripts"])             # cosine 1.0 with the note
        results, note = ci.find(cm, "designcraft", close_query, top=3)
        self.assertIn("note", [it["kind"] for it, _ in results])
        self.assertIn("System One unavailable", note)
        far, _ = ci.find(cm, "designcraft", "zzz unrelated words", top=3)
        self.assertNotIn("note", [it["kind"] for it, _ in far])

    def test_kind_note_searches_the_notes_themselves(self):
        cm, _ = self._build()
        self.kb.fail = True
        results, _ = ci.find(cm, "designcraft", "script loop", top=5, kind="note")
        self.assertEqual({it["kind"] for it, _ in results}, {"note"})


class NotesCommandTests(Base):
    def test_find_without_tips_never_returns_notes_and_asks_no_tip_questions(self):
        cm, _ = NotesAreTipsTests._build(self)
        seen = {}
        self.kb.answer = NotesAreTipsTests._answer(self, {"Bulk work": 0.99}, seen)
        results, _ = ci.find(cm, "designcraft", "change every layer", top=3, shortlist=8, tips=False)
        self.assertEqual({it["kind"] for it, _ in results}, {"cmd"})
        self.assertEqual([k for k in seen["questions"] if k.startswith("t")], [])

    def test_notes_listing_and_search_print_the_notes_only(self):
        import contextlib
        import io

        class Args:
            slot, query, top, no_rerank = "designcraft", [], 3, False
        cm, _ = NotesAreTipsTests._build(self)
        cm.check_slot = lambda slot: None
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ci.run_notes(cm, Args)
        listing = buf.getvalue()
        self.assertIn("scripts", listing)
        self.assertIn("Bulk work", listing)
        self.assertNotIn("layer.op", listing)
        self.kb.fail = True
        Args.query = ["script", "loop"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ci.run_notes(cm, Args)
        self.assertIn("1. note scripts", buf.getvalue())
        self.assertNotIn("layer.op", buf.getvalue())


class JudgeOptionTests(Base):
    def test_other_kinds_keep_label_and_the_start_of_the_description(self):
        cmd_item = {"kind": "cmd", "id": "a.b", "label": "Thread Frames", "desc": "Links text frames so the story flows. " * 10}
        text = ci.judge_option(cmd_item)
        self.assertTrue(text.startswith("Thread Frames - Links text frames"))
        self.assertLessEqual(len(text), 130)


class PendingTests(Base):
    def test_chunks_are_written_and_outputs_collected(self):
        live = {"cmd:a.%d" % i: {"id": "a.%d" % i, "label": "L%d" % i, "menu": "File", "params": "{}", "kind": "cmd"} for i in range(130)}
        work = os.path.join(self.tmp.name, "work")
        n = ci.prepare_pending(FakeCM(self.tmp.name), "designcraft", live, sorted(live), work, size=60)
        self.assertEqual(n, 3)
        with open(os.path.join(work, "chunks", "chunk_00.json"), encoding="utf-8") as fh:
            first = json.load(fh)
        self.assertEqual(len(first), 60)
        self.assertEqual(set(first[0]), {"key", "kind", "id", "label", "menu", "params"})
        with open(os.path.join(work, "out", "desc_00.json"), "w", encoding="utf-8") as fh:
            json.dump([{"key": first[0]["key"], "desc": " Does a thing. ", "keys": ["One", " two "]}, {"key": "bad"}], fh)
        with open(os.path.join(work, "out", "desc_01.json"), "w", encoding="utf-8") as fh:
            fh.write("not json")
        got = ci.collect_pending(work)
        self.assertEqual(list(got), [first[0]["key"]])
        self.assertEqual(got[first[0]["key"]], {"desc": "Does a thing.", "keys": ["one", "two"]})


class OptionTermsTests(Base):
    def test_parameter_names_and_option_values_become_words(self):
        doc = '{layer?, kind: group|rect|trim|wiggle|zigZag, name?: string, spread?: 0..9, "grainType":"soft|clumped"}'
        words = ci.option_terms(doc).split()
        for w in ("kind", "trim", "wiggle", "zig", "zag", "soft", "clumped", "grain", "type", "name"):
            self.assertIn(w, words)
        self.assertNotIn("string", words)       # type words are noise

    def test_values_reach_the_embedded_text_of_described_commands_only_when_present(self):
        item = {"kind": "cmd", "id": "layer.addShapeItem", "label": "Add", "desc": "Adds a shape operator.", "keys": ["shape"],
                "params": "{kind: rect|trim|wiggle}"}
        self.assertIn("Options:", ci.embed_text(item))
        self.assertIn("wiggle", ci.embed_text(item))
        self.assertNotIn("Options:", ci.embed_text(dict(item, params="{}")))

    def test_the_cap_keeps_long_lists_short(self):
        doc = "{kind: " + "|".join("value%d" % i for i in range(200)) + "}"
        self.assertLessEqual(len(ci.option_terms(doc)), 300)


class RegistryAndNotesTests(Base):
    def _cm(self):
        cm = FakeCM(self.tmp.name, commands=[cmd("layer.addShapeItem", "Add", "{kind: trim|wiggle}")])
        cm.effects = [{"id": "ec.noise.fractal", "name": "Fractal Noise", "category": "Noise & Grain", "params": [{"id": "amount", "name": "Amount"}, {"id": "scale", "name": "Scale"}]},
                      {"name": "no id, skipped"}]
        return cm

    def test_effects_of_a_registry_become_items_with_a_fingerprint(self):
        live = ci.live_corpus(self._cm(), "effectcraft")
        e = live["effect:ec.noise.fractal"]
        self.assertEqual((e["kind"], e["label"], e["menu"]), ("effect", "Fractal Noise", "Noise & Grain"))
        self.assertEqual(e["params"], "{Amount, Scale}")
        self.assertIn("add_effect", e["use"])
        self.assertEqual(len([k for k in live if k.startswith("effect:")]), 1)
        self.assertNotIn("effect:ec.noise.fractal", ci.live_corpus(FakeCM(self.tmp.name, commands=[]), "designcraft"))   # only slots with a registry

    def test_effects_need_a_description_like_commands(self):
        live = ci.live_corpus(self._cm(), "effectcraft")
        stored = {k: dict(v, desc="d") for k, v in live.items()}
        stored["effect:ec.noise.fractal"]["desc"] = ""
        self.assertEqual(ci.classify(live, stored)["undescribed"], ["effect:ec.noise.fractal"])

    def _write_notes(self, cm, notes):
        os.makedirs(ci.index_dir(cm), exist_ok=True)
        with open(ci.notes_path(cm, "designcraft"), "w", encoding="utf-8") as fh:
            json.dump(notes, fh)

    def test_notes_file_is_the_source_of_truth_for_note_items(self):
        cm = FakeCM(self.tmp.name)
        self.assertEqual(ci.notes_items(cm, "designcraft"), {})
        self._write_notes(cm, [{"id": "a", "title": "Trap A", "text": "Do X first.", "keys": ["Place Picture"], "see": ["place_image"]}])
        first = ci.notes_items(cm, "designcraft")["note:a"]
        self.assertEqual((first["kind"], first["label"], first["desc"], first["keys"], first["see"]), ("note", "Trap A", "Do X first.", ["place picture"], ["place_image"]))
        self._write_notes(cm, [{"id": "a", "title": "Trap A", "text": "Do Y first.", "keys": [], "see": []}])
        second = ci.notes_items(cm, "designcraft")["note:a"]
        self.assertNotEqual(first["sig"], second["sig"])          # an edited note is a "changed" item for the doctor

    def test_sync_embeds_notes_without_pi_and_drops_notes_removed_from_the_file(self):
        cm = FakeCM(self.tmp.name)
        live = {"cmd:keep": {"kind": "cmd", "id": "keep", "label": "Keep", "menu": "", "params": "", "sig": "S1"}}
        ci.save_desc(cm, "designcraft", {"items": {"cmd:keep": dict(live["cmd:keep"], desc="Keeps.", keys=[], src="pi")}})
        self._write_notes(cm, [{"id": "a", "title": "Trap A", "text": "Do X first.", "keys": ["k"], "see": []}])
        self.patch("live_corpus", lambda cm_, slot: dict(live))
        self.patch("with_app", lambda cm_, slot, fn: fn())
        self.patch("run_pi", lambda workdir, count: self.fail("notes and unchanged commands must not go to Pi"))

        class Args:
            no_describe = False
        ci.sync_slot(cm, "designcraft", Args)
        data = ci.load_desc(cm, "designcraft")
        self.assertEqual(data["items"]["note:a"]["desc"], "Do X first.")
        self.assertEqual(data["items"]["note:a"]["src"], "note")
        self.assertEqual(ci.vectors_state(cm, "designcraft", data), "ok")
        self._write_notes(cm, [])                                   # delete the note from the file
        ci.sync_slot(cm, "designcraft", Args)
        self.assertNotIn("note:a", ci.load_desc(cm, "designcraft")["items"])

    def test_find_prints_notes_in_full_and_effects_with_their_use_line(self):
        import io
        import contextlib
        notes = {"kind": "note", "id": "a", "label": "Trap A", "desc": "Do X first. Then Y.", "see": ["place_image"]}
        effect = {"kind": "effect", "id": "ec.noise.fractal", "label": "Fractal Noise", "desc": "Adds fractal noise.", "params": "{Amount}", "use": "craftmcp.py call effectcraft add_effect effect=<id> layer=<layer>"}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ci.print_find(FakeCM(self.tmp.name), "effectcraft", [(notes, 0.9), (effect, 0.5)], None)
        out = buf.getvalue()
        self.assertIn("Do X first. Then Y.", out)
        self.assertIn("see: place_image", out)
        self.assertIn("use:  craftmcp.py call effectcraft add_effect", out)


class BrokenSeeTests(Base):
    def test_notes_must_point_at_ids_that_exist(self):
        live = {
            "cmd:file.close": {"kind": "cmd", "id": "file.close"}, "tool:run_script": {"kind": "tool", "id": "run_script"},
            "effect:ec.noise.fractal": {"kind": "effect", "id": "ec.noise.fractal"},
            "note:ok": {"kind": "note", "id": "ok", "see": ["file.close", "run_script", "ec.noise.fractal"]},
            "note:stale": {"kind": "note", "id": "stale", "see": ["file.close", "file.closeEverything"]},
            "note:alone": {"kind": "note", "id": "alone", "see": []},
        }
        self.assertEqual(ci.broken_see(live), {"note:stale": ["file.closeEverything"]})

    def test_a_note_cannot_vouch_for_another_note(self):
        live = {"note:a": {"kind": "note", "id": "a", "see": ["b"]}, "note:b": {"kind": "note", "id": "b", "see": []}}
        self.assertEqual(ci.broken_see(live), {"note:a": ["b"]})


class SyncTests(Base):
    """The whole drift -> fix cycle with a fake app and a fake Pi."""

    def test_sync_drops_removed_describes_new_and_changed_and_rebuilds_vectors(self):
        cm = FakeCM(self.tmp.name)
        old = {"items": {
            "cmd:keep": {"kind": "cmd", "id": "keep", "label": "Keep", "menu": "", "params": "", "sig": "S1", "desc": "Keeps.", "keys": ["keep"], "src": "pi"},
            "cmd:edit": {"kind": "cmd", "id": "edit", "label": "Edit", "menu": "", "params": "{a}", "sig": "OLD", "desc": "Old text.", "keys": ["edit"], "src": "pi"},
            "cmd:gone": {"kind": "cmd", "id": "gone", "label": "Gone", "menu": "", "params": "", "sig": "S3", "desc": "Gone.", "keys": [], "src": "pi"},
        }}
        ci.save_desc(cm, "designcraft", old)
        live = {
            "cmd:keep": {"kind": "cmd", "id": "keep", "label": "Keep", "menu": "", "params": "", "sig": "S1"},
            "cmd:edit": {"kind": "cmd", "id": "edit", "label": "Edit", "menu": "", "params": "{a, b}", "sig": "NEW"},
            "cmd:fresh": {"kind": "cmd", "id": "fresh", "label": "Fresh", "menu": "", "params": "", "sig": "S4"},
            "tool:t": {"kind": "tool", "id": "t", "label": "", "menu": "", "params": "{}", "sig": "S5", "text0": "A real tool."},
        }
        self.patch("live_corpus", lambda cm_, slot: live)
        self.patch("with_app", lambda cm_, slot, fn: fn())
        described = []

        def fake_pi(workdir, count):
            for f in sorted(os.listdir(os.path.join(workdir, "chunks"))):
                with open(os.path.join(workdir, "chunks", f), encoding="utf-8") as fh:
                    rows = json.load(fh)
                described.extend(r["id"] for r in rows)
                with open(os.path.join(workdir, "out", f.replace("chunk", "desc")), "w", encoding="utf-8") as fh:
                    json.dump([{"key": r["key"], "desc": "New text for %s." % r["id"], "keys": ["k1", "k2"]} for r in rows], fh)
            return True
        self.patch("run_pi", fake_pi)

        class Args:
            no_describe = False
        ci.sync_slot(cm, "designcraft", Args)
        data = ci.load_desc(cm, "designcraft")
        self.assertEqual(sorted(described), ["edit", "fresh"])             # only new and changed commands go to Pi
        self.assertNotIn("cmd:gone", data["items"])
        self.assertEqual(data["items"]["cmd:keep"]["desc"], "Keeps.")      # untouched items keep their description
        self.assertEqual(data["items"]["cmd:edit"]["desc"], "New text for edit.")
        self.assertEqual(data["items"]["cmd:edit"]["sig"], "NEW")
        self.assertEqual(data["items"]["tool:t"]["desc"], "A real tool.")  # tools use the MCP text, no Pi
        self.assertEqual(ci.classify(live, data["items"]), {"new": [], "removed": [], "changed": [], "unsigned": [], "undescribed": []})
        self.assertEqual(ci.vectors_state(cm, "designcraft", data), "ok")

    def test_no_describe_keeps_old_text_and_marks_new_commands_undescribed(self):
        cm = FakeCM(self.tmp.name)
        ci.save_desc(cm, "designcraft", {"items": {}})
        live = {"cmd:fresh": {"kind": "cmd", "id": "fresh", "label": "Fresh", "menu": "", "params": "{x}", "sig": "S"}}
        self.patch("live_corpus", lambda cm_, slot: live)
        self.patch("with_app", lambda cm_, slot, fn: fn())

        class Args:
            no_describe = True
        ci.sync_slot(cm, "designcraft", Args)
        data = ci.load_desc(cm, "designcraft")
        self.assertEqual(data["items"]["cmd:fresh"]["desc"], "")
        self.assertEqual(ci.classify(live, data["items"])["undescribed"], ["cmd:fresh"])   # doctor keeps reporting it
        self.assertEqual(ci.vectors_state(cm, "designcraft", data), "ok")                # but it is searchable by label and params


if __name__ == "__main__":
    unittest.main()
