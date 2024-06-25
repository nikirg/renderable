import os
import inspect
from typing import (
    Callable,
    ParamSpec,
    Sequence,
    Type,
    TypeVar,
)
from functools import wraps

from fastapi import Depends, APIRouter, Request, Response, params
from fastapi.responses import HTMLResponse, StreamingResponse

from renderable import context, tags
from renderable.htmx import HTMX
from renderable.component import Component
from renderable.state import State, StateField
from renderable.session import Session, SessionStorage


__all__ = ["RenderableRouter"]


T = TypeVar("T")
P = ParamSpec("P")


with open(
    os.path.join(os.path.dirname(__file__), "script.js"), encoding="utf-8"
) as file:
    JS_SCRIPT_TEMPLATE = file.read()


class RenderableRouter(APIRouter):
    def __init__(
        self,
        state_schema: Type[State] | None = None,
        session_cookie_key: str = "sid",
        session_cookie_max_age: int = 60 * 60 * 24 * 7,
        htmx_cdn: str = "https://unpkg.com/htmx.org",
        htmx_sse: str = "https://unpkg.com/htmx.org/dist/ext/sse.js",
        loader_class: str = "__componentLoader__",
        loader_route_prefix: str = "/__renderable__",
        sse_endpoint_dependencies: Sequence[params.Depends] | None = None,
        **fastapi_router_kwargs,
    ):
        """Renderable Router

        Args:
            state_schema (Type[State] | None, optional): Schema of the state. Defaults to None.
                If you want to use state manager and reload_on triggers, set this argument.
            session_cookie_key (str, optional): Cookie key of the session. Defaults to "sid".
            session_cookie_max_age (int, optional): Max age of the session. Defaults to one week.
            htmx_cdn (str, optional): CDN of the htmx. Defaults to "https://unpkg.com/htmx.org".
            htmx_sse (str, optional): SSE extension of the htmx. Defaults to "https://unpkg.com/htmx.org/dist/ext/sse.js".
            loader_class (str, optional): CSS Class of the component htmx loader div. Defaults to "__componentLoader__".
            loader_route_prefix (str, optional): Route of loader request. Defaults to "/__renderable__".
            sse_endpoint_dependencies (Sequence[params.Depends] | None, optional): Dependencies of the sse endpoint. Defaults to None.

        Raises:
            TypeError: If state_schema is not a subclass of State

        Example:
            >>> router = RenderableRouter(state_schema=State)
            ... @router.page("/home")
            ... def home():
            ...     pass
        """
        self._loader_class = loader_class
        self._loader_route_prefix = loader_route_prefix
        self._htmx_cdn = htmx_cdn
        self._htmx_sse = htmx_sse
        self._session_cookie_key = session_cookie_key
        self._session_cookie_max_age = session_cookie_max_age

        dependencies = fastapi_router_kwargs.get("dependencies", [])
        dependencies.append(Depends(self._get_or_create_session))
        fastapi_router_kwargs["dependencies"] = dependencies

        self._js_script = JS_SCRIPT_TEMPLATE.replace(
            "__componentLoader__", loader_class
        )

        super().__init__(**fastapi_router_kwargs)

        self._state_schema = state_schema
        self._register_sse_endpoint(sse_endpoint_dependencies)

    @staticmethod
    async def _load_inputs(request: Request):
        inputs = dict(await request.form())
        context.set_inputs(inputs)

    async def _get_or_create_session(
        self, request: Request, response: Response
    ) -> Session:
        session_id = request.cookies.get(self._session_cookie_key)
        state = self._state_schema() if self._state_schema else None
        session = None

        if session_id:
            session = await SessionStorage.get_session(session_id)
            if not session:
                session = await SessionStorage.create_session(state)
        else:
            session = await SessionStorage.create_session(state)

        request.state.session = session
        context.set_session(session)

        if not session_id or session_id != session.id:
            response.set_cookie(
                key=self._session_cookie_key,
                value=session.id,
                httponly=True,
                max_age=self._session_cookie_max_age,
            )

        return session

    def _register_sse_endpoint(
        self, dependencies: Sequence[params.Depends] | None = None
    ):
        async def sse_endpoint(request: Request):
            async def event_stream():
                session: Session = request.state.session
                sid = session.id

                while True:
                    if await request.is_disconnected():
                        await SessionStorage.delete_session(sid)
                        break
                    component_id = await session.get_updated_component_id()
                    yield f"event: {component_id}\ndata: -\n\n"

            return StreamingResponse(event_stream(), media_type="text/event-stream")

        self.add_api_route(
            os.path.join(self._loader_route_prefix, "sse"),
            sse_endpoint,
            response_class=StreamingResponse,
            include_in_schema=False,
            dependencies=dependencies,
        )

    @staticmethod
    def _replace_self(method: Callable) -> Callable:
        async def load_component_instance(id: str) -> Type[Component] | None:
            session = context.get_session()
            component = session.get_component(id)
            return component

        sig = inspect.signature(method)

        if "self" not in sig.parameters:
            return method

        args = [param for name, param in sig.parameters.items() if name != "self"]
        self_dep_param = inspect.Parameter(
            "self",
            inspect.Parameter.KEYWORD_ONLY,
            default=Depends(load_component_instance),
        )
        args.append(self_dep_param)
        new_sig = sig.replace(parameters=args)

        def wrapper(*args, **kwargs):
            return method(*args, **kwargs)

        wrapper.__signature__ = new_sig
        return wrapper

    def page(
        self,
        path: str,
        html_lang: str = "en",
        head: Callable | None = None,
        dependencies: Sequence[Depends] | None = None,
    ):
        """Register a page

        Args:
            path (str): Fastapi path of the endpoint.
            html_lang (str, optional): Language value for "html" tag lang attribute. Defaults to "en".
            head (Callable | None, optional): A function that render html tags to head section.
                For example, it can be used to render meta, link, stryle or script tags
            dependencies (Sequence[Depends], optional): List of fastapi dependencies.

        Returns:
            Callable: A decorator that registers the page

        Raises:
            TypeError: If the path is not a string

        Example:
            >>> @app.page("/home")
            ... def home():
            ...     pass

        """

        def init_js_scripts():
            with tags.html(lang=html_lang):
                with tags.head():
                    tags.script(src=self._htmx_cdn)
                    tags.script(src=self._htmx_sse)
                    tags.script(self._js_script)

                    if head:
                        head()

                hx = HTMX(
                    ext="sse",
                    sse_connect=os.path.join(self._loader_route_prefix, "sse"),
                )
                tags.body(hx=hx)

        def decorator(func: Callable) -> None:
            class PageComponent(Component):
                async def view(self):
                    pass

            setattr(PageComponent, "view", func)

            self.component(
                path=path, dependencies=dependencies, template=init_js_scripts
            )(PageComponent)

        return decorator

    def component(
        self,
        id: str | None = None,
        path: str | None = None,
        prefix: str | None = None,
        dependencies: Sequence[Depends] | None = None,
        reload_on: list[StateField] | None = None,
        template: Callable | None = None,
        preload_content: Callable | None = None,
        class_: str | None = None,
    ):
        """Register a component

        Args:
            id (str, optional): HTML id of the component div container.
                If not specified, a python object id will be used.
                If reload_on is used, id must be specified
            path (str, optional): Fastapi path of the component view endpoint
            prefix (str, optional): Path prefix of the component view endpoint
            dependencies (Sequence[Depends], optional): Fastapi dependencies of the component view endpoint
            reload_on (list[StateField], optional): State fields whose changes will cause the component to reload.
                Component id must be specified if reload_on is used. Works only if state_schema is set on router
            template (Callable | None, optional): A function that render html tags extra to the component div
            preload_content (Callable | None, optional): A function that preloads the component content. For example skeletons
            class_ (str | None, optional): The class of the component div

        Returns:
            Callable: A decorator that registers the component

        Raises:
            ValueError: If id is not specified and reload_on is used
            TypeError: If the class is not a subclass of Component

        Example:
            >>> @app.component()
            ... class MyComponent(Component):
            ...     async def view(self):
            ...         tags.p("Hello World")
        """
        deps = [Depends(self._load_inputs)]
        if dependencies:
            deps.extend(dependencies)

        def decorator(cls: Type[T]) -> Type[T]:
            if not isinstance(cls, type(Component)):
                raise TypeError("Decorated class must be a subclass of Component")

            view_func: Callable = getattr(cls, "view")

            if not callable(view_func):
                raise TypeError("Decorated class must have a view method")

            is_async = inspect.iscoroutinefunction(view_func)
            view_func = self._replace_self(view_func)
            url = os.path.join(
                prefix or self._loader_route_prefix, path or cls.__name__
            )

            if reload_on:
                if not id:
                    raise ValueError("id must be specified if reload_on is used")
                if not self._state_schema:
                    raise ValueError(
                        "state_schema must be set on the router if reload_on is used"
                    )
                for state_field in reload_on:
                    state_field.register_component_reload(id)

            setattr(cls, "_container_id", id)
            setattr(cls, "_url", url)
            setattr(cls, "_class", class_)
            setattr(cls, "_loader_class", self._loader_class)
            setattr(cls, "_preload_content", preload_content)

            @wraps(view_func)
            async def endpoint(*args, **kwargs):
                context.clear_root_tags()

                if template:
                    template()

                try:
                    if is_async:
                        await view_func(*args, **kwargs)
                    else:
                        view_func(*args, **kwargs)

                    root_tags = context.get_root_tags()
                    html = "".join(tag.html() for tag in root_tags)
                    return html

                finally:
                    context.clear_root_tags()

            self.add_api_route(
                url,
                endpoint,
                dependencies=deps,
                response_class=HTMLResponse,
                methods=["GET", "POST"],
                include_in_schema=False,
            )

            return cls

        return decorator