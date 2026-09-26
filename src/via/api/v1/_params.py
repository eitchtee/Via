"""Query parameters shared by the sending endpoints."""

from typing import Annotated

from fastapi import Query

To = Annotated[
    str,
    Query(
        description="`all` (every other device of yours), or comma-separated device ids and "
        "`@username` contacts, e.g. `01J9Z3…,@bob`"
    ),
]
Title = Annotated[str | None, Query(max_length=1024)]
Message = Annotated[str | None, Query(description="Optional text sent along with a file")]
Ttl = Annotated[
    str | None,
    Query(description="Seconds or a duration like `1h`, capped at the server maximum"),
]
Filename = Annotated[str, Query(min_length=1, max_length=255)]
