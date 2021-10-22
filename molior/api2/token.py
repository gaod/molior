from secrets import token_hex

from ..app import app
from ..tools import OKResponse, paginate
from ..auth import req_role

from ..model.authtoken import AuthToken


@app.http_get("/api2/tokens")
@app.authenticated
async def get_tokens(request):

    query = request.cirrina.db_session.query(AuthToken)
    query = paginate(request, query)
    tokens = query.all()
    data = {
        "total_result_count": query.count(),
        "results": [
            {"id": token.id, "description": token.description}
            for token in tokens
        ],
    }
    return OKResponse(data)


@app.http_post("/api2/tokens")
@req_role("owner")
async def create_token(request):
    """
    Create auth token
    ---
    """
    params = await request.json()
    description = params.get("description")

    db = request.cirrina.db_session

    auth_token = token_hex(32)
    token = AuthToken(description=description, token=auth_token)
    db.add(token)
    db.commit()

    return OKResponse({"token": auth_token})


@app.http_delete("/api2/tokens")
@req_role("owner")
async def delete_project_token(request):
    """
    Delete auth token
    """
    params = await request.json()
    token_id = params.get("id")

    db = request.cirrina.db_session
    query = request.cirrina.db_session.query(AuthToken).filter(AuthToken.id == token_id)
    token = query.first()
    db.delete(token)
    db.commit()

    return OKResponse()
