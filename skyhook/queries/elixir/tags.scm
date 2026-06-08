((call target: (identifier) @_kw (arguments (call target: (identifier) @name))) (#any-of? @_kw "def" "defp" "defmacro")) @definition.function
((call target: (identifier) @_kw (arguments (alias) @name)) (#eq? @_kw "defmodule")) @definition.module
