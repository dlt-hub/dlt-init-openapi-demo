import re
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Dict, Iterator, List, Optional, Set, Tuple, Union

import attr

from .. import schema as oai
from .. import utils
from ..config import Config
from ..schema.openapi_schema_pydantic.security_requirement import SecurityRequirement
from ..utils import PythonIdentifier, get_content_type
from .errors import ParseError, PropertyError
from .properties import (
    Class,
    CredentialsProperty,
    ModelProperty,
    Parameters,
    Property,
    Schemas,
    SecurityProperty,
    build_credentials_property,
    property_from_data,
)
from .properties.schemas import parameter_from_reference
from .responses import Response, response_from_data, DataPropertyPath

_PATH_PARAM_REGEX = re.compile("{([a-zA-Z_][a-zA-Z0-9_]*)}")


def import_string_from_class(class_: Class, prefix: str = "") -> str:
    """Create a string which is used to import a reference"""
    return f"from {prefix}.{class_.module_name} import {class_.name}"


def generate_operation_id(*, path: str, method: str) -> str:
    """Generate an operationId from a path"""
    clean_path = path.replace("{", "").replace("}", "").replace("/", "_")
    if clean_path.startswith("_"):
        clean_path = clean_path[1:]
    if clean_path.endswith("_"):
        clean_path = clean_path[:-1]
    return f"{method}_{clean_path}"


models_relative_prefix: str = "..."
security_relative_prefix: str = "..."


