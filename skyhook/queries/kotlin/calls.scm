(call_expression (identifier) @name) @reference.call

(call_expression
  (unary_expression
    (identifier) @name .)) @reference.call

(call_expression
  (navigation_expression
    (_) @qualifier
    .
    (identifier) @name .)) @reference.call
