
import logging

from time import time

from pyramid.exceptions import ConfigurationError

try:
    import ldap3
except ImportError:  # pragma: no cover
    # this is for benefit of being able to build the docs on rtd.org
    class _Ldap3Module(object):
        BASE = None
        LEVEL = None
        SUBTREE = None
        REUSABLE = None
    ldap3 = _Ldap3Module()
    LDAPException = Exception
else:
    LDAPException = ldap3.core.exceptions.LDAPException

logger = logging.getLogger(__name__)

_ord = ord if str is bytes else int

_escape_for_search = {
    '*': '\\2A', '(': '\\28', ')': '\\29', '\\': '\\5C', '\0': '\\00'}

__all__ = ['get_ldap_connector', 'get_groups', 'groupfinder']


def escape_for_search(s):
    """Escape search string for LDAP according to RFC4515 when necessary."""
    if not s:
        return s
    if isinstance(s, bytes):
        try:
            s = s.decode('utf-8')
        except UnicodeDecodeError:
            return ''.join('\\%02x' % _ord(b) for b in s)
    return ''.join((_escape_for_search.get(c, c) for c in s))


class _LDAPQuery(object):
    """Represents an LDAP query.

    Provides rudimentary in-RAM caching of query results.
    """

    def __init__(self, base_dn, filter_tmpl, scope, attributes, cache_period):
        self.base_dn = base_dn
        self.filter_tmpl = filter_tmpl
        self.scope = scope
        self.attributes = attributes
        self.cache_period = cache_period
        self.last_timeslice = 0
        self.cache = {}

    def __str__(self):
        return ('base_dn={base_dn}, filter_tmpl={filter_tmpl}, '
                'scope={scope}, attributes={attributes}, '
                'cache_period={cache_period}'.format(**self.__dict__))

    def query_cache(self, cache_key):
        now = time()
        ts = _timeslice(self.cache_period, now)

        if ts > self.last_timeslice:
            logger.debug(
                'dumping cache; now ts: %r, last_ts: %r',
                ts, self.last_timeslice)
            self.cache = {}
            self.last_timeslice = ts

        return self.cache.get(cache_key)

    def execute(self, manager, **kw):
        cache_key = (self.base_dn % kw, self.filter_tmpl % kw)

        logger.debug('searching for %r', cache_key)

        result = self.query_cache(cache_key) if self.cache_period else None
        if result is None:
            with manager.connection() as conn:
                ret = conn.search(
                    search_scope=self.scope,
                    attributes=self.attributes, *cache_key)
                result, ret = conn.get_response(ret)
            if result is None:
                result = []
            else:
                result = [(r['dn'], r['attributes']) for r in result
                          if 'dn' in r]
                if self.cache_period:
                    self.cache[cache_key] = result
        else:
            logger.debug('result for %r retrieved from cache', cache_key)

        logger.debug('search result: %r', result)

        return result


def _timeslice(period, when=None):
    if when is None:  # pragma: no cover
        when = time()
    return when - (when % period)


def _activity_identifier(base_identifier, realm=None):
    if realm:
        return '-'.join((base_identifier, realm))
    else:
        return base_identifier


def _registry_identifier(base_identifier, realm=None):
    if realm:
        return '_'.join((base_identifier, realm))
    else:
        return base_identifier


def _pool_identifier(base_identifier, realm=None):
    if realm:
        return '_'.join((base_identifier, realm))
    else:
        return base_identifier

class ConnectionManager(object):
    """Provides API methods for managing LDAP connections."""

    # noinspection PyShadowingNames
    def __init__(
            self, uri, bind=None, passwd=None, tls=None,
            use_pool=True, pool_size=10, pool_lifetime=3600,
            get_info=None, ldap3=ldap3, realm=None):
        self.ldap3 = ldap3
        uris = uri if isinstance(uri, (list, tuple)) else uri.split()
        self.uri = uri[0] if len(uris) == 1 else uris
        if get_info is None:
            get_info = ldap3.NONE
        servers = []
        for uri in uris:
            try:
                schema, host = uri.split('://', 1)
            except ValueError:
                schema, host = 'ldap', uri
            use_ssl = schema == 'ldaps'
            try:
                host, port = host.split(':', 1)
                port = int(port)
            except ValueError:
                host, port = host, 636 if use_ssl else 389
            server = self.ldap3.Server(
                host, port=port, use_ssl=use_ssl, tls=tls,
                get_info=get_info)
            servers.append(server)
        self.server = servers[
            0] if len(servers) == 1 else self.ldap3.ServerPool(servers)
        self.bind, self.passwd = bind, passwd
        if use_pool:
            self.strategy = ldap3.REUSABLE
            self.pool_name = _pool_identifier('pyramid_ldap3', realm)
            self.pool_size = pool_size
            self.pool_lifetime = pool_lifetime
        else:
            self.strategy = ldap3.ASYNC
            self.pool_name = self.pool_size = self.pool_lifetime = None

    def __str__(self):
        return ('uri={uri}, bind={bind}/{passwd},pool={pool_size}'.format(
            **self.__dict__))

    def connection(self, user=None, password=None):
        if user:
            conn = self.ldap3.Connection(
                self.server, user=user, password=password,
                client_strategy=ldap3.SYNC,
                auto_bind=True, lazy=False, read_only=True)
        else:
            conn = self.ldap3.Connection(
                self.server, user=self.bind, password=self.passwd,
                client_strategy=self.strategy,
                pool_name=self.pool_name, pool_size=self.pool_size,
                pool_lifetime=self.pool_lifetime,
                auto_bind=True, lazy=False, read_only=True)
        return conn


