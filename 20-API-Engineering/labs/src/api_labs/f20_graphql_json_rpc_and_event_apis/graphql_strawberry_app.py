"""Strawberry 0.340.0 schema mounted on FastAPI 0.162.0, used to observe HTTP behavior."""

import strawberry
from fastapi import FastAPI
from strawberry.fastapi import GraphQLRouter


@strawberry.type
class Query:
    @strawberry.field
    def hello(self) -> str:
        return "hi"

    @strawberry.field
    def maybe(self) -> str | None:
        raise RuntimeError("maybe failed")

    @strawberry.field
    def must(self) -> str:
        raise RuntimeError("must failed")


schema = strawberry.Schema(Query)


def build_app() -> FastAPI:
    """Mount the schema at `/graphql` on a fresh FastAPI app."""
    app = FastAPI()
    app.include_router(GraphQLRouter(schema), prefix="/graphql")
    return app
