from sqlalchemy.sql import or_, func

from ..app import app
from ..tools import ErrorResponse, OKResponse, array2db, is_name_valid, paginate, parse_int, db2array, escape_for_like
from ..auth import req_role
from ..molior.queues import enqueue_aptly

from ..model.project import Project
from ..model.projectversion import ProjectVersion, get_projectversion, DEPENDENCY_POLICIES
from ..model.user import User
from ..model.userrole import UserRole, USER_ROLES


@app.http_get("/api2/projectbase/{project_name}")
@app.authenticated
async def get_project_byname(request):
    """
    Returns a project with version information.

    ---
    description: Returns information about a project.
    tags:
        - Projects
    parameters:
        - name: project_name
          in: path
          required: true
          type: string
    produces:
        - text/json
    """

    project_name = request.match_info["project_name"]

    project = request.cirrina.db_session.query(Project).filter_by(name=project_name).first()
    if not project:
        return ErrorResponse(404, "Project with name {} could not be found!".format(project_name))

    data = {
        "id": project.id,
        "name": project.name,
        "description": project.description,
    }

    return OKResponse(data)


@app.http_get("/api2/projectbase/{project_name}/versions")
@app.authenticated
async def get_projectversions2(request):
    """
    Returns a list of projectversions.

    ---
    description: Returns a list of projectversions.
    tags:
        - ProjectVersions
    parameters:
        - name: project_name
          in: path
          required: true
          type: string
        - name: basemirror_id
          in: query
          required: false
          type: integer
        - name: is_basemirror
          in: query
          required: false
          type: bool
        - name: page
          in: query
          required: false
          type: integer
        - name: page_size
          in: query
          required: false
          type: integer
        - name: per_page
          in: query
          required: false
          type: integer
    produces:
        - text/json
    """
    db = request.cirrina.db_session
    project_id = request.match_info["project_name"]
    basemirror_id = request.GET.getone("basemirror_id", None)
    is_basemirror = request.GET.getone("isbasemirror", False)
    filter_name = request.GET.getone("q", None)

    query = db.query(ProjectVersion).join(Project).filter(Project.is_mirror.is_(False), ProjectVersion.is_deleted.is_(False))
    if project_id:
        query = query.filter(or_(func.lower(Project.name) == project_id.lower(), Project.id == parse_int(project_id)))
    if filter_name:
        query = query.filter(ProjectVersion.name.ilike("%{}%".format(filter_name)))
    if basemirror_id:
        query = query.filter(ProjectVersion.basemirror_id == basemirror_id)
    elif is_basemirror:
        query = query.filter(Project.is_basemirror.is_(True), ProjectVersion.mirror_state == "ready")

    query = query.order_by(ProjectVersion.id.desc())

    nb_projectversions = query.count()
    query = paginate(request, query)
    projectversions = query.all()

    results = []
    for projectversion in projectversions:
        results.append(projectversion.data())

    data = {"total_result_count": nb_projectversions, "results": results}

    return OKResponse(data)


