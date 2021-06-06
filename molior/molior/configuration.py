import yaml

from pathlib import Path

from ..app import logger
from ..exceptions import ConfigurationError


class Configuration(object):
    """
    Molior Configuration Class
    """

    CONFIGURATION_PATH = "/etc/molior/molior.yml"

    def __init__(self, config_file=CONFIGURATION_PATH):
        self._config_file = config_file
        self._config = None

    def _load_config(self, file_path):
        """
        Loads a configuration file.

        Args:
            filepath (str): Path to the config file.
        """
        cfg_file = Path(file_path)
        if not cfg_file.exists():
            logger.error("configuration file '%s' does not exist", file_path)
            self._config = {}
            return

        config_file = open(file_path, "r")
        config = yaml.safe_load(config_file)
        self._config = config if config else {}
        config_file.close()

    def config(self):
        """
        Returns the configuration.
        """
        self._load_config(self._config_file)
        return self._config

    def __getattr__(self, name):
        """
        Gets config value of given key/name.

        Args:
            name (str): Name of the attribute/key.

        Returns:
            Value of the given key.
        """
        if not self._config:
            self._load_config(self._config_file)

        return self._config.get(name, {})


class AptlyConfiguration(Configuration):

    def __init__(self):
        super(Configuration, self).__init__()

    @property
    def apt_url(self):
        t = self.aptly.get("apt_url_public")
        if not t:
            t = self.aptly.get("apt_url")
        if not t:
            raise ConfigurationError("missing configuration in /etc/molior.yml: aptly:apt_url_public")
        return t

    @property
    def keyfile(self):
        t = self.aptly.get("apt_key_file")
        if not t:
            t = self.aptly.get("key")
        if not t:
            raise ConfigurationError("missing configuration in /etc/molior.yml: aptly:apt_key_file")
        return t
