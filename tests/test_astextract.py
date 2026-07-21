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


@unittest.skipUnless(_has("Kotlin"), "kotlin grammar not installed")
class AstExtractKotlinEnrichmentTests(unittest.TestCase):
    SRC = (
        b"package com.acme.app\n"
        b"import com.acme.engine.Engine\n"
        b"class Car(private val engine: Engine) {\n"
        b"    companion object {\n"
        b"        fun create(): Car = Car(Engine())\n"
        b"    }\n"
        b"    fun drive() {\n"
        b"        engine.start()\n"
        b"        this.stop()\n"
        b"        engine.parts.oil.check()\n"
        b"        obj?.method()\n"
        b"        Telemetry.log(\"x\")\n"
        b"        listOf(1).map { it + 1 }.sum()\n"
        b"    }\n"
        b"    fun stop() {}\n"
        b"}\n"
    )

    def test_package_and_member_call_qualifiers(self):
        fa = extract_file("Car.kt", "Kotlin", self.SRC)
        self.assertEqual(fa.package, "com.acme.app")
        calls = {(c.callee_name, c.qualifier) for c in fa.calls}
        self.assertIn(("start", "engine"), calls)          # member call
        self.assertIn(("stop", "this"), calls)             # this-call
        self.assertIn(("check", "engine.parts.oil"), calls)  # dotted chain
        self.assertIn(("log", "Telemetry"), calls)         # object/qualified call
        self.assertIn(("Car", None), calls)                # constructor (bare)

    def test_lambda_chain_receiver_normalizes_to_none(self):
        fa = extract_file("Car.kt", "Kotlin", self.SRC)
        sum_call = next(c for c in fa.calls if c.callee_name == "sum")
        self.assertIsNone(sum_call.qualifier)  # `listOf(1).map {...}` is unusable

    def test_unnamed_companion_member_scopes_to_class(self):
        fa = extract_file("Car.kt", "Kotlin", self.SRC)
        create = next(d for d in fa.defs if d.name == "create")
        self.assertEqual(create.scope, "Car")  # companion untagged -> class scope


class AstExtractEnrichmentBreadthTests(unittest.TestCase):
    """Qualifier + package + constructor extraction per language (grammar-guarded)."""

    def test_java_constructor_package_and_qualifier(self):
        if not _has("Java"):
            self.skipTest("java grammar not installed")
        src = (
            b"package com.acme;\nimport com.acme.util.Log;\n"
            b"class Svc {\n    Svc() {}\n"
            b"    void run() { helper(); Log.d(\"x\"); new Svc(); }\n"
            b"    void helper() {}\n}\n"
        )
        fa = extract_file("Svc.java", "Java", src)
        self.assertEqual(fa.package, "com.acme")
        calls = {(c.callee_name, c.qualifier) for c in fa.calls}
        self.assertIn(("helper", None), calls)
        self.assertIn(("d", "Log"), calls)
        self.assertIn(("Svc", None), calls)  # new Svc()
        self.assertIn(("Svc", "method"), {(d.name, d.structural_kind) for d in fa.defs})

    def test_swift_member_call_qualifier(self):
        if not _has("Swift"):
            self.skipTest("swift grammar not installed")
        src = b"class Car {\n    func drive() { engine.start(); stop() }\n    func stop() {}\n}\n"
        fa = extract_file("Car.swift", "Swift", src)
        calls = {(c.callee_name, c.qualifier) for c in fa.calls}
        self.assertIn(("start", "engine"), calls)
        self.assertIn(("stop", None), calls)

    def test_python_qualifier_capture(self):
        if not _has("Python"):
            self.skipTest("python grammar not installed")
        src = b"import os\nclass A:\n    def run(self):\n        self.helper()\n        os.path.join('a')\n"
        fa = extract_file("m.py", "Python", src)
        calls = {(c.callee_name, c.qualifier) for c in fa.calls}
        self.assertIn(("helper", "self"), calls)
        self.assertIn(("join", "os.path"), calls)

    def test_go_package_and_selector_qualifier(self):
        if not _has("Go"):
            self.skipTest("go grammar not installed")
        src = b'package main\nimport "fmt"\nfunc run() { fmt.Println("x") }\n'
        fa = extract_file("main.go", "Go", src)
        self.assertEqual(fa.package, "main")
        self.assertIn(("Println", "fmt"), {(c.callee_name, c.qualifier) for c in fa.calls})

    def test_typescript_new_expression_and_qualifier(self):
        if not _has("TypeScript"):
            self.skipTest("typescript grammar not installed")
        src = b"class Svc {\n    run() { this.helper(); const x = new Svc(); }\n    helper() {}\n}\n"
        fa = extract_file("svc.ts", "TypeScript", src)
        calls = {(c.callee_name, c.qualifier) for c in fa.calls}
        self.assertIn(("helper", "this"), calls)
        self.assertIn(("Svc", None), calls)  # new Svc()


class AstExtractDegradeTests(unittest.TestCase):
    def test_unsupported_language_is_empty(self):
        fa = extract_file("x.rb", "Ruby", b"def foo; end")
        self.assertTrue(fa.empty())


if __name__ == "__main__":
    unittest.main()