class Connector(object):
    """Provides API methods for accessing LDAP authentication information."""

    def __init__(self, registry, manager, realm=None):
        self.registry = registry
        self.manager = manager
        self.realm = realm
        self.login_qry_identif = _registry_identifier('ldap_login_query', realm)
        self.group_qry_identif = _registry_identifier('ldap_groups_query', realm)

    def authenticate(self, login, password):
        """Validate the given login name and password.

        Given a login name and a password, return a tuple of ``(dn,
        attrdict)`` if the matching user if the user exists and his password
        is correct.  Otherwise return ``None``.

        In a ``(dn, attrdict)`` return value, ``dn`` will be the
        distinguished name of the authenticated user.  Attrdict will be a
        dictionary mapping LDAP user attributes to sequences of values.

        A zero length password will always be considered invalid since it
        results in a request for "unauthenticated authentication" which should
        not be used for LDAP based authentication. See `section 5.1.2 of
        RFC-4513 <https://tools.ietf.org/html/rfc4513#section-5.1.2>`_ for a
        description of this behavior.

        If :meth:`pyramid.config.Configurator.ldap_set_login_query` was not
        called, using this function will raise an
        :exc:`pyramid.exceptions.ConfiguratorError`.
        """

        if password == '':
            return None

        search = getattr(self.registry, self.login_qry_identif, None)
        if search is None:
            raise ConfigurationError(
                'ldap_set_login_query was not called during setup')

        result = search.execute(
            self.manager, login=escape_for_search(login),
            password=escape_for_search(password))

        if not result or len(result) > 1:
            return None
        result = result[0]
        login_dn = result[0]

        try:
            self.manager.connection(login_dn, password).unbind()
        except LDAPException:
            logger.debug(
                'Exception in authenticate with login %r', login,
                exc_info=True)
            return None

        return result

    def user_groups(self, userdn):
        """Get the groups the user belongs to.

        Given a user DN, return a sequence of LDAP attribute dictionaries
        matching the groups of which the DN is a member.  If the DN does not
        exist, return ``None``.

        In a return value ``[(dn, attrdict), ...]``, ``dn`` will be the
        distinguished name of the group.  Attrdict will be a dictionary
        mapping LDAP group attributes to sequences of values.

        If :meth:`pyramid.config.Configurator.ldap_set_groups_query` was not
        called, using this function will raise an
        :exc:`pyramid.exceptions.ConfiguratorError`

        """
        search = getattr(self.registry, self.group_qry_identif, None)
        if search is None:
            raise ConfigurationError(
                'set_ldap_groups_query was not called during setup')
        try:
            result = search.execute(
                self.manager, userdn=escape_for_search(userdn))
        except LDAPException:
            logger.debug(
                'Exception in user_groups with userdn %r', userdn,
                exc_info=True)
            return None

        return result


def ldap_set_login_query(
        config, base_dn, filter_tmpl,
        scope=ldap3.LEVEL, attributes=None,
        cache_period=0, realm=None):
    """Configurator method to set the LDAP login search.

    ``base_dn`` is the DN at which to begin the search.
    ``filter_tmpl`` is a string which can be used as an LDAP filter:
    it should contain the replacement value ``%(login)s``.
    ``scope`` is any valid LDAP scope value
    (e.g. ``ldap3.LEVEL`` or ``ldap3.SUBTREE``).
    ``attributes`` is a list of attributes that shall be returned
    (can also be set to None or ``ldap3.ALL_ATTRIBUTES``).
    ``cache_period`` is the number of seconds to cache login search results;
    if it is 0, login search results will not be cached.
    ``realm`` is a realm for this connection. This allows multiple
    ldap servers to be used.  **default: None**

    Example::

        config.set_ldap_login_query(
            base_dn='CN=Users,DC=example,DC=com',
            filter_tmpl='(sAMAccountName=%(login)s)',
            scope=ldap3.LEVEL)

    The registered search must return one and only one value to be considered
    a valid login.
    """
    query_identif = _registry_identifier('ldap_login_query', realm)
    intr_identif = _registry_identifier('pyramid_ldap3', realm)
    act_identif = _activity_identifier('pyramid_ldap3', realm)

    query = _LDAPQuery(base_dn, filter_tmpl, scope, attributes, cache_period)

    def register():
        setattr(config.registry, query_identif, query)

    login_query_identifier = '{} login query'.format(intr_identif)
    intr = config.introspectable(
        login_query_identifier,
        None,
        str(query),
        login_query_identifier)

    config.action(act_identif, register, introspectables=(intr,))


