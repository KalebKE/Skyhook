(import_statement source: (string (string_fragment) @name))
((call_expression function: (identifier) @_fn arguments: (arguments (string (string_fragment) @name))) (#eq? @_fn "require"))
