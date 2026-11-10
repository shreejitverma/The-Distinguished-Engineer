"""Update masks (AIP-134, AIP-161) versus Pydantic `model_fields_set` for partial updates."""

from collections.abc import Mapping
from typing import Annotated, Any, Final, cast, get_origin

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError

WILDCARD: Final = "*"
# `update_time` is OUTPUT_ONLY, which AIP-161 and AIP-203 require the service to ignore in an
# update mask; `name` is IDENTIFIER (taken from the URL, immutable, absent from `BookPatch`),
# so a mask path to it can only name the unchanged value, which AIP-203 says to ignore.
NON_WRITABLE: Final = frozenset({"name", "update_time"})
_MISSING: Final = object()


class Author(BaseModel):
    given_name: str = ""
    family_name: str = ""


class Book(BaseModel):
    """The stored resource; every field has a default except the required `title`."""

    name: str
    title: str
    rating: int | None = None
    author: Author = Field(default_factory=Author)
    tags: list[str] = Field(default_factory=list[str])
    update_time: int = 0


class AuthorPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    given_name: str | None = None
    family_name: str | None = None


class BookPatch(BaseModel):
    """The `PATCH` body: every field optional so omission is representable."""

    model_config = ConfigDict(extra="forbid")
    title: str | None = None
    rating: int | None = None
    author: AuthorPatch | None = None
    tags: list[str] | None = None
    update_time: int | None = None


class InvalidFieldMaskError(ValueError):
    """A mask path that cannot exist on the resource (AIP-161 `INVALID_ARGUMENT`)."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"INVALID_ARGUMENT: update_mask path {path!r}: {reason}")
        self.path = path


def parse_update_mask(raw: str | None) -> tuple[str, ...] | None:
    """Decode the ProtoJSON string form: comma-separated lowerCamelCase paths.

    `None` (parameter absent) and `()` (parameter present but empty) are different requests.
    """
    if raw is None:
        return None
    if raw == "":
        return ()
    return tuple(_camel_to_snake(path) for path in raw.split(","))


def _camel_to_snake(path: str) -> str:
    if "_" in path:
        raise InvalidFieldMaskError(path, "JSON mask paths are lowerCamelCase, '_' is not allowed")
    return "".join(f"_{char.lower()}" if char.isupper() else char for char in path)


def canonical_mask(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Drop duplicates and paths covered by an ancestor path, then sort."""
    unique = sorted(set(paths))
    return tuple(
        path
        for path in unique
        if not any(path.startswith(other + ".") for other in unique if other != path)
    )


def _check_path(path: str) -> None:
    model: type[BaseModel] = Book
    segments = path.split(".")
    for index, segment in enumerate(segments):
        field = model.model_fields.get(segment)
        if field is None:
            raise InvalidFieldMaskError(path, f"unknown field {segment!r}")
        if index == len(segments) - 1:
            return
        annotation = field.annotation
        if not (
            get_origin(annotation) is None
            and isinstance(annotation, type)
            and issubclass(annotation, BaseModel)
        ):
            raise InvalidFieldMaskError(path, f"{segment!r} is not a singular message field")
        model = annotation


