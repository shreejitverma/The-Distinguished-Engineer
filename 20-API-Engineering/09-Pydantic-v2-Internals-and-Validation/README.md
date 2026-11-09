---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# Pydantic v2 Internals and Validation

Dissects Pydantic v2 (`2.14.3`, released 2026-11-05) and its Rust execution engine `pydantic-core` (`2.50.4`, released 2026-11-05).
Traces how Python type annotations compile into a `CoreSchema` dictionary consumed by a Rust validator/serializer tree, why `model_validate_json` avoids intermediate `dict` allocations, and how lax versus strict modes operate.
Covers `TypeAdapter`, `Annotated` functional validators, discriminated unions, PEP 695 generics, recursive schemas, custom serializers, `GenerateJsonSchema`, `pydantic-settings` (`2.16.2`), performance benchmarking against `msgspec` (`0.22.0`) and `orjson` (`3.13.0`), and v1 (`1.10.27`) migration.

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [Pydantic v2 Architecture: How Python Type Hints Compile to __pydantic_core_schema__ and Rust SchemaValidator (pydantic-core 2.50.4)](Pydantic-v2-Architecture-CoreSchema-and-Rust-Pydantic-Core.md)
2. [Validation Modes in Pydantic v2: Lax Coercion vs Strict Mode (strict=True, StrictInt, StrictStr) and ConfigDict](Lax-vs-Strict-Validation-Modes-and-Coercion-Tables.md)
3. [Why model_validate_json Is Faster and Safer Than model_validate(json.loads()): Zero Intermediate Python Dicts in Rust](Single-Pass-JSON-Validation-Model-Validate-JSON-vs-JSON-Loads.md)
4. [TypeAdapter: Validating and Serializing Lists, TypedDicts, Unions, and Primitives Without a BaseModel Wrapper](TypeAdapter-for-Validating-and-Serializing-Arbitrary-Types.md)
5. [Composable Type Constraints with typing.Annotated, Field(), StringConstraints, and Reusable Domain Types](Annotated-Constraints-Field-Metadata-and-Custom-Types.md)
6. [Functional Field Validators: BeforeValidator, AfterValidator, WrapValidator, and PlainValidator Execution Order](Functional-Validators-Before-After-Wrap-and-Plain.md)
7. [Model-Level Validators (@model_validator mode='before'|'after'|'wrap') and Passing Runtime State via ValidationInfo.context](Model-Validators-ValidationInfo-Context-and-Cross-Field-Invariants.md)
8. [Field Aliases in Depth: validation_alias, serialization_alias, AliasPath, AliasChoices, and AliasGenerator](Aliases-AliasPath-AliasChoices-and-Bidirectional-Field-Mapping.md)
9. [High-Performance Polymorphism: Discriminated Unions with String Discriminators, Tag(), and Callable Discriminator()](Discriminated-Unions-Tag-and-Callable-Discriminator-Dispatch.md)
10. [Generic Pydantic Models: PEP 695 Type Parameter Syntax and Subclass Specialization](Generic-Models-PEP-695-Syntax-and-Variance.md)
11. [Recursive Schemas, Forward References, model_rebuild(), and Cyclic Reference Detection in pydantic-core](Recursive-Models-Forward-References-and-Cyclic-Reference-Guard.md)
12. [Serialization Internals: model_dump vs model_dump_json, @field_serializer, @model_serializer, and when_used](Custom-Serialization-Field-Model-and-Wrap-Serializers.md)
13. [Validating from ORM Objects (from_attributes=True) and Preventing Accidental SQLAlchemy Lazy-Load Queries During Serialization](ORM-Mode-From-Attributes-and-Preventing-Accidental-Lazy-Loads.md)
14. [Customizing JSON Schema Generation: model_json_schema(), validation vs serialization Mode, and Subclassing GenerateJsonSchema](JSON-Schema-Generation-Internals-and-GenerateJsonSchema.md)
15. [Authoring Zero-Overhead Custom Types with __get_pydantic_core_schema__ and __get_pydantic_json_schema__](Low-Level-Custom-Types-with-Get-Pydantic-Core-Schema.md)
16. [Configuration Management with pydantic-settings (2.16.2): Env Vars, Nested Delimiters, Docker Secrets, and Custom Sources](Pydantic-Settings-Environment-Secret-Files-and-Source-Priority.md)
17. [Pydantic v2 Performance Engineering: fail_fast, Avoiding Python Validators, and Benchmarking Against msgspec (0.22.0) and orjson (3.13.0)](Pydantic-v2-Performance-Engineering-vs-MsgSpec-and-ORJSON.md)
18. [Migrating from Pydantic v1 (1.10.27) to v2 (2.14.3): Behavioral Breaking Changes, pydantic.v1 Bridge, and Incremental Strangler Patterns](Migrating-Large-Codebases-from-Pydantic-v1-to-v2.md)