@app.http_post("/api2/projectbase/{project_id}/versions")
@req_role("owner")
async def create_projectversion(request):
    """
    Create a project version

    ---
    description: Create a project version
    tags:
        - ProjectVersions
    parameters:
        - name: project_id
          in: path
          required: true
          type: string
        - name: body
          in: body
          required: true
          schema:
            type: object
            properties:
                name:
                    type: string
                    example: "1.0.0"
                description:
                    type: string
                    example: "This version does this and that"
                basemirror:
                    type: string
                    example: "stretch/9.6"
                architectures:
                    type: array
                    items:
                        type: string
                    example: ["amd64", "armhf"]
                    FIXME: only accept existing archs on mirror!
                dependency_policy:
                    required: false
                    type: string
                    description: Dependency policy
                    example: strict
    produces:
        - text/json
    """
    params = await request.json()

    name = params.get("name")
    description = params.get("description")
    dependency_policy = params.get("dependency_policy")
    if dependency_policy not in DEPENDENCY_POLICIES:
        return ErrorResponse(400, "Wrong dependency policy2")
    cibuilds = params.get("cibuilds")
    architectures = params.get("architectures", [])
    basemirror = params.get("basemirror")
    project_id = request.match_info["project_id"]

    if not project_id:
        return ErrorResponse(400, "No valid project id received")
    if not name:
        return ErrorResponse(400, "No valid name for the projectversion recieived")
    if not basemirror or not ("/" in basemirror):
        return ErrorResponse(400, "No valid basemirror received (format: 'name/version')")
    if not architectures:
        return ErrorResponse(400, "No valid architecture received")

    if not is_name_valid(name):
        return ErrorResponse(400, "Invalid project name")

    db = request.cirrina.db_session
    project = db.query(Project).filter(func.lower(Project.name) == project_id.lower()).first()
    if not project and isinstance(project_id, int):
        project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return ErrorResponse(400, "Project '{}' could not be found".format(project_id))
    if project.is_mirror:
        return ErrorResponse(400, "Cannot add projectversion to a mirror")

    projectversion = db.query(ProjectVersion).join(Project).filter(
            func.lower(ProjectVersion.name) == name.lower(), Project.id == project.id).first()
    if projectversion:
        return ErrorResponse(400, "Projectversion '{}' already exists{}".format(
                                        name,
                                        ", and is marked as deleted" if projectversion.is_deleted else ""))

    basemirror_name, basemirror_version = basemirror.split("/")
    basemirror = db.query(ProjectVersion).join(Project).filter(
                                    Project.id == ProjectVersion.project_id,
                                    func.lower(Project.name) == basemirror_name.lower(),
                                    func.lower(ProjectVersion.name) == basemirror_version.lower()).first()
    if not basemirror:
        return ErrorResponse(400, "Base mirror not found: {}/{}".format(basemirror_name, basemirror_version))

    for arch in architectures:
        if arch not in db2array(basemirror.mirror_architectures):
            return ErrorResponse(400, "Architecture not found in basemirror: {}".format(arch))

    projectversion = ProjectVersion(
            name=name,
            project=project,
            description=description,
            dependency_policy=dependency_policy,
            ci_builds_enabled=cibuilds,
            mirror_architectures=array2db(architectures),
            basemirror=basemirror,
            mirror_state=None)
    db.add(projectversion)
    db.commit()

    await enqueue_aptly({"init_repository": [
                basemirror_name,
                basemirror_version,
                projectversion.project.name,
                projectversion.name,
                architectures]})

    return OKResponse({"id": projectversion.id, "name": projectversion.name})


@app.http_put("/api2/project/{project_id}/{projectversion_id}")
@req_role("owner")
async def edit_projectversion(request):
    """
    Modify a project version

    ---
    description: Modify a project version
    tags:
        - ProjectVersions
    parameters:
        - name: project_id
          in: path
          required: true
          type: string
        - name: projectversion_id
          in: path
          required: true
          type: string
        - name: body
          in: body
          required: true
          schema:
            type: object
            properties:
                description:
                    type: string
                    example: "This version does this and that"
                dependency_policy:
                    required: false
                    type: string
                    description: Dependency policy
                    example: strict
    produces:
        - text/json
    """
    params = await request.json()
    description = params.get("description")
    dependency_policy = params.get("dependency_policy")
    if dependency_policy not in DEPENDENCY_POLICIES:
        return ErrorResponse(400, "Wrong dependency policy1")
    cibuilds = params.get("cibuilds")
    projectversion = get_projectversion(request)
    if not projectversion:
        return ErrorResponse(400, "Projectversion not found")

    for dep in projectversion.dependents:
        if dependency_policy == "strict" and dep.basemirror_id != projectversion.basemirror_id:
            return ErrorResponse(400, "Cannot change dependency policy because strict policy demands \
                                       to use the same basemirror as all dependents")
        elif dependency_policy == "distribution" and dep.basemirror.project_id != projectversion.basemirror.project_id:
            return ErrorResponse(400, "Cannot change dependency policy because the same distribution \
                                       is required as for all dependents")

    db = request.cirrina.db_session
    projectversion.description = description
    projectversion.dependency_policy = dependency_policy
    projectversion.ci_builds_enabled = cibuilds
    db.commit()

    return OKResponse({"id": projectversion.id, "name": projectversion.name})


