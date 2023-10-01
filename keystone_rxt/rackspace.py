# Copyright 2023 Cloudnull
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from dateutil import parser
import re
import hashlib
import json

import requests
from oslo_log import log
import flask

from keystone.auth.plugins import base
from keystone.auth.plugins import mapped
from keystone.auth.plugins import password
from keystone.auth.plugins import totp
from keystone.common import cache as ks_cache
from keystone.common import provider_api
import keystone.conf
from keystone import exception
from keystone.i18n import _


keystone.conf.CONF.set_override("caching", True, group="federation")

RXT_SESSION_CACHE = ks_cache.create_region(name="rxt_sess")
ks_cache.cache.configure_cache_region(keystone.conf.CONF, RXT_SESSION_CACHE)
RXT_SESSION_CACHE.expiration_time = 60

RXT_SERVICE_CACHE = ks_cache.create_region(name="rxt_srv")
ks_cache.cache.configure_cache_region(keystone.conf.CONF, RXT_SERVICE_CACHE)
RXT_SERVICE_CACHE.expiration_time = 30

LOG = log.getLogger(__name__)
PROVIDERS = provider_api.ProviderAPIs
RACKPSACE_IDENTITY_V2 = "https://identity.api.rackspacecloud.com/v2.0/tokens"


class RXTv2Credentials(object):
    """Return an authenticate object based on the provided auth payload.

    Check the value type of the provided auth payload and determine the
    correct authentication method.
    """

    hash_types = [r"^[a-fA-F0-9]{%s}$" % i for i in [32, 40, 64]]
    rxt_auth_payload = None
    rxt_headers = dict()
    session = requests.Session()
    session_id = None

    def __init__(self, auth_payload):
        """Initialize the RXTv2Credentials object.

        This method is used to initialize the RXTv2Credentials object. The
        method will hash the auth payload and username for use in caching
        and session headers.

        :param dict auth_payload: The auth payload to be used for authentication.
        """

        self.auth_payload = auth_payload
        self.hashed_auth_payload = hashlib.sha224(
            json.dumps(self.auth_payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        self.hashed_auth_user = hashlib.sha224(
            self._username.encode("utf-8")
        ).hexdigest()

    @property
    def _sessionID(self):
        """Return a parsed sessionID property.

        This property is used to return the sessionID from the session header.

        :returns: The sessionID from the session header.
        :rtype: str or None
        """

        r = re.compile(r"""sessionId='(.*?[^\\])'""")
        try:
            session_header = RXT_SESSION_CACHE.get(self.hashed_auth_user)
            if session_header:
                LOG.debug("Using cached Rackspace Session header")
            session_id = r.findall(session_header).pop()
        except (IndexError, TypeError):
            return None
        else:
            return session_id

    @property
    def _username(self):
        """Return a parsed username property.

        This property is used to return the username from the auth payload.

        :returns: The username from the auth payload.
        :rtype: str
        """

        try:
            return self.auth_payload["user"]["name"]
        except KeyError:
            try:
                return self.auth_payload["user"]["username"]
            except KeyError:
                raise exception.Unauthorized(
                    _("The authentication payload is missing the name")
                )

    @property
    def _password(self):
        """Return a parsed password property.

        This property is used to return the password from the auth payload.

        :returns: The password from the auth payload.
        :rtype: str
        """

        try:
            return self.auth_payload["user"]["password"]
        except KeyError:
            raise exception.Unauthorized(
                _("The authentication payload is missing the password")
            )

    @property
    def _passcode(self):
        """Return a parsed passcode property.

        This property is used to return the passcode from the auth payload.

        :returns: The passcode from the auth payload.
        :rtype: str or None
        """

        try:
            return self.auth_payload["user"]["passcode"]
        except KeyError:
            return None

    @property
    def apiKeyCredentials(self):
        """Return the API type.

        This method is used to return the apiKeyCredentials auth payload.

        :returns: The apiKeyCredentials auth payload.
        :rtype: tuple
        """

        return "apiKeyCredentials", {
            "auth": {
                "RAX-KSKEY:apiKeyCredentials": {
                    "username": self._username,
                    "apiKey": self._password,
                }
            }
        }

    @property
    def passwordCredentials(self):
        """Return the Password type.

        This method is used to return the passwordCredentials auth payload.

        :returns: The passwordCredentials auth payload.
        :rtype: tuple
        """

        return "passwordCredentials", {
            "auth": {
                "passwordCredentials": {
                    "username": self._username,
                    "password": self._password,
                }
            }
        }

    @property
    def passcodeCredentials(self):
        """Return the Passcode type.

        This method is used to return the passcodeCredentials auth payload.

        :returns: The passcodeCredentials auth payload.
        :rtype: tuple
        """

        return "passcodeCredentials", {
            "auth": {
                "RAX-AUTH:passcodeCredentials": {
                    "passcode": self._passcode,
                }
            }
        }

    def __enter__(self):
        """Return the correct authentication method.

        This method is used to return the authentication method which is
        evaluating the password parameter in the auth payload. If the
        password is a valid `hash_types` then the auth method is first
        apiKeyCredentials, otherwise the passwordCredentials will be used.
        """

        LOG.debug("Rackspace IDP Login started")
        self.session_id = self._sessionID
        if self._passcode and self.session_id:
            LOG.debug("Using passcode auth payload")
            self.rxt_auth_payload = self.passcodeCredentials
            self.rxt_headers["X-SessionId"] = self.session_id
        else:
            for hash_type in self.hash_types:
                if re.match(hash_type, self._password) is not None:
                    LOG.debug("Using api key auth payload")
                    self.rxt_auth_payload = self.apiKeyCredentials
                    break

        if not self.rxt_auth_payload:
            LOG.debug("Using password auth payload")
            self.rxt_auth_payload = self.passwordCredentials

        return self

    def __exit__(self, *args, **kwargs):
        """Close the session.

        This method is used to close the session after the authentication
        method has completed.
        """

        self.session.close()
        LOG.debug("Rackspace IDP Login complete, returning to OS")

    def set_federation_env(
        self,
        username=None,
        email=None,
        domain_id=None,
        tenant_name=None,
        tenant_id=None,
        org_person_type=None,
    ):
        """Set the federation environment variables.

        Set the federation environment variables based on the provided
        parameters. If the parameters are not provided the environment
        variables will not be set.

        :param str username: The username to be set in the environment.
        :param str email: The email to be set in the environment.
        :param str domain_id: The domain_id to be set in the environment.
        :param str tenant_name: The tenant_name to be set in the environment.
        :param str tenant_id: The tenant_id to be set in the environment.
        :param str org_person_type: The org_person_type to be set in the
                                    environment.
        """

        if username:
            flask.request.environ["RXT_UserName"] = username
        if email:
            flask.request.environ["RXT_Email"] = email
        if domain_id:
            flask.request.environ["RXT_DomainID"] = domain_id
        if tenant_name:
            flask.request.environ["RXT_TenantName"] = tenant_name
        if tenant_id:
            flask.request.environ["RXT_TenantID"] = tenant_id
        if org_person_type:
            flask.request.environ["RXT_orgPersonType"] = org_person_type

    def parse_service_catalog(self, service_catalog):
        """Parse the Rackspace Service Catalog and set the environment variables.

        We're parsing the Rackspace Service Catalog and setting the federation
        environment variables. If there's an error parsing the service catalog
        the method will deny access.
        """

        try:
            access = service_catalog["access"]
            access_user = access["user"]
            access_token = access["token"]
            access_user_roles = set(
                [
                    i["name"].split(":")[-1]
                    for i in service_catalog["access"]["user"]["roles"]
                ]
            )
            self.set_federation_env(
                username=access_user["name"],
                email=access_user["email"],
                domain_id=access_user["RAX-AUTH:domainId"],
                tenant_name=access_token["tenant"]["name"],
                tenant_id=access_token["tenant"]["id"],
                org_person_type=";".join(access_user_roles),
            )
        except KeyError as e:
            raise exception.Unauthorized(
                "Could not parse the Rackspace Service Catalog for access:"
                " {error}".format(error=e)
            )

    def return_auth_handler(self):
        """Return the auth handler response.

        Using the provided auth payload, return the auth handler response.
        This will work for both scoped and unscoped tokens.
        """

        self.auth_payload["identity_provider"] = "rackspace"
        self.auth_payload["protocol"] = "rackspace"
        if "id" in self.auth_payload:
            token_ref = PROVIDERS.token_provider_api.validate_token(
                self.auth_payload["id"]
            )
            response_data = mapped.handle_scoped_token(
                token_ref, PROVIDERS.federation_api, PROVIDERS.identity_api
            )
        else:
            response_data = mapped.handle_unscoped_token(
                self.auth_payload,
                PROVIDERS.resource_api,
                PROVIDERS.federation_api,
                PROVIDERS.identity_api,
                PROVIDERS.assignment_api,
                PROVIDERS.role_api,
            )
        return base.AuthHandlerResponse(
            status=True, response_body=None, response_data=response_data
        )

    def _rxt_auth(self, auth_data, auth_type):
        """Authenticate using the Rackspace Identity API.

        Internal method used to authenticate using the Rackspace Identity API.
        The method will return True if the Rackspace Identity API returns a
        boolean. The boolean informs the main auth method if the user is
        attempting to use rackspace MFA.

        :param dict auth_data: The auth data to be used for authentication.
        :param str auth_type: The auth type to be used for authentication.
        :returns: True if the Rackspace Identity API returns a boolean.
        :rtype: bool
        """
        service_catalog = RXT_SERVICE_CACHE.get(self.hashed_auth_payload)
        if service_catalog:
            try:
                expires = service_catalog["access"]["token"]["expires"]
            except KeyError:
                LOG.debug(
                    "Rackspace service catalog is invalid, running cleanup."
                )
                RXT_SERVICE_CACHE.delete(self.hashed_auth_payload)
            else:
                token_expire = parser.parse(expires)
                if token_expire.timestamp() > token_expire.now().timestamp():
                    LOG.debug("Using cached Rackspace service catalog")
                    self.parse_service_catalog(service_catalog=service_catalog)
                    return False
                else:
                    LOG.debug(
                        "Rackspace service catalog is expired, running cleanup."
                    )
                    RXT_SERVICE_CACHE.delete(self.hashed_auth_payload)

        if self.session_id and auth_type == "passwordCredentials":
            LOG.debug("Found cached Rackspace session header.")
            return True

        r = self.session.post(
            RACKPSACE_IDENTITY_V2,
            json=auth_data,
            headers=self.rxt_headers,
        )
        if r.status_code == 401 and "WWW-Authenticate" in r.headers:
            LOG.debug("Caching Rackspace session header")
            RXT_SESSION_CACHE.set(
                self.hashed_auth_user, r.headers["WWW-Authenticate"]
            )
            return True
        else:
            r.raise_for_status()
            service_catalog = r.json()
            self.parse_service_catalog(service_catalog=service_catalog)
            LOG.debug("Caching Rackspace service catalog")
            RXT_SERVICE_CACHE.set(self.hashed_auth_payload, service_catalog)
            return False

    def rxt_auth(self, rxt_auth_mfa=False):
        """Authenticate using the Rackspace Identity API.

        This method is used to authenticate using the Rackspace Identity API.
        The method will return an auth handler response if the Rackspace
        Identity API returns a boolean. The boolean informs the main auth
        method if the user is attempting to use rackspace MFA.

        > In the event of a failure when using the apiKeyCredentials auth
          method, the method will attempt to re-authenticate using the
          passwordCredentials auth method.

        :param bool rxt_auth_mfa: If True, the user is attempting to use
                                  rackspace MFA.
        :returns: An auth handler response if the Rackspace Identity API
                  returns a boolean
        :rtype: keystone.auth.plugins.base.AuthHandlerResponse
        """

        auth_type, auth_data = self.rxt_auth_payload
        LOG.debug(
            "Attempting to authenticate using {auth_type}".format(
                auth_type=auth_type
            )
        )
        try:
            rxt_auth_mfa = self._rxt_auth(
                auth_data=auth_data, auth_type=auth_type
            )
        except requests.HTTPError:
            if auth_type == "apiKeyCredentials":
                self.rxt_auth_payload = self.passwordCredentials
                LOG.debug(
                    "Attempting to re-authenticate using passwordCredentials"
                )
                return self.rxt_auth(rxt_auth_mfa=False)

            raise exception.Unauthorized(
                "Failed to authenticate using the Rackspace Identity API"
                " with {auth_type}".format(auth_type=auth_type)
            )
        else:
            if rxt_auth_mfa is False:
                return self.return_auth_handler()
            else:
                return base.AuthHandlerResponse(
                    status=False,
                    response_body=None,
                    response_data=None,
                )


class RXTPassword(password.Password):
    def authenticate(self, auth_payload):
        """Return a signed request with an access key into a keystone token."""

        try:
            assert (
                auth_payload["user"]["domain"]["name"]
                == "rackspace_cloud_domain"
            )
        except (KeyError, AssertionError):
            LOG.debug("Using OS Password Authentication")
            return super(RXTPassword, self).authenticate(auth_payload)
        else:
            LOG.debug("Using Rackspace Global Authentication")
            with RXTv2Credentials(auth_payload=auth_payload) as rxt:
                return rxt.rxt_auth()


class RXTTOTP(totp.TOTP):
    def authenticate(self, auth_payload):
        """Return a signed request with an access key into a keystone token."""
        try:
            assert (
                auth_payload["user"]["domain"]["name"]
                == "rackspace_cloud_domain"
            )
        except (KeyError, AssertionError):
            LOG.debug("Using OS TOTP Authentication")
            return super(RXTTOTP, self).authenticate(auth_payload)
        else:
            LOG.debug("Using Rackspace Global Authentication TOTP")
            with RXTv2Credentials(auth_payload=auth_payload) as rxt:
                return rxt.rxt_auth()