# pylint: disable=too-many-instance-attributes
@dataclass
class Endpoint:
    """
    Describes a single endpoint on the server
    """

    path: str
    method: str
    description: Optional[str]
    name: str
    requires_security: bool
    security: List[SecurityRequirement]
    tag: str
    python_name: PythonIdentifier
    summary: Optional[str] = ""
    security_schemes: Dict[str, SecurityProperty] = field(default_factory=dict)
    """Security schemes matching this endpoint's security requirements"""

    relative_imports: Set[str] = field(default_factory=set)
    query_parameters: Dict[str, Property] = field(default_factory=dict)
    path_parameters: "OrderedDict[str, Property]" = field(default_factory=OrderedDict)
    header_parameters: Dict[str, Property] = field(default_factory=dict)
    cookie_parameters: Dict[str, Property] = field(default_factory=dict)
    security_parameters: Dict[str, SecurityProperty] = field(default_factory=dict)
    credentials_parameter: Optional[CredentialsProperty] = None
    responses: List[Response] = field(default_factory=list)
    form_body: Optional[Property] = None
    json_body: Optional[Property] = None
    multipart_body: Optional[Property] = None
    errors: List[ParseError] = field(default_factory=list)
    used_python_identifiers: Set[PythonIdentifier] = field(default_factory=set)

    parent: Optional["Endpoint"] = None
    rank = 0  # How many other endpoints root models are referenced

    @staticmethod
    def parse_request_form_body(
        *, body: oai.RequestBody, schemas: Schemas, parent_name: str, config: Config
    ) -> Tuple[Union[Property, PropertyError, None], Schemas]:
        """Return form_body and updated schemas"""
        body_content = body.content
        form_body = body_content.get("application/x-www-form-urlencoded")
        if form_body is not None and form_body.media_type_schema is not None:
            prop, schemas = property_from_data(
                name="data",
                required=True,
                data=form_body.media_type_schema,
                schemas=schemas,
                parent_name=parent_name,
                config=config,
            )
            if isinstance(prop, ModelProperty):
                schemas = attr.evolve(schemas, classes_by_name={**schemas.classes_by_name, prop.class_info.name: prop})
            return prop, schemas
        return None, schemas

    @staticmethod
    def parse_multipart_body(
        *, body: oai.RequestBody, schemas: Schemas, parent_name: str, config: Config
    ) -> Tuple[Union[Property, PropertyError, None], Schemas]:
        """Return multipart_body"""
        body_content = body.content
        multipart_body = body_content.get("multipart/form-data")
        if multipart_body is not None and multipart_body.media_type_schema is not None:
            prop, schemas = property_from_data(
                name="multipart_data",
                required=True,
                data=multipart_body.media_type_schema,
                schemas=schemas,
                parent_name=parent_name,
                config=config,
            )
            if isinstance(prop, ModelProperty):
                prop = attr.evolve(prop, is_multipart_body=True)
                schemas = attr.evolve(schemas, classes_by_name={**schemas.classes_by_name, prop.class_info.name: prop})
            return prop, schemas
        return None, schemas

    @staticmethod
    def parse_request_json_body(
        *, body: oai.RequestBody, schemas: Schemas, parent_name: str, config: Config
    ) -> Tuple[Union[Property, PropertyError, None], Schemas]:
        """Return json_body"""
        json_body = None
        for content_type, schema in body.content.items():
            content_type = get_content_type(content_type)

            if content_type == "application/json" or content_type.endswith("+json"):
                json_body = schema
                break

        if json_body is not None and json_body.media_type_schema is not None:
            return property_from_data(
                name="json_body",
                required=True,
                data=json_body.media_type_schema,
                schemas=schemas,
                parent_name=parent_name,
                config=config,
            )
        return None, schemas

    @staticmethod
    def _add_body(
        *,
        endpoint: "Endpoint",
        data: oai.Operation,
        schemas: Schemas,
        config: Config,
    ) -> Tuple[Union[ParseError, "Endpoint"], Schemas]:
        """Adds form or JSON body to Endpoint if included in data"""
        endpoint = deepcopy(endpoint)
        if data.requestBody is None or isinstance(data.requestBody, oai.Reference):
            return endpoint, schemas

        form_body, schemas = Endpoint.parse_request_form_body(
            body=data.requestBody, schemas=schemas, parent_name=endpoint.name, config=config
        )

        if isinstance(form_body, ParseError):
            return (
                ParseError(
                    header=f"Cannot parse form body of endpoint {endpoint.name}",
                    detail=form_body.detail,
                    data=form_body.data,
                ),
                schemas,
            )

        json_body, schemas = Endpoint.parse_request_json_body(
            body=data.requestBody, schemas=schemas, parent_name=endpoint.name, config=config
        )
        if isinstance(json_body, ParseError):
            return (
                ParseError(
                    header=f"Cannot parse JSON body of endpoint {endpoint.name}",
                    detail=json_body.detail,
                    data=json_body.data,
                ),
                schemas,
            )

        multipart_body, schemas = Endpoint.parse_multipart_body(
            body=data.requestBody, schemas=schemas, parent_name=endpoint.name, config=config
        )
        if isinstance(multipart_body, ParseError):
            return (
                ParseError(
                    header=f"Cannot parse multipart body of endpoint {endpoint.name}",
                    detail=multipart_body.detail,
                    data=multipart_body.data,
                ),
                schemas,
            )

        # No reasons to use lazy imports in endpoints, so add lazy imports to relative here.
        if form_body is not None:
            endpoint.form_body = form_body
            endpoint.relative_imports.update(endpoint.form_body.get_imports(prefix=models_relative_prefix))
            endpoint.relative_imports.update(endpoint.form_body.get_lazy_imports(prefix=models_relative_prefix))
        if multipart_body is not None:
            endpoint.multipart_body = multipart_body
            endpoint.relative_imports.update(endpoint.multipart_body.get_imports(prefix=models_relative_prefix))
            endpoint.relative_imports.update(endpoint.multipart_body.get_lazy_imports(prefix=models_relative_prefix))
        if json_body is not None:
            endpoint.json_body = json_body
            endpoint.relative_imports.update(endpoint.json_body.get_imports(prefix=models_relative_prefix))
            endpoint.relative_imports.update(endpoint.json_body.get_lazy_imports(prefix=models_relative_prefix))

        return endpoint, schemas

    @staticmethod
    def _add_security(
        *, endpoint: "Endpoint", data: oai.Operation, security_schemes: Dict[str, SecurityProperty], config: Config
    ) -> "Endpoint":
        endpoint = deepcopy(endpoint)
        security = data.security or []

        # TODO: Remove dupe matching schemes from constructor, only do this here
        matching_schemes = {}
        for item in security:
            key = next(iter(item.keys()))
            scheme = security_schemes[key]

            # endpoint.relative_imports.update(scheme.get_imports(prefix=security_relative_prefix))
            # endpoint.relative_imports.update(scheme.get_lazy_imports(prefix=security_relative_prefix))
            matching_schemes[key] = scheme
        endpoint.security_parameters = matching_schemes
        if not endpoint.security_parameters:
            return endpoint
        credentials = build_credentials_property(
            name="credentials", security_properties=list(endpoint.security_parameters.values()), config=config
        )
        endpoint.relative_imports.update(credentials.get_imports(prefix=security_relative_prefix))
        endpoint.relative_imports.update(credentials.get_lazy_imports(prefix=security_relative_prefix))
        endpoint.credentials_parameter = credentials
        return endpoint

    @staticmethod
    def _add_responses(
        *, endpoint: "Endpoint", data: oai.Responses, schemas: Schemas, config: Config
    ) -> Tuple["Endpoint", Schemas]:
        endpoint = deepcopy(endpoint)
        for code, response_data in data.items():
            status_code: HTTPStatus
            try:
                status_code = HTTPStatus(int(code))
            except ValueError:
                endpoint.errors.append(
                    ParseError(
                        detail=(
                            f"Invalid response status code {code} (not a valid HTTP "
                            f"status code), response will be ommitted from generated "
                            f"client"
                        )
                    )
                )
                continue

            response, schemas = response_from_data(
                status_code=status_code, data=response_data, schemas=schemas, parent_name=endpoint.name, config=config
            )
            if isinstance(response, ParseError):
                detail_suffix = "" if response.detail is None else f" ({response.detail})"
                endpoint.errors.append(
                    ParseError(
                        detail=(
                            f"Cannot parse response for status code {status_code}{detail_suffix}, "
                            f"response will be ommitted from generated client"
                        ),
                        data=response.data,
                    )
                )
                continue

            # No reasons to use lazy imports in endpoints, so add lazy imports to relative here.
            endpoint.relative_imports |= response.prop.get_lazy_imports(prefix=models_relative_prefix)
            endpoint.relative_imports |= response.prop.get_imports(prefix=models_relative_prefix)
            endpoint.responses.append(response)
            # dlt_schema = create_dlt_schemas(response.prop)
        return endpoint, schemas

    # pylint: disable=too-many-return-statements
    @staticmethod
    def add_parameters(
        *,
        endpoint: "Endpoint",
        data: Union[oai.Operation, oai.PathItem],
        schemas: Schemas,
        parameters: Parameters,
        config: Config,
    ) -> Tuple[Union["Endpoint", ParseError], Schemas, Parameters]:
        """Process the defined `parameters` for an Endpoint.

        Any existing parameters will be ignored, so earlier instances of a parameter take precedence. PathItem
        parameters should therefore be added __after__ operation parameters.

        Args:
            endpoint: The endpoint to add parameters to.
            data: The Operation or PathItem to add parameters from.
            schemas: The cumulative Schemas of processing so far which should contain details for any references.
            parameters: The cumulative Parameters of processing so far which should contain details for any references.
            config: User-provided config for overrides within parameters.

        Returns:
            `(result, schemas, parameters)` where `result` is either an updated Endpoint containing the parameters or a
            ParseError describing what went wrong. `schemas` is an updated version of the `schemas` input, adding any
            new enums or classes. `parameters` is an updated version of the `parameters` input, adding new parameters.

        See Also:
            - https://swagger.io/docs/specification/describing-parameters/
            - https://swagger.io/docs/specification/paths-and-operations/
        """
        # pylint: disable=too-many-branches, too-many-locals
        # There isn't much value in breaking down this function further other than to satisfy the linter.

        if data.parameters is None:
            return endpoint, schemas, parameters

        endpoint = deepcopy(endpoint)

        unique_parameters: Set[Tuple[str, oai.ParameterLocation]] = set()
        parameters_by_location = {
            oai.ParameterLocation.QUERY: endpoint.query_parameters,
            oai.ParameterLocation.PATH: endpoint.path_parameters,
            oai.ParameterLocation.HEADER: endpoint.header_parameters,
            oai.ParameterLocation.COOKIE: endpoint.cookie_parameters,
        }

        for param in data.parameters:
            # Obtain the parameter from the reference or just the parameter itself
            param_or_error = parameter_from_reference(param=param, parameters=parameters)
            if isinstance(param_or_error, ParseError):
                return param_or_error, schemas, parameters
            param = param_or_error

            if param.param_schema is None:
                continue

            unique_param = (param.name, param.param_in)
            if unique_param in unique_parameters:
                return (
                    ParseError(
                        data=data,
                        detail=(
                            "Parameters MUST NOT contain duplicates. "
                            "A unique parameter is defined by a combination of a name and location. "
                            f"Duplicated parameters named `{param.name}` detected in `{param.param_in}`."
                        ),
                    ),
                    schemas,
                    parameters,
                )

            unique_parameters.add(unique_param)

            prop, new_schemas = property_from_data(
                name=param.name,
                required=param.required,
                data=param.param_schema,
                schemas=schemas,
                parent_name=endpoint.name,
                config=config,
            )

            if isinstance(prop, ParseError):
                return (
                    ParseError(detail=f"cannot parse parameter of endpoint {endpoint.name}", data=prop.data),
                    schemas,
                    parameters,
                )

            schemas = new_schemas

            location_error = prop.validate_location(param.param_in)
            if location_error is not None:
                location_error.data = param
                return location_error, schemas, parameters

            if prop.name in parameters_by_location[param.param_in]:
                # This parameter was defined in the Operation, so ignore the PathItem definition
                continue

            for location, parameters_dict in parameters_by_location.items():
                if location == param.param_in or prop.name not in parameters_dict:
                    continue
                existing_prop: Property = parameters_dict[prop.name]
                # Existing should be converted too for consistency
                endpoint.used_python_identifiers.remove(existing_prop.python_name)
                existing_prop.set_python_name(new_name=f"{existing_prop.name}_{location}", config=config)

                if existing_prop.python_name in endpoint.used_python_identifiers:
                    return (
                        ParseError(
                            detail=f"Parameters with same Python identifier `{existing_prop.python_name}` detected",
                            data=data,
                        ),
                        schemas,
                        parameters,
                    )
                endpoint.used_python_identifiers.add(existing_prop.python_name)
                prop.set_python_name(new_name=f"{param.name}_{param.param_in}", config=config)

            if prop.python_name in endpoint.used_python_identifiers:
                return (
                    ParseError(
                        detail=f"Parameters with same Python identifier `{prop.python_name}` detected", data=data
                    ),
                    schemas,
                    parameters,
                )
            if param.param_in == oai.ParameterLocation.QUERY and (prop.nullable or not prop.required):
                # There is no NULL for query params, so nullable and not required are the same.
                prop = attr.evolve(prop, required=False, nullable=True)

            # No reasons to use lazy imports in endpoints, so add lazy imports to relative here.
            endpoint.relative_imports.update(prop.get_lazy_imports(prefix=models_relative_prefix))
            endpoint.relative_imports.update(prop.get_imports(prefix=models_relative_prefix))
            endpoint.used_python_identifiers.add(prop.python_name)
            parameters_by_location[param.param_in][prop.name] = prop

        return endpoint, schemas, parameters

    @staticmethod
    def sort_parameters(*, endpoint: "Endpoint") -> Union["Endpoint", ParseError]:
        """
        Sorts the path parameters of an `endpoint` so that they match the order declared in `endpoint.path`.
        Sorts the query parameters so required params come first.

        Args:
            endpoint: The endpoint to sort the parameters of.

        Returns:
            Either an updated `endpoint` with sorted path parameters or a `ParseError` if something was wrong with
                the path parameters and they could not be sorted.
        """
        endpoint = deepcopy(endpoint)
        parameters_from_path = re.findall(_PATH_PARAM_REGEX, endpoint.path)
        try:
            sorted_params = sorted(
                endpoint.path_parameters.values(), key=lambda param: parameters_from_path.index(param.name)
            )
            endpoint.path_parameters = OrderedDict((param.name, param) for param in sorted_params)
        except ValueError:
            pass  # We're going to catch the difference down below

        if parameters_from_path != list(endpoint.path_parameters):
            return ParseError(
                detail=f"Incorrect path templating for {endpoint.path} (Path parameters do not match with path)",
            )
        endpoint.query_parameters = dict(
            sorted(endpoint.query_parameters.items(), key=lambda item: item[1].required, reverse=True)
        )
        return endpoint

    @staticmethod
    def from_data(
        *,
        data: oai.Operation,
        path: str,
        method: str,
        tag: str,
        schemas: Schemas,
        parameters: Parameters,
        security_schemes: Dict[str, SecurityProperty],
        config: Config,
    ) -> Tuple[Union["Endpoint", ParseError], Schemas, Parameters]:
        """Construct an endpoint from the OpenAPI data"""

        if data.operationId is None:
            name = generate_operation_id(path=path, method=method)
        else:
            name = data.operationId

        security = data.security or []

        matching_security_schemes = {}
        for item in security:
            key = next(iter(item.keys()))
            matching_security_schemes[key] = security_schemes[key]

        endpoint = Endpoint(
            path=path,
            method=method,
            summary=utils.remove_string_escapes(data.summary) if data.summary else "",
            description=utils.remove_string_escapes(data.description) if data.description else "",
            name=name,
            requires_security=bool(data.security),
            security=security,
            security_schemes=security_schemes,
            tag=tag,
            python_name=PythonIdentifier(name, prefix=config.field_prefix),
        )

        result, schemas, parameters = Endpoint.add_parameters(
            endpoint=endpoint, data=data, schemas=schemas, parameters=parameters, config=config
        )
        if isinstance(result, ParseError):
            return result, schemas, parameters
        result, schemas = Endpoint._add_responses(endpoint=result, data=data.responses, schemas=schemas, config=config)
        result, schemas = Endpoint._add_body(endpoint=result, data=data, schemas=schemas, config=config)
        if isinstance(result, ParseError):
            return result, schemas, parameters
        result = Endpoint._add_security(endpoint=result, data=data, security_schemes=security_schemes, config=config)

        return result, schemas, parameters

    def response_type(self) -> str:
        """Get the Python type of any response from this endpoint"""
        types = sorted({response.prop.get_type_string(quoted=False) for response in self.responses})
        if len(types) == 0:
            return "Any"
        if len(types) == 1:
            return self.responses[0].prop.get_type_string(quoted=False)
        return f"Union[{', '.join(types)}]"

    def iter_all_parameters(self) -> Iterator[Property]:
        """Iterate through all the parameters of this endpoint"""
        yield from self.path_parameters.values()
        yield from self.query_parameters.values()
        yield from self.header_parameters.values()
        yield from self.cookie_parameters.values()
        if self.multipart_body:
            yield self.multipart_body
        if self.json_body:
            yield self.json_body

    def list_all_parameters(self) -> List[Property]:
        """Return a List of all the parameters of this endpoint"""
        return list(self.iter_all_parameters())

    @property
    def has_path_parameters(self) -> bool:
        return bool(self.path_parameters)

    @property
    def list_property(self) -> Optional[DataPropertyPath]:
        resp = self.responses[0]  # TODO: Assuming first response is data
        return resp.list_property

    @property
    def table_name(self) -> str:
        list_prop = self.list_property
        if list_prop:
            return list_prop.prop.class_info.name
        resp = self.responses[0]
        prop = resp.prop
        if isinstance(prop, ModelProperty):
            return prop.class_info.name
        return self.name

    @property
    def data_json_path(self) -> Optional[str]:
        list_prop = self.list_property
        if not list_prop:
            return ""
        return ".".join(list_prop.path) or ""

    @property
    def transformer(self) -> "TransformerSetting":
        if not self.parent:
            return None
        if not self.parent.list_property:
            return None
        if not self.path_parameters:
            return None
        if len(self.path_parameters) > 1:
            # TODO: Can't handle endpoints with more than 1 path param for now
            return None
        last_path = list(self.path_parameters.values())[-1]
        list_prop = self.parent.list_property.prop
        transformer_arg = None
        id_prop = None
        # TODO: find parent_property should recurse up to 2 levels object and be a json path
        for prop in (list_prop.required_properties or []) + (list_prop.optional_properties or []):
            if prop.name == last_path.name:
                transformer_arg = prop
                break
            if prop.name == "id":
                id_prop = prop
        if id_prop and not transformer_arg:
            transformer_arg = id_prop
        if not transformer_arg:
            return None
        return TransformerSetting(self.parent, transformer_arg, last_path)

    @property
    def is_root_endpoint(self) -> bool:
        return bool(self.list_property) and not self.has_path_parameters

    @property
    def root_model(self) -> Optional[ModelProperty]:
        if self.list_property:
            if isinstance(self.list_property.prop, ModelProperty):
                return self.list_property.prop
        if not self.has_json_response:
            return None
        resp = self.responses[0]
        if isinstance(resp.prop, ModelProperty):
            return resp.prop
        return None

    @property
    def has_json_response(self) -> bool:
        # non-json responses are ignored by the parser, so just check for response with 2xx status
        for response in self.responses:
            if 200 <= response.status_code <= 299:
                return True
        return False

    def __hash__(self) -> int:
        return hash(self.name)


@dataclass
class TransformerSetting:
    parent_endpoint: Endpoint
    parent_property: Property
    path_parameter: Property
