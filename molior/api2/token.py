from secrets import token_hex

from ..app import app
from ..tools import OKResponse, paginate, array2db
from ..auth import req_role

from ..model.authtoken import Authtoken


@app.http_get("/api2/tokens")
@app.authenticated
async def get_tokens(request):

    query = request.cirrina.db_session.query(Authtoken)
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

    # FIXME: check existing description

    auth_token = token_hex(32)
    token = Authtoken(description=description, token=auth_token, roles=array2db(['project_create', 'mirror_create']))
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
    query = request.cirrina.db_session.query(Authtoken).filter(Authtoken.id == token_id)
    token = query.first()
    db.delete(token)
    db.commit()

    return OKResponse()
