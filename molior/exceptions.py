class AptlyError(Exception):
    """Base exception for all aptly specific errors."""

    def __init__(self, error):
        super(AptlyError, self).__init__(error)


class HTTPError(Exception):
    pass


class ConfigurationError(Exception):
    pass


class UnauthorizedError(HTTPError):
    """
    Exception which is raised if the request returned
    a status_code which means unauthorized.
    """

    def __init__(self, status_code):
        super(UnauthorizedError, self).__init__(
            "The webserver returned status code '{0}': Unauthorized".format(status_code)
        )


class NotFoundError(HTTPError):
    """
    Exception which is raised if the request returned
    a status_code which means that the page could not be found.
    """

    def __init__(self, status_code):
        super(NotFoundError, self).__init__(
            "The webserver returned status code '{0}': Not Found".format(status_code)
        )


class BadRequestError(HTTPError):
    """
    Exception which is raised if the request returned
    a status_code which means that a Bad Request occured.
    """

    def __init__(self, status_code):
        super(BadRequestError, self).__init__(
            "The webserver returned status code '{0}': Bad Request".format(status_code)
        )
