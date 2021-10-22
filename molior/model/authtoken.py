from sqlalchemy import Column, String, Integer

from .database import Base


class AuthToken(Base):
    __tablename__ = "authtoken"

    id = Column(Integer, primary_key=True)
    token = Column(String)
    description = Column(String)