@app.http_delete("/api2/projectbase/{project_id}")
@req_role("owner")
async def delete_project2(request):
    """
    Removes a project from the database.

    ---
    description: Deletes a project with the given id.
    tags:
        - Projects
    parameters:
        - name: project_id
          in: path
          required: true
          type: string
    produces:
        - text/json
    """
    db = request.cirrina.db_session
    project_name = request.match_info["project_id"]
    project = db.query(Project).filter_by(name=project_name).first()

    if not project:
        return ErrorResponse(400, "Project not found")

    if project.projectversions:
        return ErrorResponse(400, "Cannot delete project containing projectversions")

    query = request.cirrina.db_session.query(UserRole).join(User).join(Project)
    query = query.filter(Project.id == project.id)
    userroles = query.all()
    for userrole in userroles:
        db.delete(userrole)
    db.delete(project)
    db.commit()
    return OKResponse("project {} deleted".format(project_name))


@app.http_get("/api2/projectbase/{project_name}/permissions")
@app.authenticated
async def get_project_users2(request):
    """
    Get project user permissions.

    ---
    description: Get project user permissions.
    tags:
        - Projects
    parameters:
        - name: project_name
          in: path
          required: true
          type: string
        - name: candidates
          in: query
          required: false
          type: bool
        - name: q
          in: query
          required: false
          type: string
          description: Filter query
        - name: role
          in: query
          required: false
          type: string
          description: Filter role
        - name: page
          in: query
          required: false
          type: integer
        - name: page_size
          in: query
          required: false
          type: integer
        - name: per_page
          in: query
          required: false
          type: integer
    produces:
        - text/json
    """
    project_name = request.match_info["project_name"]
    candidates = request.GET.getone("candidates", None)
    if candidates:
        candidates = candidates == "true"

    project = request.cirrina.db_session.query(Project).filter_by(name=project_name).first()
    if not project:
        return ErrorResponse(404, "Project with name {} could not be found!".format(project_name))
    if project.is_mirror:
        return ErrorResponse(400, "Cannot get permissions from project which is a mirror")

    filter_name = request.GET.getone("q", None)
    filter_role = request.GET.getone("role", None)

    if candidates:
        query = request.cirrina.db_session.query(User).outerjoin(UserRole).outerjoin(Project)
        query = query.filter(User.username != "admin")
        query = query.filter(or_(UserRole.project_id.is_(None), Project.id != project.id))
        if filter_name:
            escaped_filter_name = escape_for_like(filter_name)
            query = query.filter(User.username.ilike(f"%{escaped_filter_name}%"))
        query = query.order_by(User.username)
        query = paginate(request, query)
        users = query.all()
        data = {
            "total_result_count": query.count(),
            "results": [
                {"id": user.id, "username": user.username}
                for user in users
            ],
        }
        return OKResponse(data)

    query = request.cirrina.db_session.query(UserRole).join(User).join(Project).order_by(User.username)

    if filter_name:
        query = query.filter(User.username.ilike("%{}%".format(filter_name)))

    if filter_role:
        for r in USER_ROLES:
            if filter_role.lower() in r:
                query = query.filter(UserRole.role == r)

    query = paginate(request, query)
    roles = query.all()
    nb_roles = query.count()

    data = {
        "total_result_count": nb_roles,
        "results": [
            {"id": role.user.id, "username": role.user.username, "role": role.role}
            for role in roles
        ],
    }
    return OKResponse(data)


