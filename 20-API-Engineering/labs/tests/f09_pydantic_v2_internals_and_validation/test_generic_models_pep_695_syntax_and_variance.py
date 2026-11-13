"""Generic Pydantic models: PEP 695 parameters, cached specialization, variance, and naming."""

import pickle
from typing import Any, Generic, TypeVar, cast

import httpx
import pytest
from pydantic import BaseModel, ValidationError, create_model

from api_labs.f09_pydantic_v2_internals_and_validation import generic_envelopes
from api_labs.f09_pydantic_v2_internals_and_validation.generic_envelopes import (
    AuditedPage,
    Batch,
    BoundedEnvelope,
    DefaultedEnvelope,
    Item,
    ItemPage,
    PaginatedResponse,
    Pair,
    PromoItem,
    Scalar,
    build_app,
    transient_specialization_is_collected,
)

_V = TypeVar("_V")


def _runtime(cls: object) -> type[Any]:
    """Erase a parametrized generic for class checks that type checkers reject statically."""
    return cast("type[Any]", cls)


def _promo_page() -> PaginatedResponse[PromoItem]:
    promo = PromoItem(sku="s-1", price_cents=5, promo_code="STAFF-ONLY")
    return PaginatedResponse[PromoItem](items=[promo], total=1)


@pytest.mark.unit
def test_pep695_class_declares_an_inferred_variance_typevar() -> None:
    (param,) = PaginatedResponse.__type_params__
    assert isinstance(param, TypeVar)
    assert param.__infer_variance__ is True
    assert (param.__covariant__, param.__contravariant__) == (False, False)
    assert Generic in PaginatedResponse.__mro__
    assert PaginatedResponse.__pydantic_generic_metadata__ == {
        "origin": None,
        "args": (),
        "parameters": (param,),
    }


@pytest.mark.unit
def test_specialization_is_a_cached_real_subclass() -> None:
    specialized = PaginatedResponse[Item]
    assert specialized is PaginatedResponse[Item]
    assert specialized.__name__ == "PaginatedResponse[Item]"
    assert specialized.__bases__ == (PaginatedResponse,)
    assert specialized.__pydantic_generic_metadata__ == {
        "origin": PaginatedResponse,
        "args": (Item,),
        "parameters": (),
    }
    page = specialized(items=[Item(sku="a", price_cents=1)], total=1)
    assert isinstance(page, PaginatedResponse)
    assert isinstance(page, _runtime(specialized))


@pytest.mark.unit
def test_parent_model_embeds_the_specialized_validator_as_prebuilt() -> None:
    class Holder(BaseModel):
        page: PaginatedResponse[Item]

    field_schema = cast("dict[str, Any]", Holder.__pydantic_core_schema__)["schema"]["fields"]
    assert field_schema["page"]["schema"]["cls"] is PaginatedResponse[Item]
    assert "PrebuiltValidator" in repr(Holder.__pydantic_validator__)
    assert 'title="PaginatedResponse[Item]"' in repr(PaginatedResponse[Item].__pydantic_validator__)


@pytest.mark.unit
def test_module_level_specialization_is_registered_for_pickle() -> None:
    assert vars(generic_envelopes)["PaginatedResponse[Item]"] is PaginatedResponse[Item]
    page = PaginatedResponse[Item](items=[Item(sku="a", price_cents=1)], total=1)
    assert pickle.loads(pickle.dumps(page)) == page  # noqa: S301

    local = Pair[bytes, bytes]
    assert "Pair[bytes, bytes]" not in vars(generic_envelopes)
    with pytest.raises(pickle.PicklingError):
        pickle.dumps(local(key=b"k", value=b"v"))


@pytest.mark.unit
def test_unparametrized_typevar_validates_as_any() -> None:
    unparametrized: type[PaginatedResponse[Any]] = PaginatedResponse
    loose = unparametrized.model_validate({"items": [1, "x", None], "total": 3})
    assert loose.items == [1, "x", None]
    with pytest.raises(ValidationError) as exc:
        PaginatedResponse[int].model_validate({"items": [1, "x"], "total": 2})
    assert [(e["type"], e["loc"]) for e in exc.value.errors()] == [
        ("int_parsing", ("items", 1)),
    ]


@pytest.mark.unit
def test_bound_validates_as_bound_but_serializes_as_any() -> None:
    unparametrized: type[BoundedEnvelope[Item]] = BoundedEnvelope
    from_dict = unparametrized.model_validate(
        {"data": {"sku": "a", "price_cents": 1, "promo_code": "P"}}
    )
    assert type(from_dict.data) is Item

    promo = PromoItem(sku="a", price_cents=1, promo_code="P")
    assert BoundedEnvelope(data=promo).model_dump()["data"] == {
        "sku": "a",
        "price_cents": 1,
        "promo_code": "P",
    }
    assert BoundedEnvelope[Item](data=promo).model_dump()["data"] == {"sku": "a", "price_cents": 1}


@pytest.mark.unit
def test_constraints_validate_as_a_union() -> None:
    unparametrized: type[Scalar[Any]] = Scalar
    scalar = unparametrized.model_validate({"value": "a"})
    assert scalar.value == "a"
    with pytest.raises(ValidationError) as exc:
        unparametrized.model_validate({"value": 1.5})
    assert [e["type"] for e in exc.value.errors()] == ["int_from_float", "string_type"]


