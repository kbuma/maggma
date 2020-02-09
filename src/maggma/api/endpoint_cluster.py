import pathlib
import copy
from inspect import isclass
from typing import List, Dict, Union, Optional, Set, Any
from pydantic import BaseModel
from monty.json import MSONable
from monty.serialization import loadfn
from fastapi import FastAPI, APIRouter, Path, HTTPException, Depends, Query
from maggma.core import Store
from maggma.utils import dynamic_import
from starlette import responses
import inspect
from typing_extensions import Literal

default_responses = loadfn(pathlib.Path(__file__).parent / "default_responses.yaml")
mapping = {
    "eq": "$eq",
    "not_eq": "$not",
    "lt": "$lt",
    "gt": "$gt",
}


def build_dynamic_query(model: BaseModel, additional_signature_fields=None):
    """
    Building default query fields by inspecting the model passed in,
    and constructing a dynamic_call function on the fly.
    Args:
        additional_signature_fields: additional fields that user can specify. In the format of {"FIELD_NAME": [FIELD_TYPE, Query]}
        model: the PyDantic model that holds the data. Its fields are used to build Query

    Returns:
        A function named dynamic_call that has its signature built according to the data model passed in.

    """
    print(mapping)
    if additional_signature_fields is None:
        additional_signature_fields = dict()
    params = dict()
    # construct fields

    all_fields = list(model.__fields__.items())
    print(all_fields)

    for name, model_field in model.__fields__.items():
        if model_field.type_ == str:
            params[f"{model_field.name}_eq"] = [
                model_field.type_,
                Query(model_field.default),
            ]
            params[f"{model_field.name}_not_eq"] = [
                model_field.type_,
                Query(model_field.default),
            ]
        elif model_field.type_ == int or model_field == float:
            params[f"{model_field.name}_lt"] = [
                model_field.type_,
                Query(model_field.default),
            ]
            params[f"{model_field.name}_gt"] = [
                model_field.type_,
                Query(model_field.default),
            ]
            params[f"{model_field.name}_eq"] = [
                model_field.type_,
                Query(model_field.default),
            ]
            params[f"{model_field.name}_not_eq"] = [
                model_field.type_,
                Query(model_field.default),
            ]
        else:
            raise NotImplementedError(
                f"Field name {model_field.name} with {model_field.type_} not implemented"
            )

    def dynamic_call(**kwargs):
        return kwargs

    # user's input always have higher priority than the default's -- data model
    params.update(additional_signature_fields)
    # building the signatures for FastAPI Swagger UI
    signatures = []
    signatures.extend(
        inspect.Parameter(
            param,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=query[1],
            annotation=query[0],
        )
        for param, query in params.items()
    )

    dynamic_call.__signature__ = inspect.Signature(signatures)

    return dynamic_call


def PaginationParamsFactory(
    default_skip: int = 0, default_limit: int = 10, max_limit: int = 100
):
    """
    Factory method to generate a dependency for pagination in FastAPI

    Args:
        default_skip: default number of items to skip
        default_limit: default number of items to return
        max_limit: max number of items to return
    """

    def paginate(
        skip: int = Query(
            default_skip, description="Number of entries to skip in the search"
        ),
        limit: int = Query(
            default_limit,
            description="Max number of entries to return in a single query."
            f" Limited to {max_limit}",
        ),
    ) -> Dict[Literal["criteria", "properties", "skip", "limit"], Any]:
        """
        Pagination parameters for the API Endpoint
        """
        if limit > max_limit:
            raise Exception(
                "Requested more data per query than allowed by this endpoint."
                f"The max limit is {max_limit} entries"
            )
        return {"skip": skip, "limit": limit}

    return paginate


def FieldSetParamFactory(
    model: BaseModel,
    default_fields: Union[None, str, List[str]] = None,
    exclusion_fields: Union[None, str, List[str]] = None,
):
    """
    Factory method to generate a dependency for sparse field sets in FastAPI

    Args:
        exclusion_fields: fields that are ignored.
        model: PyDantic Model that represents the underlying data source
        default_fields: default fields to return in the API response if no fields are explicitly requested
    """

    default_fields = ",".join(default_fields) if default_fields else None
    projection_type = Optional[str] if default_fields is None else str
    all_model_fields = list(model.__fields__.keys())

    # TODO: Add ability to not consider some model fields

    def field_set(
        fields: projection_type = Query(
            default_fields,
            description=f"Fields to project from {model.__name__} as a list of comma separated strings",
        ),
        all_fields: bool = Query(False, description="Include all fields."),
    ):
        """
        Projection parameters for the API Endpoint
        """
        all_fields = all_fields

        fields = fields.split(",") if isinstance(fields, str) else None

        if fields is not None and all_fields:
            raise Exception("projection and all_includes does not match")
        elif all_fields:
            fields = all_model_fields

        return {"properties": fields}

    return field_set