def resolve_mask(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Validate a mask and return the canonical writable paths it selects."""
    if WILDCARD in paths:
        if paths != (WILDCARD,):
            raise InvalidFieldMaskError(WILDCARD, "the wildcard must be the only path")
        return tuple(name for name in Book.model_fields if name not in NON_WRITABLE)
    for path in paths:
        _check_path(path)
    return tuple(p for p in canonical_mask(paths) if p.split(".", 1)[0] not in NON_WRITABLE)


def mask_from_fields_set(patch: BaseModel, prefix: str = "") -> tuple[str, ...]:
    """Derive an implied mask from the keys the client sent, including explicit nulls."""
    paths: list[str] = []
    for name in sorted(patch.model_fields_set):
        value: object = getattr(patch, name)
        if isinstance(value, BaseModel) and value.model_fields_set:
            paths.extend(mask_from_fields_set(value, f"{prefix}{name}."))
        else:
            paths.append(f"{prefix}{name}")
    return tuple(paths)


def implied_mask_populated(body: Mapping[str, object], prefix: str = "") -> tuple[str, ...]:
    """AIP-134 implied mask: only fields with a non-empty value (proto3 implicit presence)."""
    paths: list[str] = []
    for name, value in sorted(body.items()):
        nested = _object_map_or_none(value)
        if nested is not None:
            paths.extend(implied_mask_populated(nested, f"{prefix}{name}."))
        elif not (value is None or (isinstance(value, str | int | float | list) and not value)):
            paths.append(f"{prefix}{name}")
    return tuple(paths)


def _object_map_or_none(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    items = cast("Mapping[object, object]", value)
    return {str(key): item for key, item in items.items()}


def _as_object_map(value: object) -> dict[str, object]:
    mapping = _object_map_or_none(value)
    if mapping is None:
        raise TypeError(f"expected a JSON object, got {type(value).__name__}")
    return mapping


def _strip_none(value: object) -> object:
    """Treat JSON `null` as "clear to default": remove it so the resource default applies."""
    mapping = _object_map_or_none(value)
    if mapping is None:
        return value
    return {key: _strip_none(item) for key, item in mapping.items() if item is not None}


def _lookup(data: dict[str, object], path: str) -> object:
    node: object = data
    for segment in path.split("."):
        mapping = _object_map_or_none(node)
        if mapping is None or segment not in mapping:
            return _MISSING
        node = mapping[segment]
    return node


def _write(data: dict[str, Any], path: str, value: object) -> None:
    *parents, leaf = path.split(".")
    node = data
    for segment in parents:
        node = node.setdefault(segment, {})
    if value is _MISSING:
        node.pop(leaf, None)
    else:
        node[leaf] = value


def apply_update_mask(stored: Book, patch: BookPatch, mask: tuple[str, ...]) -> Book:
    """Copy exactly the masked paths from `patch` onto `stored`; absent or null -> default.

    A path names a whole subtree: `author` replaces the author, `author.given_name` touches
    one leaf.  Raises `InvalidFieldMaskError` or pydantic `ValidationError`.
    """
    paths = resolve_mask(mask)
    if not paths:
        return stored
    supplied = _as_object_map(_strip_none(patch.model_dump(exclude_unset=True)))
    merged = stored.model_dump()
    for path in paths:
        _write(merged, path, _lookup(supplied, path))
    merged["update_time"] = stored.update_time + 1
    return Book.model_validate(merged)


def naive_model_copy_update(stored: Book, patch: BookPatch) -> Book:
    """The common `model_copy(update=model_dump(exclude_unset=True))` recipe, for contrast."""
    return stored.model_copy(update=patch.model_dump(exclude_unset=True))


def project(resource: Mapping[str, object], paths: tuple[str, ...]) -> dict[str, object]:
    """Read-side mask: keep only the selected paths of a dumped resource."""
    result: dict[str, Any] = {}
    source = _as_object_map(resource)
    for path in canonical_mask(paths):
        value = _lookup(source, path)
        if value is not _MISSING:
            _write(result, path, value)
    return result


def build_app(store: dict[str, Book]) -> FastAPI:
    """`PATCH /v1/books/{book_id}?updateMask=...` with an implied mask from `model_fields_set`."""
    app = FastAPI()

    @app.patch("/v1/books/{book_id}")
    async def update_book(  # pyright: ignore[reportUnusedFunction]
        book_id: str,
        patch: BookPatch,
        update_mask: Annotated[str | None, Query(alias="updateMask")] = None,
    ) -> Book:
        stored = store.get(book_id)
        if stored is None:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        try:
            explicit = parse_update_mask(update_mask)
            mask = explicit if explicit is not None else mask_from_fields_set(patch)
            updated = apply_update_mask(stored, patch, mask)
        except InvalidFieldMaskError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValidationError as exc:
            fields = sorted(".".join(str(part) for part in err["loc"]) for err in exc.errors())
            raise HTTPException(status_code=400, detail=f"INVALID_ARGUMENT: {fields}") from exc
        store[book_id] = updated
        return updated

    return app
