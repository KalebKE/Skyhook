(method_invocation !object name: (identifier) @name) @reference.call
(method_invocation object: (_) @qualifier name: (identifier) @name) @reference.call
(object_creation_expression type: (type_identifier) @name) @reference.call
(object_creation_expression type: (scoped_type_identifier (type_identifier) @name .)) @reference.call