def ldap_set_groups_query(
        config, base_dn, filter_tmpl,
        scope=ldap3.SUBTREE, attributes=None,
        cache_period=0, realm=None):
    """ Configurator method to set the LDAP groups search.

    ``base_dn`` is the DN at which to begin the search.
    ``filter_tmpl`` is a string which can be used as an LDAP filter:
    it should contain the replacement value ``%(userdn)s``.
    ``scope`` is any valid LDAP scope value
    (e.g. ``ldap3.LEVEL`` or ``ldap3.SUBTREE``).
    ``attributes`` is a list of attributes that shall be returned
    (can also be set to None or ``ldap3.ALL_ATTRIBUTES``).
    ``cache_period`` is the number of seconds to cache groups search results;
    if it is 0, groups search results will not be cached.
    ``realm`` is a realm for this connection. This allows multiple ldap
    servers to be used.  **default: None**

    Example::

        config.set_ldap_groups_query(
            base_dn='CN=Users,DC=example,DC=com',
            filter_tmpl='(&(objectCategory=group)(member=%(userdn)s))'
            scope=ldap3.SUBTREE)

    """
    query_identif = _registry_identifier('ldap_groups_query', realm)
    intr_identif = _registry_identifier('pyramid_ldap3', realm)
    act_identif = _activity_identifier('ldap-set-groups-query', realm)

    query = _LDAPQuery(base_dn, filter_tmpl, scope, attributes, cache_period)

    def register():
        setattr(config.registry, query_identif, query)

    groups_query_identifier = '{} groups query'.format(intr_identif)
    intr = config.introspectable(
        groups_query_identifier,
        None,
        str(query),
        groups_query_identifier)

    config.action(act_identif, register, introspectables=(intr,))


def ldap_setup(
        config, uri,
        bind=None, passwd=None, use_tls=False,
        use_pool=True, pool_size=10, pool_lifetime=3600,
        get_info=None, realm=None):
    """Configurator method to set up an LDAP connection pool.

    - **uri**: ldap server uri(s) **[mandatory]**
    - **bind**: default bind that will be used to bind a connector.
      **default: None**
    - **passwd**: default password that will be used to bind a connector.
      **default: None**
    - **use_tls**: activate TLS when connecting. **default: False**
    - **use_pool**: activates the connection pool. If False, will recreate a
      connector each time. **default: True**
    - **pool_size**: connection pool size. **default: 10**
    - **pool_lifetime**: number of seconds before recreating a new connection
      when using a connection pool.  **default: 3600**
    - **get_info**: specifies if schema or server specific info shall be read
      for proper formatting of attributes.  **default: None**
    - **realm**: specify a realm for this connection. This allows multiple ldap servers to be used.  **default: None**
    """
    conn_identif = _registry_identifier('ldap_connector', realm)
    intr_identif = _registry_identifier('pyramid_ldap3', realm)
    act_identif = _activity_identifier('ldap-setup', realm)

    manager = ConnectionManager(
        uri, bind, passwd, use_tls,
        use_pool, pool_size if use_pool else None,
        pool_lifetime if use_pool else None, get_info, realm=realm)

    def get_connector(request):
        return Connector(request.registry, manager, realm)

    config.add_request_method(
        get_connector, conn_identif, property=True, reify=True)

    introspectable_name = '{} setup'.format(intr_identif)
    intr = config.introspectable(
        introspectable_name,
        None,
        str(manager),
        introspectable_name)
    config.action(act_identif, None, introspectables=(intr,))

def get_ldap_connector_name(realm=None):
    """ Return the name of the connector attached to the request
    for the named **realm**."""
    return _registry_identifier('ldap_connector', realm)

def get_ldap_connector(request, realm=None):
    """Return the LDAP connector attached to the request.

    If :meth:`pyramid.config.Configurator.ldap_setup` was not called, using
    this function will raise an :exc:`pyramid.exceptions.ConfigurationError`.
    """
    conn_name = get_ldap_connector_name(realm)
    connector = getattr(request, conn_name, None)
    if connector is None:
        if LDAPException is Exception:  # pragma: no cover
            raise ImportError(
                'You must install ldap3 to use an LDAP connector.')
        raise ConfigurationError(
            'You must call Configurator.ldap_setup during setup '
            'to use an LDAP connector.')
    return connector


def get_groups(userdn, request):
    """Raw groupfinder function returning the complete group query result."""
    connector = get_ldap_connector(request)
    return connector.user_groups(userdn)


def groupfinder(userdn, request):
    """Groupfinder function for Pyramid.

    A groupfinder implementation useful in conjunction with out-of-the-box
    Pyramid authentication policies.  It returns the DN of each group
    belonging to the user specified by ``userdn`` to as a principal
    in the list of results; if the user does not exist, it returns None.
    """
    groups = get_groups(userdn, request)
    if groups:
        groups = [r[0] for r in groups]
    return groups


def includeme(config):
    """Set up Configurator methods for pyramid_ldap3."""
    config.add_directive('ldap_setup', ldap_setup)
    config.add_directive('ldap_set_login_query', ldap_set_login_query)
    config.add_directive('ldap_set_groups_query', ldap_set_groups_query)
