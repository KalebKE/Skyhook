(call_expression function: (identifier) @name) @reference.call
(call_expression function: (member_expression object: (_) @qualifier property: (property_identifier) @name)) @reference.call
(new_expression constructor: (identifier) @name) @reference.call
