"""SQL-backed list pages, with full-list compatibility for existing clients."""

from sqlalchemy.orm import Query


def paginate_list(query: Query, *, limit: int | None, offset: int | None) -> dict:
    # Supplying either parameter opts into paging. Old callers that supply
    # neither retain their complete list; new screens explicitly set a limit.
    page_limit = limit if limit is not None else (20 if offset is not None else None)
    page_offset = offset if offset is not None else 0
    if (page_limit is not None and not 1 <= page_limit <= 100) or page_offset < 0:
        raise ValueError("invalid list page")
    total = query.order_by(None).count()
    items = query.offset(page_offset).limit(page_limit).all()
    return {"items": items, "total": total, "limit": page_limit, "offset": page_offset}
