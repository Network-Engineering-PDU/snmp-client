from .oid import Oid


class BaseOidManager:
    def first_oid(self) -> Oid:
        """ Returns the first oid of this manager. """
        raise NotImplementedError

    def last_oid(self) -> Oid:
        """ Returns the last oid of this manager. """
        raise NotImplementedError

    def get(self, oid: Oid) -> [str, str]:
        """ Returns the tuple (type, value), as strings, if the oid is
        present. Returns None otherwise.
        """
        raise NotImplementedError

    def get_next_oid(self, oid: Oid) -> Oid:
        """ Returns the next oid in this manager.
        If lower than the manager first oid, returns the first oid.
        If bigger than the manager last oid, returns None.
        """
        raise NotImplementedError
