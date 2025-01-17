from sqlalchemy.orm import aliased
from sqlalchemy import func, or_
from aiohttp import web
from shutil import rmtree

from ..app import app, logger
from ..auth import req_role
from ..tools import ErrorResponse, OKResponse, is_name_valid, db2array
from ..api.projectversion import do_lock, do_overlay
from ..molior.queues import enqueue_aptly
from ..molior.configuration import Configuration

from ..model.projectversion import (
    ProjectVersion, get_projectversion, get_projectversion_deps,
    get_projectversion_byname, get_projectversion_byid)
from ..model.project import Project
from ..model.sourcerepository import SourceRepository
from ..model.sourepprover import SouRepProVer
from ..model.build import Build
from ..model.buildtask import BuildTask
from ..model.postbuildhook import PostBuildHook
from ..model.projectversiondependency import ProjectVersionDependency


@app.http_get("/api2/project/{project_name}/{project_version}")
@app.authenticated
async def get_projectversion2(request):
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
        - name: project_version
          in: path
          required: true
          type: string
    produces:
        - text/json
    """
    projectversion = get_projectversion(request)
    if not projectversion:
        return ErrorResponse(400, "Projectversion not found")

    if projectversion.project.is_mirror:
        return ErrorResponse(400, "Projectversion is mirror")

    return OKResponse(projectversion.data())


@app.http_get("/api2/project/{project_id}/{projectversion_id}/dependencies")
@app.authenticated
async def get_projectversion_dependencies(request):
    """
    Returns a list of project version dependencies.

    ---
    description: Returns a list of project version dependencies.
    tags:
        - ProjectVersions
    parameters:
        - name: project_id
          in: path
          required: true
          type: integer
        - name: projectversion_id
          in: path
          required: true
          type: string
        - name: basemirror_id
          in: query
          required: false
          type: integer
        - name: candidates
          in: query
          required: false
          type: bool
        - name: q
          in: query
          required: false
          type: string
          description: Filter query
    produces:
        - text/json
    """
    db = request.cirrina.db_session
    candidates = request.GET.getone("candidates", None)
    filter_name = request.GET.getone("q", None)

    if candidates:
        candidates = candidates == "true"

    projectversion = get_projectversion(request)
    if not projectversion:
        return ErrorResponse(400, "Projectversion not found")

    # get existing dependencies
    deps = projectversion.dependencies
    dep_ids = [d.id for d in deps]

    if candidates:  # return candidate dependencies
        results = []
        cands_query = db.query(ProjectVersion).filter(ProjectVersion.basemirror_id == projectversion.basemirror_id,
                                                      ProjectVersion.id != projectversion.id,
                                                      ProjectVersion.id.notin_(dep_ids))
        BaseMirror = aliased(ProjectVersion)
        dist_query = db.query(ProjectVersion).join(BaseMirror, BaseMirror.id == ProjectVersion.basemirror_id).filter(
                                                   ProjectVersion.dependency_policy == "distribution",
                                                   BaseMirror.project_id == projectversion.basemirror.project_id,
                                                   BaseMirror.id != projectversion.basemirror_id,
                                                   ProjectVersion.id.notin_(dep_ids))

        any_query = db.query(ProjectVersion).filter(
                                                   ProjectVersion.dependency_policy == "any",
                                                   ProjectVersion.id != projectversion.id,
                                                   ProjectVersion.id.notin_(dep_ids))
        cands = cands_query.union(dist_query, any_query).join(Project).order_by(Project.is_mirror,
                                                                                func.lower(Project.name),
                                                                                func.lower(ProjectVersion.name))
        if filter_name:
            cands = cands.filter(ProjectVersion.fullname.ilike("%{}%".format(filter_name)))

        for cand in cands.all():
            results.append(cand.data())

        data = {"total_result_count": len(results), "results": results}
        return OKResponse(data)

    # return existing dependencies
    results = []
    # send unique deps
    deps_unique = []
    for d in deps:
        if d not in deps_unique:
            deps_unique.append(d)
    for d in deps_unique:
        dep = db.query(ProjectVersion).filter(ProjectVersion.id == d.id)
        if filter_name:
            dep = dep.filter(ProjectVersion.fullname.ilike("%{}%".format(filter_name)))
        dep = dep.first()
        if dep:
            data = dep.data()
            results.append(data)

    # FIXME paginate ??
    data = {"total_result_count": len(results), "results": results}
    return OKResponse(data)


@app.http_post("/api2/project/{project_id}/{projectversion_id}/dependencies")
@req_role("owner")
async def add_projectversion_dependency(request):
    """
    Add project version dependencies.

    ---
    description: Add project version dependencies.
    tags:
        - ProjectVersions
    parameters:
        - name: project_id
          in: path
          required: true
          type: integer
        - name: projectversion_id
          in: path
          required: true
          type: string
        - name: body
          in: body
          description: Dependency data
          required: true
          schema:
              type: object
              properties:
                  dependency:
                      required: false
                      type: string
                      description: Name of the dependency
                  use_cibuilds:
                      required: false
                      type: bool
                      description: Use CI builds?
    produces:
        - text/json
    """
    params = await request.json()
    dependency_name = params.get("dependency")
    use_cibuilds = params.get("use_cibuilds")

    projectversion = get_projectversion(request)
    if not projectversion:
        return ErrorResponse(400, "Projectversion not found")

    if projectversion.project.is_mirror:
        return ErrorResponse(400, "Cannot add dependencies to project which is a mirror")

    if projectversion.is_locked:
        return ErrorResponse(400, "Cannot add dependencies on a locked projectversion")

    db = request.cirrina.db_session
    dependency = get_projectversion_byname(dependency_name, db)
    if not dependency:
        return ErrorResponse(400, "Dependency not found")

    if dependency.id == projectversion.id:
        return ErrorResponse(400, "Cannot add a dependency of the same projectversion to itself")

    if dependency.project.is_basemirror:
        return ErrorResponse(400, "Cannot add a dependency which is a basemirror")

    if dependency.dependency_policy == "strict":
        if dependency.basemirror_id != projectversion.basemirror_id:
            return ErrorResponse(400, "Cannot add a dependency with different basemirror as per dependency policy")
    elif dependency.dependency_policy == "distribution":
        if dependency.basemirror.project.id != projectversion.basemirror.project.id:
            return ErrorResponse(400, "Cannot add a dependency from different distribution as per dependency policy")

    # check for dependency loops
    deps = get_projectversion_deps(dependency.id, db)
    dep_ids = [d[0] for d in deps]
    if projectversion.id in dep_ids:
        return ErrorResponse(400, "Cannot add a dependency of a projectversion depending itself on this projectversion")
    if dependency.id in dep_ids:
        return ErrorResponse(400, "Dependency already exists")
    for dep_id in dep_ids:
        dep = get_projectversion_byid(dep_id, db)
        if dep.dependency_policy == "strict":
            if dep.basemirror_id != dependency.basemirror_id:
                return ErrorResponse(400, "Cannot add a dependency with different basemirror as per dependency policy")
        elif dep.dependency_policy == "distribution":
            if dep.basemirror.project.id != dependency.basemirror.project.id:
                return ErrorResponse(400, "Cannot add a dependency from different distribution as per dependency policy")

    # do not allow using a mirror for ci builds
    if dependency.project.is_mirror:
        use_cibuilds = False

    pdep = ProjectVersionDependency(
            projectversion_id=projectversion.id,
            dependency_id=dependency.id,
            use_cibuilds=use_cibuilds)
    db.add(pdep)
    db.commit()
    return OKResponse("Dependency added")


@app.http_delete("/api2/project/{project_id}/{projectversion_id}/dependency/{dependency_name}/{dependency_version}")
@req_role("owner")
async def delete_projectversion_dependency(request):
    """
    Delete a project version dependency.

    ---
    description: Delete a project version dependency.
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
        - name: dependecy_name
          in: path
          required: true
          type: string
        - name: dependecy_version
          in: path
          required: true
          type: string
    produces:
        - text/json
    """
    db = request.cirrina.db_session
    dependency_name = request.match_info["dependency_name"]
    dependency_version = request.match_info["dependency_version"]

    projectversion = get_projectversion(request)
    if not projectversion:
        return ErrorResponse(400, "Projectversion not found")

    if projectversion.project.is_mirror:
        return ErrorResponse(400, "Cannot delete dependencies from project which is a mirror")

    if projectversion.is_locked:
        return ErrorResponse(400, "Projectversion is locked")

    dependency = get_projectversion_byname(dependency_name + "/" + dependency_version, db)
    if not dependency:
        return ErrorResponse(400, "Dependency not found")

    if dependency not in projectversion.dependencies:
        return ErrorResponse(400, "Dependency not found")

    projectversion.dependencies.remove(dependency)
    db.commit()
    return OKResponse("Dependency deleted")


@app.http_post("/api2/project/{project_id}/{projectversion_id}/copy")
@req_role("owner")
async def clone_projectversion(request):
    """
    Clone a project version.

    ---
    description: Clone a project version.
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
    produces:
        - text/json
    """
    db = request.cirrina.db_session
    params = await request.json()
    new_version = params.get("name")
    description = params.get("description")
    dependency_policy = params.get("dependency_policy")
    basemirror = params.get("basemirror")
    architectures = params.get("architectures", [])
    cibuilds = params.get("cibuilds", False)

    if not new_version:
        return ErrorResponse(400, "No valid name for the projectversion received")
    if not is_name_valid(new_version):
        return ErrorResponse(400, "Invalid project name!")

    projectversion = get_projectversion(request)
    if not projectversion:
        return ErrorResponse(404, "Projectversion not found")

    if db.query(ProjectVersion).join(Project).filter(
                func.lower(ProjectVersion.name) == new_version.lower(),
                Project.id == projectversion.project_id).first():
        return ErrorResponse(400, "Projectversion already exists.")

    basemirror_name, basemirror_version = basemirror.split("/")
    basemirror = db.query(ProjectVersion).join(Project).filter(
            func.lower(Project.name) == basemirror_name.lower(),
            func.lower(ProjectVersion.name) == basemirror_version.lower()).first()
    if not basemirror:
        return ErrorResponse(400, "Base mirror not found: {}/{}".format(basemirror_name, basemirror_version))

    projectversion.copy(db, new_version, description, dependency_policy, basemirror.id, architectures, cibuilds)
    await enqueue_aptly(
        {
            "init_repository": [
                basemirror.project.name,
                basemirror.name,
                projectversion.project.name,
                new_version,
                architectures,
            ]
        }
    )
    return OKResponse()


@app.http_post("/api2/project/{project_id}/{projectversion_id}/lock")
@req_role("owner")
async def lock_projectversion(request):
    """
    Lock a project version.

    ---
    description: Clone a project version.
    tags:
        - Projects
    parameters:
        - name: project_id
          in: path
          required: true
          type: string
        - name: projectversion_id
          in: path
          required: true
          type: string
    produces:
        - text/json
    """
    projectversion = get_projectversion(request)
    if not projectversion:
        return ErrorResponse(400, "Projectversion not found")
    if projectversion.basemirror.external_repo:
        return ErrorResponse(400, "Projectversion is based on external mirror")

    return do_lock(request, projectversion.id)


@app.http_post("/api2/project/{project_id}/{projectversion_id}/overlay")
@req_role("owner")
async def overlay_projectversion(request):
    """
    Overlay a project version.

    ---
    description: Overlay a project version.
    tags:
        - Projects
    parameters:
        - name: project_id
          in: path
          required: true
          type: string
        - name: projectversion_id
          in: path
          required: true
          type: string
    produces:
        - text/json
    """
    params = await request.json()

    name = params.get("name")
    projectversion = get_projectversion(request)
    if not projectversion:
        return ErrorResponse(400, "Projectversion not found")

    return await do_overlay(request, projectversion.id, name)


@app.http_post("/api2/project/{project_id}/{projectversion_id}/snapshot")
@req_role("owner")
async def snapshot_projectversion(request):
    params = await request.json()

    name = params.get("name")
    projectversion = get_projectversion(request)
    if not projectversion:
        return ErrorResponse(400, "Projectversion not found")
    if projectversion.basemirror.external_repo:
        return ErrorResponse(400, "Projectversion is based on external mirror")
    if projectversion.projectversiontype in ['overlay', 'snapshot']:
        return ErrorResponse(400, "Projectversion is of type overlay/snapshot")

    if not name:
        return ErrorResponse(400, "No valid name for the projectversion recieived")
    if not is_name_valid(name):
        return ErrorResponse(400, "Invalid project name")

    db = request.cirrina.db_session
    if db.query(ProjectVersion).join(Project).filter(
            func.lower(ProjectVersion.name) == name.lower(),
            Project.id == projectversion.project_id).first():
        return ErrorResponse(400, "Projectversion '%s' already exists" % name)

    # check dependencies are locked
    for dep in projectversion.dependencies:
        if not dep.is_locked:
            return ErrorResponse(400, "Dependency '%s/%s' is not locked" % (dep.project.name, dep.name))

    # find latest builds
    latest_builds = db.query(func.max(Build.id).label("latest_id")).filter(
            Build.projectversion_id == projectversion.id,
            Build.buildtype == "deb").group_by(Build.sourcerepository_id).subquery()

    builds = db.query(Build).join(latest_builds, Build.id == latest_builds.c.latest_id).order_by(
            Build.sourcename, Build.id.desc()).all()

    build_source_names = []
    for build in builds:
        logger.info("snapshot: found latest build: %s/%s (%s)" % (build.sourcename, build.version, build.buildstate))
        if build.buildstate != "successful":
            return ErrorResponse(400, "Not all latest builds are successful")
        if build.sourcename in build_source_names:
            logger.warning("shapshot: ignoring duplicate build sourcename: %s/%s" % (build.sourcename, build.version))
            continue
        build_source_names.append(build.sourcename)
        if not build.debianpackages:
            return ErrorResponse(400, "No debian packages found for %s/%s" % (build.sourcename, build.version))

    new_projectversion = ProjectVersion(
        name=name,
        project=projectversion.project,
        dependencies=projectversion.dependencies,   # FIXME: use_cubilds not included via relationship
        mirror_architectures=projectversion.mirror_architectures,
        basemirror_id=projectversion.basemirror_id,
        sourcerepositories=projectversion.sourcerepositories,
        ci_builds_enabled=False,
        is_locked=True,
        projectversiontype="snapshot",
        baseprojectversion_id=projectversion.id
    )

    for repo in new_projectversion.sourcerepositories:
        sourepprover = db.query(SouRepProVer).filter(
                SouRepProVer.sourcerepository_id == repo.id,
                SouRepProVer.projectversion_id == projectversion.id).first()
        new_sourepprover = db.query(SouRepProVer).filter(
                SouRepProVer.sourcerepository_id == repo.id,
                SouRepProVer.projectversion_id == new_projectversion.id).first()
        new_sourepprover.architectures = sourepprover.architectures

    db.add(new_projectversion)
    db.commit()

    await enqueue_aptly(
        {
            "snapshot_repository": [
                projectversion.basemirror.project.name,
                projectversion.basemirror.name,
                projectversion.project.name,
                projectversion.name,
                db2array(projectversion.mirror_architectures),
                new_projectversion.name,
                projectversion.id,
                new_projectversion.id
            ]
        }
    )

    return OKResponse({"id": new_projectversion.id, "name": new_projectversion.name})


@app.http_delete("/api2/project/{project_id}/{projectversion_id}")
@req_role("owner")
async def delete_projectversion(request):
    force = request.GET.getone("forceremoval", None)
    force = force == "true"
    projectversion = get_projectversion(request)
    if not projectversion:
        return ErrorResponse(400, "Projectversion not found")
    if projectversion.is_locked:
        return ErrorResponse(400, "Projectversion is locked")

    if projectversion.dependents:
        blocking_dependents = []
        for d in projectversion.dependents:
            if not d.is_deleted:
                blocking_dependents.append("{}/{}".format(d.project.name, d.name))
                logger.error("projectversion delete: projectversion_id %d still has dependency %d", projectversion.id, d.id)
        if blocking_dependents:
            return ErrorResponse(400, "Projectversions '{}' are still depending on this version, cannot delete it".format(
                                  ", ".join(blocking_dependents)))

    db = request.cirrina.db_session
    if not force:
        # do not delete PV if builds are running
        debbuilds = db.query(Build).filter(Build.projectversion_id == projectversion.id, Build.buildtype == "deb", or_(
                                           Build.buildstate == "needs_build",
                                           Build.buildstate == "scheduled",
                                           Build.buildstate == "building",
                                           Build.buildstate == "needs_publish",
                                           Build.buildstate == "publishing"
                                           )).first()
        if debbuilds:
            return ErrorResponse(400, "Builds are still depending on this version, cannot delete it")

    # remember configuration
    basemirror_name = projectversion.basemirror.project.name
    basemirror_version = projectversion.basemirror.name
    project_name = projectversion.project.name
    project_version = projectversion.name
    architectures = db2array(projectversion.mirror_architectures)

    # mark as deleted
    projectversion.is_deleted = True
    projectversion.is_locked = True
    projectversion.ci_builds_enabled = False
    projectversion.name = projectversion.name + "-deleted"
    db.commit()

    # delete deb builds and parents if needed
    todelete = []
    debbuilds = db.query(Build).filter(Build.projectversion_id == projectversion.id, Build.buildtype == "deb").all()
    for debbuild in debbuilds:
        todelete.append(debbuild)

    for debbuild in debbuilds:
        sourcebuild = None
        if debbuild.parent:
            sourcebuild = debbuild.parent
            for child in debbuild.parent.children:
                if child.projectversion_id == projectversion.id:
                    continue
                sourcebuild = None  # source build has childs belonging to a different projectversion
        if sourcebuild and sourcebuild not in todelete:
            todelete.append(sourcebuild)

    def deletebuild(build):
        buildtasks = db.query(BuildTask).filter(BuildTask.build == build).all()
        for buildtask in buildtasks:
            db.delete(buildtask)
        db.delete(build)
        buildout = "/var/lib/molior/buildout/%d" % build.id
        try:
            rmtree(buildout)
        except Exception:
            pass

    for build in todelete:
        if build.buildtype == "source":
            topbuild = build.parent
            deletebuild(build)
            deletebuild(topbuild)
        else:
            deletebuild(build)

    # delete hooks
    todelete = []
    sourcerepositoryprojectversions = db.query(SouRepProVer).filter(SouRepProVer.projectversion_id == projectversion.id).all()
    for sourcerepositoryprojectversion in sourcerepositoryprojectversions:
        hooks = db.query(PostBuildHook).filter(PostBuildHook.sourcerepositoryprojectversion_id ==
                                               sourcerepositoryprojectversion.id).all()

        for hook in hooks:
            db.delete(hook)

        todelete.append(sourcerepositoryprojectversion)

    db.commit()

    for d in todelete:
        db.delete(d)

    # delete references from copies and snapshots
    relatives = db.query(ProjectVersion).filter(ProjectVersion.baseprojectversion_id == projectversion.id).all()
    for relative in relatives:
        relative.baseprojectversion_id = None

    db.commit()

    # delete projectversion
    db.delete(projectversion)
    db.commit()

    await enqueue_aptly(
        {
            "delete_repository": [
                basemirror_name,
                basemirror_version,
                project_name,
                project_version,
                architectures
            ]
        }
    )
    return OKResponse("Deleted Project Version")


@app.http_delete("/api2/project/{project_id}/{projectversion_id}/repository/{sourcerepository_id}")
@req_role(["member", "owner"])
async def remove_repository2(request):
    """
    Remove a sourcerepositories from a projectversion.

    ---
    description: Remove a sourcerepositories from a projectversion.
    tags:
        - ProjectVersions
    consumes:
        - application/json
    parameters:
        - name: projectversion_id
          in: path
          required: true
          type: integer
        - name: sourcerepository_id
          in: path
          required: true
          type: integer
    produces:
        - text/json
    """
    db = request.cirrina.db_session
    projectversion = get_projectversion(request)
    if not projectversion:
        return ErrorResponse(400, "Projectversion not found")
    if projectversion.is_locked:
        return ErrorResponse(400, "Projectversion is locked")
    try:
        sourcerepository_id = int(request.match_info["sourcerepository_id"])
    except Exception:
        return ErrorResponse(400, "No valid sourcerepository_id received")

    sourcerepository = db.query(SourceRepository).filter(SourceRepository.id == sourcerepository_id).first()
    if not sourcerepository:
        return ErrorResponse(400, "Sourcerepository {} could not been found".format(sourcerepository_id))

    # get the association of the projectversion and the sourcerepository
    sourcerepositoryprojectversion = db.query(SouRepProVer).filter(SouRepProVer.sourcerepository_id == sourcerepository_id,
                                                                   SouRepProVer.projectversion_id == projectversion.id).first()
    if not sourcerepositoryprojectversion:
        return ErrorResponse(400, "Could not find the sourcerepository for the projectversion")

    projectversion.sourcerepositories.remove(sourcerepository)
    db.commit()

    return OKResponse("Sourcerepository removed from projectversion")


@app.http_get("/api2/project/{project_name}/{project_version}/aptsources")
async def get_apt_sources2(request):
    """
    Returns apt sources list for given project,
    projectversion and distrelease.

    ---
    description: Returns apt sources list.
    tags:
        - Projects
    consumes:
        - application/x-www-form-urlencoded
    parameters:
        - name: project_name
          in: path
          required: true
          type: str
        - name: project_version
          in: path
          required: true
          type: str
    produces:
        - text/json
    responses:
        "200":
            description: successful
        "400":
            description: Parameter missing
    """
    db = request.cirrina.db_session
    unstable = request.GET.getone("unstable", False)
    if unstable == "true":
        unstable = True
    internal = request.GET.getone("internal", False)
    if internal == "true":
        internal = True

    projectversion = get_projectversion(request)
    if not projectversion:
        return ErrorResponse(400, "projectversion not found")

    deps = [(projectversion.id, projectversion.ci_builds_enabled)]
    deps += get_projectversion_deps(projectversion.id, db)

    cfg = Configuration()
    apt_url = None
    if not internal:
        apt_url = cfg.aptly.get("apt_url_public")
    if not apt_url:
        apt_url = cfg.aptly.get("apt_url")
    keyfile = cfg.aptly.get("key")

    sources_list = "# APT Sources for project {0} {1}\n".format(projectversion.project.name, projectversion.name)
    sources_list += "# GPG-Key: {0}/{1}\n".format(apt_url, keyfile)
    if not projectversion.project.is_basemirror and projectversion.basemirror:
        sources_list += "\n# Base Mirror\n"
        sources_list += "{}\n".format(projectversion.basemirror.get_apt_repo(internal=internal))

    sources_list += "\n# Project Sources\n"
    for d in deps:
        dep = db.query(ProjectVersion).filter(ProjectVersion.id == d[0]).first()
        if not dep:
            logger.error("projectsources: projecversion %d not found", d[0])
        sources_list += "{}\n".format(dep.get_apt_repo(internal=internal))
        # ci builds requested & use ci builds from this dep & dep has ci builds
        if unstable and d[1] and dep.ci_builds_enabled:
            sources_list += "{}\n".format(dep.get_apt_repo(dist="unstable", internal=internal))

    return web.Response(status=200, text=sources_list)


@app.http_get("/api2/project/{project_id}/{projectversion_id}/dependents")
@app.authenticated
async def get_projectversion_dependents(request):
    """
    Returns a list of projectversions.

    ---
    description: Returns a list of projectversions.
    tags:
        - ProjectVersions
    consumes:
        - application/x-www-form-urlencoded
    parameters:
        - name: basemirror_id
          in: query
          required: false
          type: integer
        - name: is_basemirror
          in: query
          required: false
          type: bool
        - name: project_id
          in: query
          required: false
          type: integer
        - name: project_name
          in: query
          required: false
          type: string
        - name: page
          in: query
          required: false
          type: integer
        - name: page_size
          in: query
          required: false
          type: integer
    produces:
        - text/json
    responses:
        "200":
            description: successful
        "500":
            description: internal server error
    """
    db = request.cirrina.db_session
    candidates = request.GET.getone("candidates", None)
    filter_name = request.GET.getone("q", None)

    if candidates:
        candidates = candidates == "true"

    projectversion = get_projectversion(request)
    if not projectversion:
        return ErrorResponse(400, "Projectversion not found")

    # get existing dependents
    deps = projectversion.dependents
    dep_ids = []
    for d in deps:
        dep_ids.append(d.id)

    if candidates:  # return candidate dependents
        results = []
        cands_query = db.query(ProjectVersion).filter(ProjectVersion.basemirror_id == projectversion.basemirror_id,
                                                      ProjectVersion.id != projectversion.id,
                                                      ProjectVersion.id.notin_(dep_ids))
        BaseMirror = aliased(ProjectVersion)
        dist_query = db.query(ProjectVersion).join(BaseMirror, BaseMirror.id == ProjectVersion.basemirror_id).filter(
                                                   ProjectVersion.dependency_policy == "distribution",
                                                   BaseMirror.project_id == projectversion.basemirror.project_id,
                                                   BaseMirror.id != projectversion.basemirror_id,
                                                   ProjectVersion.id.notin_(dep_ids))

        any_query = db.query(ProjectVersion).filter(
                                                   ProjectVersion.dependency_policy == "any",
                                                   ProjectVersion.id != projectversion.id,
                                                   ProjectVersion.id.notin_(dep_ids))
        cands = cands_query.union(dist_query, any_query).join(Project).order_by(Project.is_mirror,
                                                                                func.lower(Project.name),
                                                                                func.lower(ProjectVersion.name))
        if filter_name:
            cands = cands.filter(ProjectVersion.fullname.ilike("%{}%".format(filter_name)))

        for cand in cands.all():
            results.append(cand.data())

        data = {"total_result_count": len(results), "results": results}
        return OKResponse(data)

    # return existing dependents
    results = []
    for d in deps:
        dep = db.query(ProjectVersion).filter(ProjectVersion.id == d.id)
        if filter_name:
            dep = dep.filter(ProjectVersion.fullname.ilike("%{}%".format(filter_name)))
        dep = dep.first()
        if dep:
            data = dep.data()
            results.append(data)

    # FIXME paginate ??
    data = {"total_result_count": len(results), "results": results}
    return OKResponse(data)