@app.http_post("/api2/projectbase/{project_name}/permissions")
@req_role("owner")
async def add_project_users2(request):
    """
    Add permission for project

    ---
    description: Add permission for project
    tags:
        - Projects
    parameters:
        - name: project_name
          in: path
          required: true
          type: string
        - name: body
          in: body
          required: true
          schema:
            type: object
            properties:
                username:
                    type: string
                    required: true
                    description: Username
                    example: "username"
                role:
                    type: string
                    required: true
                    description: User role, e.g. member, manager, owner, ...
                    example: "member"
    produces:
        - text/json
    """
    project_name = request.match_info["project_name"]
    params = await request.json()
    username = params.get("username")
    role = params.get("role")

    if role not in ["member", "manager", "owner"]:
        return ErrorResponse(400, "Invalid role")

    if username == "admin":
        return ErrorResponse(400, "User not allowed")

    db = request.cirrina.db_session
    project = db.query(Project).filter_by(name=project_name).first()
    if not project:
        return ErrorResponse(404, "Project with name {} could not be found".format(project_name))
    if project.is_mirror:
        return ErrorResponse(400, "Cannot set permissions to project which is a mirror")

    user = db.query(User).filter(User.username == username).first()
    if not user:
        return ErrorResponse(404, "User not found")

    # check existing
    query = request.cirrina.db_session.query(UserRole).join(User).join(Project)
    query = query.filter(User.username == username)
    query = query.filter(Project.id == project.id)
    if query.all():
        return ErrorResponse(400, "User permission already added")

    userrole = UserRole(user_id=user.id, project_id=project.id, role=role)
    db.add(userrole)
    db.commit()

    return OKResponse()


@app.http_put("/api2/projectbase/{project_name}/permissions")
@req_role("owner")
async def edit_project_users2(request):
    """
    Edit project user permissions.

    ---
    description: Edit project user permissions.
    tags:
        - Projects
    parameters:
        - name: project_name
          in: path
          required: true
          type: string
        - name: body
          in: body
          required: true
          schema:
            type: object
            properties:
                username:
                    type: string
                    required: true
                    description: Username
                    example: "username"
                role:
                    type: string
                    required: true
                    description: User role, e.g. member, manager, owner, ...
                    example: "member"
    """
    project_name = request.match_info["project_name"]
    params = await request.json()
    username = params.get("username")
    role = params.get("role")

    if role not in ["member", "manager", "owner"]:
        return ErrorResponse(400, "Invalid role")

    if username == "admin":
        return ErrorResponse(400, "User not allowed")

    db = request.cirrina.db_session
    project = db.query(Project).filter_by(name=project_name).first()
    if not project:
        return ErrorResponse(404, "Project with name {} could not be found".format(project_name))
    if project.is_mirror:
        return ErrorResponse(400, "Cannot edit permissions of project which is a mirror")

    user = db.query(User).filter(User.username == username).first()
    if not user:
        return ErrorResponse(404, "User not found")

    userrole = request.cirrina.db_session.query(UserRole).filter(UserRole.project_id == project.id,
                                                                 UserRole.user_id == user.id).first()
    if not userrole:
        return ErrorResponse(400, "User Role not found")
    userrole.role = role
    db.commit()

    return OKResponse()


@app.http_delete("/api2/projectbase/{project_name}/permissions")
@req_role("owner")
async def delete_project_users2(request):
    """
    Delete permissions for project

    ---
    description: Delete permissions for project
    tags:
        - Projects
    parameters:
        - name: project_name
          in: path
          required: true
          type: string
        - name: body
          in: body
          required: true
          schema:
            type: object
            properties:
                username:
                    type: string
                    required: true
                    description: Username
                    example: "username"
    """
    project_name = request.match_info["project_name"]
    params = await request.json()
    username = params.get("username")

    if username == "admin":
        return ErrorResponse(400, "User not allowed")

    db = request.cirrina.db_session
    project = db.query(Project).filter_by(name=project_name).first()
    if not project:
        return ErrorResponse(404, "Project with name {} could not be found".format(project_name))
    if project.is_mirror:
        return ErrorResponse(400, "Cannot delete permissions from project which is a mirror")

    user = db.query(User).filter(User.username == username).first()
    if not user:
        return ErrorResponse(404, "User not found")

    # FIXME: check existing role

    query = request.cirrina.db_session.query(UserRole).join(User).join(Project)
    query = query.filter(User.username == username)
    query = query.filter(Project.id == project.id)
    userrole = query.first()
    db.delete(userrole)
    db.commit()

    return OKResponse()
