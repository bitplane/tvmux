"""Better auto-generated CLI from FastAPI routes using introspection."""

import click
import json
import inspect
import logging
from typing import Union, get_origin, get_args
from pydantic import BaseModel
from fastapi.routing import APIRoute

from ..connection import Connection
from ..server.main import app

logger = logging.getLogger(__name__)


def pydantic_to_click_options(model: type[BaseModel]):
    """Convert Pydantic model fields to Click options."""
    options = []
    for field_name, field_info in model.model_fields.items():
        # Convert field name to CLI option
        option_name = f"--{field_name.replace('_', '-')}"

        # Get field type
        field_type = field_info.annotation

        # Determine Click type
        if field_type is str:
            click_type = click.STRING
        elif field_type is int:
            click_type = click.INT
        elif field_type is float:
            click_type = click.FLOAT
        elif field_type is bool:
            click_type = click.BOOL
        else:
            # Handle Optional[X] (i.e. Union[X, None])
            origin = get_origin(field_type)
            if origin is Union:
                args = [a for a in get_args(field_type) if a is not type(None)]
                if args and args[0] is int:
                    click_type = click.INT
                else:
                    click_type = click.STRING
            else:
                click_type = click.STRING

        # Get default value
        default = field_info.default if field_info.default is not None else None

        # Create option with help text
        help_text = field_info.description if hasattr(field_info, 'description') else f"{field_name} parameter"

        options.append((option_name, field_name, click_type, default, help_text))

    return options


def create_command_for_route(route: APIRoute):
    """Create a Click command for a FastAPI route."""

    method = list(route.methods)[0] if route.methods else "GET"
    endpoint = route.endpoint
    sig = inspect.signature(endpoint)

    # Extract path parameters
    path_params = []
    query_params = []
    body_model = None

    for param_name, param in sig.parameters.items():
        param_type = param.annotation

        # Skip Response objects
        if param_type.__name__ == 'Response':
            continue

        # Check if it's a path parameter
        if f'{{{param_name}}}' in route.path:
            path_params.append(param_name)
        # Check if it's a Pydantic model (body)
        elif hasattr(param_type, '__bases__') and BaseModel in param_type.__mro__:
            body_model = param_type
        # Otherwise it's a query parameter
        elif param_name not in ['request', 'response']:
            query_params.append((param_name, param))

    def command_func(**kwargs):
        """Execute API call."""
        logger.debug(f"CLI received kwargs: {kwargs}")

        conn = Connection()
        if not conn.is_running:
            click.echo("Server not running. Start with: tvmux server start", err=True)
            return

        # Build the path
        path = route.path
        for param in path_params:
            if param in kwargs:
                path = path.replace(f'{{{param}}}', str(kwargs[param]))

        # Build query parameters
        query = {}
        for param_name, _ in query_params:
            if param_name in kwargs and kwargs[param_name] is not None:
                query[param_name] = kwargs[param_name]

        # Build body from remaining kwargs
        body = {}
        if body_model:
            for field_name in body_model.model_fields:
                # Click converts --hook-name to hook_name in kwargs, so use field_name directly
                if field_name in kwargs and kwargs[field_name] is not None:
                    value = kwargs[field_name]
                    logger.debug(f"Processing field {field_name} = {repr(value)}")
                    if isinstance(value, str):
                        # Accept JSON literals for structured values, but fall
                        # back to the raw string so plain values just work
                        try:
                            value = json.loads(value)
                        except json.JSONDecodeError:
                            pass
                    body[field_name] = value

        # Log the API call
        logger.info(f"API call: {method} {path}")
        if query:
            logger.debug(f"Query params: {query}")
        if body:
            logger.debug(f"Body: {body}")

        # Make the API call
        api = conn.client()

        try:
            if method == "GET":
                response = api.get(path, params=query)
            elif method == "POST":
                response = api.post(path, json=body if body else None, params=query)
            elif method == "DELETE":
                response = api.delete(path, params=query)
            elif method == "PATCH":
                response = api.patch(path, json=body if body else None, params=query)
            elif method == "PUT":
                response = api.put(path, json=body if body else None, params=query)
            else:
                click.echo(f"Unsupported method: {method}", err=True)
                return

            # Log response
            logger.debug(f"Response: {response.status_code}")

            # Handle response
            if 200 <= response.status_code < 300:
                try:
                    data = response.json()
                    click.echo(json.dumps(data, indent=2))
                except ValueError:
                    click.echo(response.text)
            else:
                logger.error(f"API error {response.status_code}: {response.text}")
                click.echo(f"Error {response.status_code}: {response.text}", err=True)

        except Exception as e:
            logger.exception(f"Request failed: {e}")
            click.echo(f"Request failed: {e}", err=True)

    # Add decorators for parameters
    # First add path parameters as arguments
    for param in reversed(path_params):
        command_func = click.argument(param)(command_func)

    # Add query parameters as options
    for param_name, param in query_params:
        param_type = param.annotation

        # Determine type
        if param_type is int:
            click_type = click.INT
        elif param_type is bool:
            click_type = click.BOOL
        else:
            click_type = click.STRING

        default = param.default if param.default != inspect.Parameter.empty else None
        command_func = click.option(f'--{param_name.replace("_", "-")}',
                                   type=click_type,
                                   default=default,
                                   help=f"{param_name} parameter")(command_func)

    # Add body model fields as options
    if body_model:
        for option_name, field_name, click_type, default, help_text in pydantic_to_click_options(body_model):
            command_func = click.option(option_name,
                                       field_name.replace('-', '_'),
                                       type=click_type,
                                       default=default,
                                       help=help_text)(command_func)

    # Set docstring
    if endpoint.__doc__:
        command_func.__doc__ = endpoint.__doc__.strip()
    else:
        command_func.__doc__ = f"{method} {route.path}"

    return command_func


@click.group()
def api():
    """Direct API access for testing (auto-generated from routes)."""
    pass


# Generate CLI structure from routes
def generate_cli():
    """Generate CLI commands from FastAPI routes."""

    # Group routes by base resource
    resources = {}

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue

        # Skip internal routes
        if route.path in ['/', '/version', '/openapi.json', '/docs', '/redoc']:
            continue

        # Extract resource name
        parts = route.path.strip('/').split('/')
        if not parts:
            continue

        resource = parts[0]

        if resource not in resources:
            # Create resource group
            @click.group(name=resource)
            def resource_group():
                f"""Commands for {resource}."""
                pass
            resources[resource] = resource_group
            api.add_command(resource_group)

        # Determine command name
        method = list(route.methods)[0] if route.methods else "GET"

        if len(parts) == 1:
            # Base resource endpoint
            if method == "GET":
                cmd_name = "list"
            elif method == "POST":
                cmd_name = "create"
            else:
                cmd_name = method.lower()
        elif len(parts) == 2 and parts[1].startswith('{'):
            # Resource with ID
            if method == "GET":
                cmd_name = "get"
            elif method == "DELETE":
                cmd_name = "delete"
            elif method in ["PATCH", "PUT"]:
                cmd_name = "update"
            else:
                cmd_name = method.lower()
        else:
            # Sub-resource or action
            if parts[-1].startswith('{'):
                cmd_name = '_'.join(parts[-2:-1])
            else:
                cmd_name = parts[-1].replace('-', '_')

        # Create and add command
        command = create_command_for_route(route)
        resources[resource].command(name=cmd_name)(command)


# Generate CLI when module loads
generate_cli()