@pytest.mark.unit
def test_pep696_default_drives_validation_and_serialization() -> None:
    validated = DefaultedEnvelope.model_validate({"data": {"sku": "a", "price_cents": 1}})
    assert type(validated.data) is Item
    promo = PromoItem(sku="a", price_cents=1, promo_code="P")
    assert DefaultedEnvelope(data=promo).model_dump() == {"data": {"sku": "a", "price_cents": 1}}
    assert DefaultedEnvelope[PromoItem](data=promo).model_dump()["data"]["promo_code"] == "P"


@pytest.mark.unit
def test_specializations_are_invariant_but_revalidated_through_the_origin() -> None:
    assert not issubclass(PaginatedResponse[PromoItem], _runtime(PaginatedResponse[Item]))

    class Holder(BaseModel):
        page: PaginatedResponse[Item]

    promo_page = _promo_page()
    holder = Holder.model_validate({"page": promo_page})
    assert holder.page is not promo_page
    assert type(holder.page) is PaginatedResponse[Item]
    assert type(holder.page.items[0]) is PromoItem
    assert holder.model_dump()["page"]["items"] == [{"sku": "s-1", "price_cents": 5}]
    as_any = holder.model_dump(serialize_as_any=True)["page"]["items"][0]
    assert as_any["promo_code"] == "STAFF-ONLY"

    with pytest.raises(ValidationError) as exc:
        Holder.model_validate({"page": PaginatedResponse[int](items=[1], total=1)})
    assert [(e["type"], e["loc"]) for e in exc.value.errors()] == [
        ("model_type", ("page", "items", 0)),
    ]


def _total(page: PaginatedResponse[Item]) -> int:
    return page.total


@pytest.mark.unit
def test_type_checkers_treat_the_field_typevar_as_invariant() -> None:
    # mypy (warn_unused_ignores via strict) fails this file if the ignore becomes unnecessary,
    # which is what would happen if `T` were inferred covariant.
    assert _total(_promo_page()) == 1  # type: ignore[arg-type]


@pytest.mark.unit
def test_subclass_specialization_rules() -> None:
    assert ItemPage.__pydantic_generic_metadata__["parameters"] == ()
    assert issubclass(ItemPage, _runtime(PaginatedResponse[Item]))
    with pytest.raises(TypeError, match="is not a generic class"):
        cast("Any", ItemPage)[int]

    audited = AuditedPage[int]
    assert audited.__bases__ == (AuditedPage,)
    assert issubclass(audited, PaginatedResponse)
    assert not issubclass(audited, _runtime(PaginatedResponse[int]))
    parsed = audited.model_validate({"items": ["7"], "total": 1, "request_id": "r"})
    assert parsed.items == [7]


@pytest.mark.unit
def test_partial_specialization_misses_the_late_cache() -> None:
    generic: Any = Pair
    full = Pair[str, int]
    via_partial = generic[str, _V][int]
    assert via_partial is not full
    assert via_partial.__name__ == full.__name__ == "Pair[str, int]"

    both = create_model("Both", a=(full, ...), b=(via_partial, ...))
    instance = both.model_validate({"a": {"key": "k", "value": 1}, "b": {"key": "k", "value": 2}})
    assert type(instance.__dict__["b"]) is full


@pytest.mark.unit
def test_type_alias_with_type_parameters_keeps_its_constraints() -> None:
    assert Batch[int].model_validate({"rows": ["1"]}).rows == [1]
    with pytest.raises(ValidationError) as exc:
        Batch[int](rows=[])
    assert exc.value.errors()[0]["type"] == "too_short"
    assert Batch[int].model_json_schema()["properties"]["rows"] == {"$ref": "#/$defs/NonEmpty_int_"}


@pytest.mark.unit
def test_unreferenced_specialization_is_garbage_collected() -> None:
    assert transient_specialization_is_collected() is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openapi_component_names_and_response_filtering() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        filtered = (await client.get("/items")).json()
        unfiltered = (await client.get("/promos")).json()
        leaky = (await client.get("/leaky")).json()
        safe = (await client.get("/safe")).json()
        spec = (await client.get("/openapi.json")).json()

    assert filtered["items"] == [{"sku": "sku-1", "price_cents": 999}]
    assert unfiltered["items"][0]["promo_code"] == "STAFF-ONLY"
    assert leaky["data"]["promo_code"] == "LEAK"
    assert "promo_code" not in safe["data"]

    schemas = spec["components"]["schemas"]
    assert sorted(schemas) == [
        "BoundedEnvelope",
        "BoundedEnvelope_Item_",
        "Envelope_int_",
        "Item",
        "ItemPage",
        "PaginatedResponse_Item_",
        "PaginatedResponse_PromoItem_",
        "PromoItem",
    ]
    assert schemas["PaginatedResponse_Item_"]["title"] == "PaginatedResponse[Item]"
    assert schemas["Envelope_int_"]["title"] == "IntEnvelope"
    assert schemas["BoundedEnvelope"]["properties"]["data"] == {"$ref": "#/components/schemas/Item"}
