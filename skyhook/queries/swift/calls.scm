(call_expression (simple_identifier) @name) @reference.call

(call_expression
  (navigation_expression
    (_) @qualifier
    (navigation_suffix (simple_identifier) @name))) @reference.call
