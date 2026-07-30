import logging
from collections.abc import Callable
from typing import ClassVar

logger = logging.getLogger(__name__)


def list_response(items: list, limit: int, offset: int) -> dict:
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


class ListResponseMixin:
    # Declared, never defined: the concrete service supplies `list`, and the
    # mixin only wraps its result in the paginated envelope. The declaration
    # states that contract so the call below resolves.
    list: ClassVar[Callable[..., list]]

    @classmethod
    def list_response(cls, db, *args, **kwargs):
        if "limit" in kwargs and "offset" in kwargs:
            limit = kwargs["limit"]
            offset = kwargs["offset"]
            items = cls.list(db, *args, **kwargs)
        else:
            if len(args) < 2:
                raise ValueError("limit and offset are required for list responses")
            *list_args, limit, offset = args
            items = cls.list(db, *list_args, limit=limit, offset=offset, **kwargs)
        return list_response(items, limit, offset)
