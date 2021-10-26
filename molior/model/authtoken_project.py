from sqlalchemy import Column, ForeignKey, Enum, PrimaryKeyConstraint
from sqlalchemy.orm import relationship

from .database import Base
from .authtoken import Authtoken
from .project import Project
from .userrole import USER_ROLES


class Authtoken_Project(Base):
    __tablename__ = "authtoken_project"
    __table_args__ = (PrimaryKeyConstraint("authtoken_id", "project_id"),)

    authtoken_id = Column(ForeignKey("authtoken.id"))
    authtoken = relationship(Authtoken)
    project_id = Column(ForeignKey("project.id"))
    project = relationship(Project)
    roles = Column(Enum(*USER_ROLES, name="role_enum"))
