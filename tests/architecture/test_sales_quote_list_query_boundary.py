"""Guard the single authoritative Quote-list query boundary."""

from __future__ import annotations

import inspect

from app.services import web_sales
from app.services.sales import service as sales_service
from app.web.admin import sales as sales_routes


def test_quote_list_rows_and_count_delegate_to_one_typed_owner_query() -> None:
    source = inspect.getsource(web_sales.build_quotes_list_context)

    assert "sales_service.QuoteListQueryInput(" in source
    assert "sales_service.quotes.query(" in source
    assert "_count_quotes" not in source
    assert "sales_service.quotes.list(" not in source


def test_quote_search_uses_correlated_exists_without_full_row_distinct() -> None:
    predicate_source = inspect.getsource(sales_service._quote_search_predicate)
    list_source = inspect.getsource(sales_service.Quotes.list)
    query_source = inspect.getsource(sales_service.Quotes.query)

    assert predicate_source.count(".exists()") >= 4
    assert ".distinct()" not in predicate_source
    assert ".distinct()" not in list_source
    assert ".distinct()" not in query_source
    assert "func.count(Quote.id)" in query_source


def test_quote_list_route_maps_database_failure_without_owning_query_rules() -> None:
    source = inspect.getsource(sales_routes.quotes_list)

    assert "build_quotes_list_context(" in source
    assert "except SQLAlchemyError as exc:" in source
    assert "sales_quotes_list_load_failed" in source
    assert "logger.exception" not in source
    assert "build_quotes_failure_context(" in source
    assert "db.query(" not in source
