import unittest

from skyhook import grammars
from skyhook.astextract import extract_file


def _has(lang):
    return grammars.get_language(lang, "") is not None


@unittest.skipUnless(_has("Python"), "python grammar not installed")
class AstExtractPythonTests(unittest.TestCase):
    SRC = b"import os\nfrom a.b import c\ndef foo(x):\n    return bar(x)\nclass A:\n    def run(self):\n        return foo(1)\n"

    def test_defs_with_scope_and_method(self):
        fa = extract_file("m.py", "Python", self.SRC)
        self.assertTrue(fa.parsed)
        names = {(d.name, d.structural_kind, d.scope) for d in fa.defs}
        self.assertIn(("foo", "function", None), names)
        self.assertIn(("A", "class", None), names)
        self.assertIn(("run", "method", "A"), names)

    def test_calls_and_imports(self):
        fa = extract_file("m.py", "Python", self.SRC)
        callees = {c.callee_name for c in fa.calls}
        self.assertIn("bar", callees)
        self.assertIn("foo", callees)
        self.assertIn("os", [i.target for i in fa.imports])
        self.assertIn("a.b", [i.target for i in fa.imports])
        # call enclosing scope
        bar_call = next(c for c in fa.calls if c.callee_name == "bar")
        self.assertEqual(bar_call.enclosing, "foo")


@unittest.skipUnless(_has("Swift"), "swift grammar not installed")
class AstExtractSwiftTests(unittest.TestCase):
    def test_swift_class_method_call(self):
        src = b"import UIKit\nclass FooView {\n    func render() { bar() }\n}\nfunc bar() {}\n"
        fa = extract_file("F.swift", "Swift", src)
        names = {(d.name, d.structural_kind, d.scope) for d in fa.defs}
        self.assertIn(("FooView", "class", None), names)
        self.assertIn(("render", "method", "FooView"), names)
        self.assertIn("UIKit", [i.target for i in fa.imports])
        self.assertIn("bar", {c.callee_name for c in fa.calls})


class AstExtractBreadthTests(unittest.TestCase):
    """Each language: a def, a call, and an import extract (grammar-guarded)."""

    CASES = {
        "Java": ("F.java", b"package a;\nimport b.C;\nclass Foo { int run(int x){ return baz(x); } }\n",
                 "Foo", "baz", "b.C"),
        "JavaScript": ("a.js", b"import {x} from 'react';\nfunction foo(a){ return bar(a); }\n",
                       "foo", "bar", "react"),
        "TypeScript": ("a.ts", b"import React from 'react';\nfunction foo(a:number){ return bar(a); }\n",
                       "foo", "bar", "react"),
        "Go": ("m.go", b'package main\nimport "fmt"\nfunc foo() { bar() }\n', "foo", "bar", "fmt"),
        "Elixir": ("m.ex", b"defmodule Foo do\n  alias A.B\n  def run(x), do: baz(x)\nend\n",
                   "Foo", "baz", "A.B"),
        "Kotlin": ("V.kt", b"package a\nimport b.C\nfun baz(x: Int) = x\nfun run(x: Int) = baz(x)\n",
                   "run", "baz", "b.C"),
    }

    def test_each_language(self):
        for lang, (path, src, want_def, want_call, want_import) in self.CASES.items():
            with self.subTest(language=lang):
                if not _has(lang):
                    self.skipTest(f"{lang} grammar not installed")
                fa = extract_file(path, lang, src)
                self.assertTrue(fa.parsed, f"{lang} should parse")
                self.assertIn(want_def, {d.name for d in fa.defs}, f"{lang} def")
                self.assertIn(want_call, {c.callee_name for c in fa.calls}, f"{lang} call")
                self.assertIn(want_import, {i.target for i in fa.imports}, f"{lang} import")


class AstExtractDegradeTests(unittest.TestCase):
    def test_unsupported_language_is_empty(self):
        fa = extract_file("x.rb", "Ruby", b"def foo; end")
        self.assertTrue(fa.empty())


if __name__ == "__main__":
    unittest.main()