class EndpointCluster(MSONable):
    """
    Implements an endpoint cluster which is a REST Compatible Resource as
    a URL endpoint
    """

    def __init__(
        self,
        store: Store,
        model: Union[BaseModel, str],
        tags: Optional[List[str]] = None,
        responses: Optional[Dict] = None,
        default_projection: Optional[Set[str]] = None,
        description: str = "No Description",
    ):
        """
        Args:
            store: The Maggma Store to get data from
            model: the pydantic model to apply to the documents from the Store
                This can be a string with a full python path to a model or
                an actuall pydantic Model if this is being instantied in python
                code. Serializing this via Monty will autoconvert the pydantic model
                into a python path string
            tags: list of tags for the Endpoint
            responses: default responses for error codes
        """
        if isinstance(model, str):
            module_path = ".".join(model.split(".")[:-1])
            class_name = model.split(".")[-1]
            self.model = dynamic_import(module_path, class_name)
        elif isclass(model) and issubclass(model, BaseModel):  # type: ignore
            self.model = model
        else:
            raise ValueError(
                "Model has to be a pydantic model or python path to a pydantic model"
            )

        self.store = store
        self.router = APIRouter()
        self.tags = tags or []
        self.responses = responses
        try:
            model_fields = set(self.model.__dict__["__fields__"].keys())

            self.default_projection = (
                model_fields if default_projection is None else default_projection
            )
            if not self.default_projection.issubset(model_fields):
                raise Exception(
                    "default projection contains some fields that are not in the model fields"
                )
        except Exception:
            raise Exception("Cannot set default_filter")
        self.description = description
        self.prepare_endpoint()

    def prepare_endpoint(self):
        """
        Internal method to prepare the endpoint by setting up default handlers
        for routes
        """

        # GET
        self.set_root_router()
        self.set_get_by_key_router()
        self.set_dynamic_model_search()
        # POST
        self.set_default_post()

        # PUT
        self.set_default_put()
        # PATCH
        self.set_default_patch()
        # DELETE
        self.set_default_delete()

    """
    GET Request Code Block
    """

    def set_dynamic_model_search(self):
        responses = copy.copy(default_responses)
        if self.responses:
            responses.update(self.responses)

        async def dynamic_model_search(
            criteria: Dict = Depends(build_dynamic_query(self.model)),
            field_set: Dict = Depends(FieldSetParamFactory(self.model)),
            pagination: Dict = Depends(PaginationParamsFactory()),
        ):
            items = self.store.query(
                criteria=criteria,
                skip=pagination.get("skip"),
                limit=pagination.get("limit"),
            )
            result = []
            for item in items:
                model_item = self.model(**item)
                result.append(model_item)
            return result

        self.router.get(
            "/{query}", response_description="WIP", tags=self.tags, responses=responses,
        )(dynamic_model_search)
        return None

    def set_get_by_key_router(self):
        key_name = self.store.key
        model_name = self.model.__name__
        responses = copy.copy(default_responses)
        if self.responses:
            responses.update(self.responses)

        async def get_by_key(
            key: str = Path(..., title=f"The {key_name} of the {model_name} to get"),
        ):
            f"""
            Get's a document by the primary key in the store

            Args:
                {key_name}: the id of a single

            Returns:
                a single document that satisfies the {model_name} model
            """
            item = self.store.query_one(criteria={self.store.key: key})

            if item is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Item with {self.store.key} = {key} not found",
                )
            else:
                model_item = self.model(**item)
                return model_item

        self.router.get(
            f"/{key_name}/{{key}}",
            response_description=f"Get an {model_name} by {key_name}",
            response_model=self.model,
            tags=self.tags,
            responses=responses,
        )(get_by_key)

    def set_root_router(self):
        async def root() -> responses:
            """
            Return:
                a list of child endpoints
            """
            # Per discussion on Stackoverflow[https://stackoverflow.com/questions/2894723/what-are-the-best-practices
            # -for -the-root-page-of-a-rest-api] and example from github[https://api.github.com/], it seems like the
            # root should return a list of child endpoints. In this case, i think we should display a set of
            # supported paths

            return responses.RedirectResponse(url="/docs")

        self.router.get(
            "/",
            response_description="Default GET endpoint root, listing possible Paths",
            tags=self.tags,
            responses=responses,
        )(root)

    """
    POST Request Code Block
    """

    def set_default_post(self):
        model = self.model

        async def post_item(item: model):
            print("in POST")
            return item

        responses = copy.copy(default_responses)
        if self.responses:
            responses.update(self.responses)
        self.router.post(
            "/",
            response_description="Default POST endpoint",
            tags=self.tags,
            responses=responses,
        )(post_item)

    """
    PUT Request Code Block
    """

    def set_default_put(self):
        model = self.model

        async def put_item(item: model):
            print("in PUT")
            return item

        responses = copy.copy(default_responses)
        if self.responses:
            responses.update(self.responses)
        self.router.put(
            "/",
            response_description="Default POST endpoint",
            tags=self.tags,
            responses=responses,
        )(put_item)

    """
    PATCH Request Code Block
    """

    def set_default_patch(self):
        model = self.model

        async def patch_item(item: model):
            print("in PATCH")
            return item

        responses = copy.copy(default_responses)
        if self.responses:
            responses.update(self.responses)
        self.router.patch(
            "/",
            response_description="Default POST endpoint",
            tags=self.tags,
            responses=responses,
        )(patch_item)

    """
    DELETE Request Code Block
    """

    def set_default_delete(self):
        model = self.model

        async def delete_item(item: model):
            print("in POST")
            return item

        responses = copy.copy(default_responses)
        if self.responses:
            responses.update(self.responses)
        self.router.delete(
            "/",
            response_description="Default POST endpoint",
            tags=self.tags,
            responses=responses,
        )(delete_item)

    def run(self):  # pragma: no cover
        """
        Runs the Endpoint cluster locally
        This is intended for testing not production
        """
        import uvicorn

        app = FastAPI()
        app.include_router(self.router, prefix="")
        uvicorn.run(app)

    def as_dict(self) -> Dict:
        """
        Special as_dict implemented to convert pydantic models into strings
        """

        d = super().as_dict()  # Ensures sub-classes serialize correctly
        d["model"] = f"{self.model.__module__}.{self.model.__name__}"

        for field in ["tags", "responses"]:
            if not d.get(field, None):
                del d[field]
        return d

    def get_description(self):
        return self.description
