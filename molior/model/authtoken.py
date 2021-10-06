from sqlalchemy import Column, ForeignKey, String, Integer

from .database import Base


class AuthToken(Base):
    __tablename__ = "authtoken"

    id = Column(Integer, primary_key=True)
    project_id = Column(ForeignKey("project.id"))
    token = Column(String)
    description = Column(String)
