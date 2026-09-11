import functools as _functools
import types as _types


def _identity_await(self):
    """__await__ for result classes: awaiting yields the instance itself."""
    if False:  # pragma: no cover - makes this a generator (required by await)
        yield
    return self


class _AsyncDict(dict):
    """A dict that is also awaitable; ``await x`` yields a plain ``dict``."""
    __slots__ = ()

    def __await__(self):
        if False:  # pragma: no cover
            yield
        return dict(self)


class _AsyncStr(str):
    """A str that is also awaitable; ``await x`` yields the plain ``str``."""
    __slots__ = ()

    def __await__(self):
        if False:  # pragma: no cover
            yield
        return str(self)


class _AsyncList(list):
    """A list that is also awaitable; ``await x`` yields a plain ``list``."""
    __slots__ = ()

    def __await__(self):
        if False:  # pragma: no cover
            yield
        return list(self)


class _AsyncInt(int):
    """An int that is also awaitable; ``await x`` yields a plain ``int``."""
    __slots__ = ()

    def __new__(cls, v):
        return int.__new__(cls, int(v))

    def __await__(self):
        if False:  # pragma: no cover
            yield
        return int(self)


class _AsyncFloat(float):
    """A float that is also awaitable; ``await x`` yields a plain ``float``."""
    __slots__ = ()

    def __new__(cls, v):
        return float.__new__(cls, float(v))

    def __await__(self):
        if False:  # pragma: no cover
            yield
        return float(self)


class _AsyncBool(int):
    """A bool that is also awaitable; ``await x`` yields a plain ``bool``."""
    __slots__ = ()

    def __new__(cls, v):
        return int.__new__(cls, bool(v))


    def __str__(self):
        return str(bool(self))

    def __repr__(self):
        return repr(bool(self))

    def __await__(self):
        if False:  # pragma: no cover
            yield
        return bool(self)


def _wrap_result(value):
    """Return a value that works both with and without ``await``."""
    if isinstance(value, dict):
        return _AsyncDict(value)
    if isinstance(value, str):
        return _AsyncStr(value)
    if isinstance(value, list):
        return _AsyncList(value)
    if isinstance(value, bool):
        return _AsyncBool(value)
    if isinstance(value, int):
        return _AsyncInt(value)
    if isinstance(value, float):
        return _AsyncFloat(value)
    # Any object that already knows how to be awaited (our own result classes,
    # or a swallowed coroutine) is returned untouched.
    if hasattr(value, "__await__"):
        return value
    # Fallback generic proxy for anything else (incl. None).
    return _AsyncScalar(value)


class _AsyncScalar(object):
    """Awaitable + mostly-transparent wrapper for any other type (incl. None)."""

    __slots__ = ("_v",)

    def __init__(self, v):
        object.__setattr__(self, "_v", v)

    def __await__(self):
        if False:  # pragma: no cover
            yield
        return self._v

    def __getattr__(self, name):
        return getattr(self._v, name)

    def __getitem__(self, k):
        return self._v[k]

    def __setitem__(self, k, val):
        self._v[k] = val

    def __iter__(self):
        return iter(self._v)

    def __contains__(self, item):
        return item in self._v

    def __len__(self):
        return len(self._v)

    def __bool__(self):
        return bool(self._v)

    def __str__(self):
        return str(self._v)

    def __repr__(self):
        return repr(self._v)

    def __float__(self):
        return float(self._v)

    def __int__(self):
        return int(self._v)

    def __eq__(self, o):
        return self._v == o

    def __ne__(self, o):
        return self._v != o

    def __hash__(self):
        return hash(self._v)

    def __call__(self, *a, **k):
        return self._v(*a, **k)

    def __format__(self, spec):
        return format(self._v, spec)


def _make_async(fn):
    """Wrap a sync public function so calls work with or without ``await``."""
    import functools

    @functools.wraps(fn)
    def _async_aware(*args, **kwargs):
        return _wrap_result(fn(*args, **kwargs))

    return _async_aware


def _apply_async_to(module_dict, public=None):
    """Replace each public API function with an async-aware twin.

    Only names in ``public`` (the module's ``__all__``) are wrapped, so
    re-exported / imported names (``parsedate_to_datetime`` etc.) keep their
    original, non-wrapped identity. Within ``public`` these stay untouched:

      * generator functions (``yield`` in the body, e.g. ``stream_answer`` /
        ``stream_search``) keep their line-by-line streaming behaviour;
      * already ``async def`` coroutines / async generators are left alone.

    ``functools.wraps`` preserves each wrapped function's name/docstring/signature.
    """
    import inspect as _inspect
    keep = set(public) if public is not None else None
    for _name, _obj in list(module_dict.items()):
        if keep is not None and _name not in keep:
            continue
        if not isinstance(_obj, _types.FunctionType):
            continue
        if getattr(_obj, "__name__", "") == "_async_aware":
            continue
        if _inspect.isgeneratorfunction(_obj) or _inspect.isasyncgenfunction(_obj) \
           or _inspect.iscoroutinefunction(_obj):
            continue
        module_dict[_name] = _make_async(_obj)

