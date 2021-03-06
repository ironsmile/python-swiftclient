# Copyright (c) 2010-2012 OpenStack, LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging

try:
    from unittest import mock
except ImportError:
    import mock

import six
import socket
import testtools
import warnings
import tempfile
from hashlib import md5
from six.moves.urllib.parse import urlparse

from .utils import (MockHttpTest, fake_get_auth_keystone, StubResponse,
                    FakeKeystone, _make_fake_import_keystone_client)

from swiftclient.utils import EMPTY_ETAG
from swiftclient import client as c
import swiftclient.utils
import swiftclient


class TestClientException(testtools.TestCase):

    def test_is_exception(self):
        self.assertTrue(issubclass(c.ClientException, Exception))

    def test_format(self):
        exc = c.ClientException('something failed')
        self.assertTrue('something failed' in str(exc))
        test_kwargs = (
            'scheme',
            'host',
            'port',
            'path',
            'query',
            'status',
            'reason',
            'device',
        )
        for value in test_kwargs:
            kwargs = {
                'http_%s' % value: value,
            }
            exc = c.ClientException('test', **kwargs)
            self.assertTrue(value in str(exc))


class MockHttpResponse(object):
    def __init__(self, status=0, headers=None, verify=False):
        self.status = status
        self.status_code = status
        self.reason = "OK"
        self.buffer = []
        self.requests_params = None
        self.verify = verify
        self.md5sum = md5()
        self.headers = {'etag': '"%s"' % EMPTY_ETAG}
        if headers:
            self.headers.update(headers)
        self.closed = False

        class Raw(object):
            def __init__(self, headers):
                self.headers = headers

            def read(self, **kw):
                return ""

            def getheader(self, name, default):
                return self.headers.get(name, default)

        self.raw = Raw(headers)

    def read(self):
        return ""

    def close(self):
        self.closed = True

    def getheader(self, name, default):
        return self.headers.get(name, default)

    def getheaders(self):
        return dict(self.headers)

    def fake_response(self):
        return self

    def _fake_request(self, *arg, **kwarg):
        self.status = 200
        self.requests_params = kwarg
        if self.verify:
            for chunk in kwarg['data']:
                self.md5sum.update(chunk)

        # This simulate previous httplib implementation that would do a
        # putrequest() and then use putheader() to send header.
        for k, v in kwarg['headers'].items():
            self.buffer.append((k, v))
        return self.fake_response()


class TestHttpHelpers(MockHttpTest):

    def test_quote(self):
        value = b'bytes\xff'
        self.assertEqual('bytes%FF', c.quote(value))
        value = 'native string'
        self.assertEqual('native%20string', c.quote(value))
        value = u'unicode string'
        self.assertEqual('unicode%20string', c.quote(value))
        value = u'unicode:\xe9\u20ac'
        self.assertEqual('unicode%3A%C3%A9%E2%82%AC', c.quote(value))

    def test_http_connection(self):
        url = 'http://www.test.com'
        _junk, conn = c.http_connection(url)
        self.assertTrue(isinstance(conn, c.HTTPConnection))
        url = 'https://www.test.com'
        _junk, conn = c.http_connection(url)
        self.assertTrue(isinstance(conn, c.HTTPConnection))
        url = 'ftp://www.test.com'
        self.assertRaises(c.ClientException, c.http_connection, url)

    def test_encode_meta_headers(self):
        headers = {'abc': '123',
                   u'x-container-meta-\u0394': '123',
                   u'x-account-meta-\u0394': '123',
                   u'x-object-meta-\u0394': '123'}

        encoded_str_type = type(''.encode())
        r = swiftclient.encode_meta_headers(headers)

        self.assertEqual(len(headers), len(r))
        # ensure non meta headers are not encoded
        self.assertTrue('abc' in r)
        self.assertTrue(isinstance(r['abc'], encoded_str_type))
        del r['abc']

        for k, v in r.items():
            self.assertTrue(isinstance(k, encoded_str_type))
            self.assertTrue(isinstance(v, encoded_str_type))

    def test_set_user_agent_default(self):
        _junk, conn = c.http_connection('http://www.example.com')
        req_headers = {}

        def my_request_handler(*a, **kw):
            req_headers.update(kw.get('headers', {}))
        conn._request = my_request_handler

        # test the default
        conn.request('GET', '/')
        ua = req_headers.get('user-agent', 'XXX-MISSING-XXX')
        self.assertTrue(ua.startswith('python-swiftclient-'))

    def test_set_user_agent_per_request_override(self):
        _junk, conn = c.http_connection('http://www.example.com')
        req_headers = {}

        def my_request_handler(*a, **kw):
            req_headers.update(kw.get('headers', {}))
        conn._request = my_request_handler

        # test if it's actually set
        conn.request('GET', '/', headers={'User-Agent': 'Me'})
        ua = req_headers.get('user-agent', 'XXX-MISSING-XXX')
        self.assertEqual(ua, b'Me', req_headers)

    def test_set_user_agent_default_override(self):
        _junk, conn = c.http_connection(
            'http://www.example.com',
            default_user_agent='a-new-default')
        req_headers = {}

        def my_request_handler(*a, **kw):
            req_headers.update(kw.get('headers', {}))
        conn._request = my_request_handler

        # test setting a default
        conn._request = my_request_handler
        conn.request('GET', '/')
        ua = req_headers.get('user-agent', 'XXX-MISSING-XXX')
        self.assertEqual(ua, 'a-new-default')


class TestGetAuth(MockHttpTest):

    def test_ok(self):
        c.http_connection = self.fake_http_connection(200)
        url, token = c.get_auth('http://www.test.com', 'asdf', 'asdf')
        self.assertEqual(url, None)
        self.assertEqual(token, None)

    def test_invalid_auth(self):
        self.assertRaises(c.ClientException, c.get_auth,
                          'http://www.tests.com', 'asdf', 'asdf',
                          auth_version="foo")

    def test_auth_v1(self):
        c.http_connection = self.fake_http_connection(200, auth_v1=True)
        url, token = c.get_auth('http://www.test.com', 'asdf', 'asdf',
                                auth_version="1.0")
        self.assertEqual(url, 'storageURL')
        self.assertEqual(token, 'someauthtoken')

    def test_auth_v1_insecure(self):
        c.http_connection = self.fake_http_connection(200, 200, auth_v1=True)
        url, token = c.get_auth('http://www.test.com/invalid_cert',
                                'asdf', 'asdf',
                                auth_version='1.0',
                                insecure=True)
        self.assertEqual(url, 'storageURL')
        self.assertEqual(token, 'someauthtoken')

        e = self.assertRaises(c.ClientException, c.get_auth,
                              'http://www.test.com/invalid_cert',
                              'asdf', 'asdf', auth_version='1.0')
        # TODO: this test is really on validating the mock and not the
        # the full plumbing into the requests's 'verify' option
        self.assertIn('invalid_certificate', str(e))

    def test_auth_v1_timeout(self):
        # this test has some overlap with
        # TestConnection.test_timeout_passed_down but is required to check that
        # get_auth does the right thing when it is not passed a timeout arg
        orig_http_connection = c.http_connection
        timeouts = []

        def fake_request_handler(*a, **kw):
            if 'timeout' in kw:
                timeouts.append(kw['timeout'])
            else:
                timeouts.append(None)
            return MockHttpResponse(
                status=200,
                headers={
                    'x-auth-token': 'a_token',
                    'x-storage-url': 'http://files.example.com/v1/AUTH_user'})

        def fake_connection(*a, **kw):
            url, conn = orig_http_connection(*a, **kw)
            conn._request = fake_request_handler
            return url, conn

        with mock.patch('swiftclient.client.http_connection', fake_connection):
            c.get_auth('http://www.test.com', 'asdf', 'asdf',
                       auth_version="1.0", timeout=42.0)
            c.get_auth('http://www.test.com', 'asdf', 'asdf',
                       auth_version="1.0", timeout=None)
            c.get_auth('http://www.test.com', 'asdf', 'asdf',
                       auth_version="1.0")

        self.assertEqual(timeouts, [42.0, None, None])

    def test_auth_v2_timeout(self):
        # this test has some overlap with
        # TestConnection.test_timeout_passed_down but is required to check that
        # get_auth does the right thing when it is not passed a timeout arg
        fake_ks = FakeKeystone(endpoint='http://some_url', token='secret')
        with mock.patch('swiftclient.client._import_keystone_client',
                        _make_fake_import_keystone_client(fake_ks)):
            c.get_auth('http://www.test.com', 'asdf', 'asdf',
                       os_options=dict(tenant_name='tenant'),
                       auth_version="2.0", timeout=42.0)
            c.get_auth('http://www.test.com', 'asdf', 'asdf',
                       os_options=dict(tenant_name='tenant'),
                       auth_version="2.0", timeout=None)
            c.get_auth('http://www.test.com', 'asdf', 'asdf',
                       os_options=dict(tenant_name='tenant'),
                       auth_version="2.0")
        self.assertEqual(3, len(fake_ks.calls))
        timeouts = [call['timeout'] for call in fake_ks.calls]
        self.assertEqual([42.0, None, None], timeouts)

    def test_auth_v2_with_tenant_name(self):
        os_options = {'tenant_name': 'asdf'}
        req_args = {'auth_version': '2.0'}
        c.get_auth_keystone = fake_get_auth_keystone(os_options,
                                                     required_kwargs=req_args)
        url, token = c.get_auth('http://www.test.com', 'asdf', 'asdf',
                                os_options=os_options,
                                auth_version="2.0")
        self.assertTrue(url.startswith("http"))
        self.assertTrue(token)

    def test_auth_v2_with_tenant_id(self):
        os_options = {'tenant_id': 'asdf'}
        req_args = {'auth_version': '2.0'}
        c.get_auth_keystone = fake_get_auth_keystone(os_options,
                                                     required_kwargs=req_args)
        url, token = c.get_auth('http://www.test.com', 'asdf', 'asdf',
                                os_options=os_options,
                                auth_version="2.0")
        self.assertTrue(url.startswith("http"))
        self.assertTrue(token)

    def test_auth_v2_with_project_name(self):
        os_options = {'project_name': 'asdf'}
        req_args = {'auth_version': '2.0'}
        c.get_auth_keystone = fake_get_auth_keystone(os_options,
                                                     required_kwargs=req_args)
        url, token = c.get_auth('http://www.test.com', 'asdf', 'asdf',
                                os_options=os_options,
                                auth_version="2.0")
        self.assertTrue(url.startswith("http"))
        self.assertTrue(token)

    def test_auth_v2_with_project_id(self):
        os_options = {'project_id': 'asdf'}
        req_args = {'auth_version': '2.0'}
        c.get_auth_keystone = fake_get_auth_keystone(os_options,
                                                     required_kwargs=req_args)
        url, token = c.get_auth('http://www.test.com', 'asdf', 'asdf',
                                os_options=os_options,
                                auth_version="2.0")
        self.assertTrue(url.startswith("http"))
        self.assertTrue(token)

    def test_auth_v2_no_tenant_name_or_tenant_id(self):
        c.get_auth_keystone = fake_get_auth_keystone({})
        self.assertRaises(c.ClientException, c.get_auth,
                          'http://www.tests.com', 'asdf', 'asdf',
                          os_options={},
                          auth_version='2.0')

    def test_auth_v2_with_tenant_name_none_and_tenant_id_none(self):
        os_options = {'tenant_name': None,
                      'tenant_id': None}
        c.get_auth_keystone = fake_get_auth_keystone(os_options)
        self.assertRaises(c.ClientException, c.get_auth,
                          'http://www.tests.com', 'asdf', 'asdf',
                          os_options=os_options,
                          auth_version='2.0')

    def test_auth_v2_with_tenant_user_in_user(self):
        tenant_option = {'tenant_name': 'foo'}
        c.get_auth_keystone = fake_get_auth_keystone(tenant_option)
        url, token = c.get_auth('http://www.test.com', 'foo:bar', 'asdf',
                                os_options={},
                                auth_version="2.0")
        self.assertTrue(url.startswith("http"))
        self.assertTrue(token)

    def test_auth_v2_tenant_name_no_os_options(self):
        tenant_option = {'tenant_name': 'asdf'}
        c.get_auth_keystone = fake_get_auth_keystone(tenant_option)
        url, token = c.get_auth('http://www.test.com', 'asdf', 'asdf',
                                tenant_name='asdf',
                                os_options={},
                                auth_version="2.0")
        self.assertTrue(url.startswith("http"))
        self.assertTrue(token)

    def test_auth_v2_with_os_options(self):
        os_options = {'service_type': 'object-store',
                      'endpoint_type': 'internalURL',
                      'tenant_name': 'asdf'}
        c.get_auth_keystone = fake_get_auth_keystone(os_options)
        url, token = c.get_auth('http://www.test.com', 'asdf', 'asdf',
                                os_options=os_options,
                                auth_version="2.0")
        self.assertTrue(url.startswith("http"))
        self.assertTrue(token)

    def test_auth_v2_with_tenant_user_in_user_no_os_options(self):
        tenant_option = {'tenant_name': 'foo'}
        c.get_auth_keystone = fake_get_auth_keystone(tenant_option)
        url, token = c.get_auth('http://www.test.com', 'foo:bar', 'asdf',
                                auth_version="2.0")
        self.assertTrue(url.startswith("http"))
        self.assertTrue(token)

    def test_auth_v2_with_os_region_name(self):
        os_options = {'region_name': 'good-region',
                      'tenant_name': 'asdf'}
        c.get_auth_keystone = fake_get_auth_keystone(os_options)
        url, token = c.get_auth('http://www.test.com', 'asdf', 'asdf',
                                os_options=os_options,
                                auth_version="2.0")
        self.assertTrue(url.startswith("http"))
        self.assertTrue(token)

    def test_auth_v2_no_endpoint(self):
        os_options = {'region_name': 'unknown_region',
                      'tenant_name': 'asdf'}
        c.get_auth_keystone = fake_get_auth_keystone(
            os_options, c.ClientException)
        self.assertRaises(c.ClientException, c.get_auth,
                          'http://www.tests.com', 'asdf', 'asdf',
                          os_options=os_options, auth_version='2.0')

    def test_auth_v2_ks_exception(self):
        c.get_auth_keystone = fake_get_auth_keystone(
            {}, c.ClientException)
        self.assertRaises(c.ClientException, c.get_auth,
                          'http://www.tests.com', 'asdf', 'asdf',
                          os_options={},
                          auth_version='2.0')

    def test_auth_v2_cacert(self):
        os_options = {'tenant_name': 'foo'}
        c.get_auth_keystone = fake_get_auth_keystone(
            os_options, None)

        auth_url_secure = 'https://www.tests.com'
        auth_url_insecure = 'https://www.tests.com/self-signed-certificate'

        url, token = c.get_auth(auth_url_secure, 'asdf', 'asdf',
                                os_options=os_options, auth_version='2.0',
                                insecure=False)
        self.assertTrue(url.startswith("http"))
        self.assertTrue(token)

        url, token = c.get_auth(auth_url_insecure, 'asdf', 'asdf',
                                os_options=os_options, auth_version='2.0',
                                cacert='ca.pem', insecure=False)
        self.assertTrue(url.startswith("http"))
        self.assertTrue(token)

        self.assertRaises(c.ClientException, c.get_auth,
                          auth_url_insecure, 'asdf', 'asdf',
                          os_options=os_options, auth_version='2.0')
        self.assertRaises(c.ClientException, c.get_auth,
                          auth_url_insecure, 'asdf', 'asdf',
                          os_options=os_options, auth_version='2.0',
                          insecure=False)

    def test_auth_v2_insecure(self):
        os_options = {'tenant_name': 'foo'}
        c.get_auth_keystone = fake_get_auth_keystone(
            os_options, None)

        auth_url_secure = 'https://www.tests.com'
        auth_url_insecure = 'https://www.tests.com/invalid-certificate'

        url, token = c.get_auth(auth_url_secure, 'asdf', 'asdf',
                                os_options=os_options, auth_version='2.0')
        self.assertTrue(url.startswith("http"))
        self.assertTrue(token)

        url, token = c.get_auth(auth_url_insecure, 'asdf', 'asdf',
                                os_options=os_options, auth_version='2.0',
                                insecure=True)
        self.assertTrue(url.startswith("http"))
        self.assertTrue(token)

        self.assertRaises(c.ClientException, c.get_auth,
                          auth_url_insecure, 'asdf', 'asdf',
                          os_options=os_options, auth_version='2.0')
        self.assertRaises(c.ClientException, c.get_auth,
                          auth_url_insecure, 'asdf', 'asdf',
                          os_options=os_options, auth_version='2.0',
                          insecure=False)

    def test_auth_v3_with_tenant_name(self):
        # check the correct auth version is passed to get_auth_keystone
        os_options = {'tenant_name': 'asdf'}
        req_args = {'auth_version': '3'}
        c.get_auth_keystone = fake_get_auth_keystone(os_options,
                                                     required_kwargs=req_args)
        url, token = c.get_auth('http://www.test.com', 'asdf', 'asdf',
                                os_options=os_options,
                                auth_version="3")
        self.assertTrue(url.startswith("http"))
        self.assertTrue(token)

    def test_get_keystone_client_2_0(self):
        # check the correct auth version is passed to get_auth_keystone
        os_options = {'tenant_name': 'asdf'}
        req_args = {'auth_version': '2.0'}
        c.get_auth_keystone = fake_get_auth_keystone(os_options,
                                                     required_kwargs=req_args)
        url, token = c.get_keystoneclient_2_0('http://www.test.com', 'asdf',
                                              'asdf', os_options=os_options)
        self.assertTrue(url.startswith("http"))
        self.assertTrue(token)


class TestGetAccount(MockHttpTest):

    def test_no_content(self):
        c.http_connection = self.fake_http_connection(204)
        value = c.get_account('http://www.test.com', 'asdf')[1]
        self.assertEqual(value, [])

    def test_param_marker(self):
        c.http_connection = self.fake_http_connection(
            204,
            query_string="format=json&marker=marker")
        c.get_account('http://www.test.com', 'asdf', marker='marker')

    def test_param_limit(self):
        c.http_connection = self.fake_http_connection(
            204,
            query_string="format=json&limit=10")
        c.get_account('http://www.test.com', 'asdf', limit=10)

    def test_param_prefix(self):
        c.http_connection = self.fake_http_connection(
            204,
            query_string="format=json&prefix=asdf/")
        c.get_account('http://www.test.com', 'asdf', prefix='asdf/')

    def test_param_end_marker(self):
        c.http_connection = self.fake_http_connection(
            204,
            query_string="format=json&end_marker=end_marker")
        c.get_account('http://www.test.com', 'asdf', end_marker='end_marker')


class TestHeadAccount(MockHttpTest):

    def test_ok(self):
        c.http_connection = self.fake_http_connection(200, headers={
            'x-account-meta-color': 'blue',
        })
        resp_headers = c.head_account('http://www.tests.com', 'asdf')
        self.assertEqual(resp_headers['x-account-meta-color'], 'blue')
        self.assertRequests([
            ('HEAD', 'http://www.tests.com', '', {'x-auth-token': 'asdf'})
        ])

    def test_server_error(self):
        body = 'c' * 65
        c.http_connection = self.fake_http_connection(500, body=body)
        e = self.assertRaises(c.ClientException, c.head_account,
                              'http://www.tests.com', 'asdf')
        self.assertEqual(e.http_response_content, body)
        self.assertEqual(e.http_status, 500)
        self.assertRequests([
            ('HEAD', 'http://www.tests.com', '', {'x-auth-token': 'asdf'})
        ])
        # TODO: this is a fairly brittle test of the __repr__ on the
        # ClientException which should probably be in a targeted test
        new_body = "[first 60 chars of response] " + body[0:60]
        self.assertEqual(e.__str__()[-89:], new_body)


class TestGetContainer(MockHttpTest):

    def test_no_content(self):
        c.http_connection = self.fake_http_connection(204)
        value = c.get_container('http://www.test.com', 'asdf', 'asdf')[1]
        self.assertEqual(value, [])

    def test_param_marker(self):
        c.http_connection = self.fake_http_connection(
            204,
            query_string="format=json&marker=marker")
        c.get_container('http://www.test.com', 'asdf', 'asdf', marker='marker')

    def test_param_limit(self):
        c.http_connection = self.fake_http_connection(
            204,
            query_string="format=json&limit=10")
        c.get_container('http://www.test.com', 'asdf', 'asdf', limit=10)

    def test_param_prefix(self):
        c.http_connection = self.fake_http_connection(
            204,
            query_string="format=json&prefix=asdf/")
        c.get_container('http://www.test.com', 'asdf', 'asdf', prefix='asdf/')

    def test_param_delimiter(self):
        c.http_connection = self.fake_http_connection(
            204,
            query_string="format=json&delimiter=/")
        c.get_container('http://www.test.com', 'asdf', 'asdf', delimiter='/')

    def test_param_end_marker(self):
        c.http_connection = self.fake_http_connection(
            204,
            query_string="format=json&end_marker=end_marker")
        c.get_container('http://www.test.com', 'asdf', 'asdf',
                        end_marker='end_marker')

    def test_param_path(self):
        c.http_connection = self.fake_http_connection(
            204,
            query_string="format=json&path=asdf")
        c.get_container('http://www.test.com', 'asdf', 'asdf',
                        path='asdf')

    def test_request_headers(self):
        c.http_connection = self.fake_http_connection(
            204, query_string="format=json")
        conn = c.http_connection('http://www.test.com')
        headers = {'x-client-key': 'client key'}
        c.get_container('url_is_irrelevant', 'TOKEN', 'container',
                        http_conn=conn, headers=headers)
        self.assertRequests([
            ('GET', '/container?format=json', '', {
                'x-auth-token': 'TOKEN',
                'x-client-key': 'client key',
            }),
        ])


class TestHeadContainer(MockHttpTest):

    def test_head_ok(self):
        fake_conn = self.fake_http_connection(
            200, headers={'x-container-meta-color': 'blue'})
        with mock.patch('swiftclient.client.http_connection',
                        new=fake_conn):
            resp = c.head_container('https://example.com/v1/AUTH_test',
                                    'token', 'container')
        self.assertEqual(resp['x-container-meta-color'], 'blue')
        self.assertRequests([
            ('HEAD', 'https://example.com/v1/AUTH_test/container', '',
             {'x-auth-token': 'token'}),
        ])

    def test_server_error(self):
        body = 'c' * 60
        c.http_connection = self.fake_http_connection(500, body=body)
        e = self.assertRaises(c.ClientException, c.head_container,
                              'http://www.test.com', 'asdf', 'container')
        self.assertRequests([
            ('HEAD', '/container', '', {'x-auth-token': 'asdf'}),
        ])
        self.assertEqual(e.http_status, 500)
        self.assertEqual(e.http_response_content, body)


class TestPutContainer(MockHttpTest):

    def test_ok(self):
        c.http_connection = self.fake_http_connection(200)
        value = c.put_container('http://www.test.com', 'asdf', 'asdf')
        self.assertEqual(value, None)

    def test_server_error(self):
        body = 'c' * 60
        c.http_connection = self.fake_http_connection(500, body=body)
        e = self.assertRaises(c.ClientException, c.put_container,
                              'http://www.test.com', 'token', 'container')
        self.assertEqual(e.http_response_content, body)
        self.assertRequests([
            ('PUT', '/container', '', {'x-auth-token': 'token'}),
        ])


class TestDeleteContainer(MockHttpTest):

    def test_ok(self):
        c.http_connection = self.fake_http_connection(200)
        value = c.delete_container('http://www.test.com', 'asdf', 'asdf')
        self.assertEqual(value, None)


class TestGetObject(MockHttpTest):

    def test_server_error(self):
        c.http_connection = self.fake_http_connection(500)
        self.assertRaises(c.ClientException, c.get_object,
                          'http://www.test.com', 'asdf', 'asdf', 'asdf')

    def test_query_string(self):
        c.http_connection = self.fake_http_connection(200,
                                                      query_string="hello=20")
        c.get_object('http://www.test.com', 'asdf', 'asdf', 'asdf',
                     query_string="hello=20")
        for req in self.iter_request_log():
            self.assertEqual(req['method'], 'GET')
            self.assertEqual(req['parsed_path'].path, '/asdf/asdf')
            self.assertEqual(req['parsed_path'].query, 'hello=20')
            self.assertEqual(req['body'], '')
            self.assertEqual(req['headers']['x-auth-token'], 'asdf')

    def test_request_headers(self):
        c.http_connection = self.fake_http_connection(200)
        conn = c.http_connection('http://www.test.com')
        headers = {'Range': 'bytes=1-2'}
        c.get_object('url_is_irrelevant', 'TOKEN', 'container', 'object',
                     http_conn=conn, headers=headers)
        self.assertRequests([
            ('GET', '/container/object', '', {
                'x-auth-token': 'TOKEN',
                'range': 'bytes=1-2',
            }),
        ])

    def test_chunk_size_read_method(self):
        conn = c.Connection('http://auth.url/', 'some_user', 'some_key')
        with mock.patch('swiftclient.client.get_auth_1_0') as mock_get_auth:
            mock_get_auth.return_value = ('http://auth.url/', 'tToken')
            c.http_connection = self.fake_http_connection(200, body='abcde')
            __, resp = conn.get_object('asdf', 'asdf', resp_chunk_size=3)
            self.assertTrue(hasattr(resp, 'read'))
            self.assertEqual(resp.read(3), 'abc')
            self.assertEqual(resp.read(None), 'de')
            self.assertEqual(resp.read(), '')

    def test_chunk_size_iter(self):
        conn = c.Connection('http://auth.url/', 'some_user', 'some_key')
        with mock.patch('swiftclient.client.get_auth_1_0') as mock_get_auth:
            mock_get_auth.return_value = ('http://auth.url/', 'tToken')
            c.http_connection = self.fake_http_connection(200, body='abcde')
            __, resp = conn.get_object('asdf', 'asdf', resp_chunk_size=3)
            self.assertTrue(hasattr(resp, 'next'))
            self.assertEqual(next(resp), 'abc')
            self.assertEqual(next(resp), 'de')
            self.assertRaises(StopIteration, next, resp)

    def test_chunk_size_read_and_iter(self):
        conn = c.Connection('http://auth.url/', 'some_user', 'some_key')
        with mock.patch('swiftclient.client.get_auth_1_0') as mock_get_auth:
            mock_get_auth.return_value = ('http://auth.url/', 'tToken')
            c.http_connection = self.fake_http_connection(200, body='abcdef')
            __, resp = conn.get_object('asdf', 'asdf', resp_chunk_size=2)
            self.assertTrue(hasattr(resp, 'read'))
            self.assertEqual(resp.read(3), 'abc')
            self.assertEqual(next(resp), 'de')
            self.assertEqual(resp.read(), 'f')
            self.assertRaises(StopIteration, next, resp)
            self.assertEqual(resp.read(), '')


class TestHeadObject(MockHttpTest):

    def test_server_error(self):
        c.http_connection = self.fake_http_connection(500)
        self.assertRaises(c.ClientException, c.head_object,
                          'http://www.test.com', 'asdf', 'asdf', 'asdf')

    def test_request_headers(self):
        c.http_connection = self.fake_http_connection(204)
        conn = c.http_connection('http://www.test.com')
        headers = {'x-client-key': 'client key'}
        c.head_object('url_is_irrelevant', 'TOKEN', 'container',
                      'asdf', http_conn=conn, headers=headers)
        self.assertRequests([
            ('HEAD', '/container/asdf', '', {
                'x-auth-token': 'TOKEN',
                'x-client-key': 'client key',
            }),
        ])


class TestPutObject(MockHttpTest):

    def test_ok(self):
        c.http_connection = self.fake_http_connection(200)
        args = ('http://www.test.com', 'asdf', 'asdf', 'asdf', 'asdf')
        value = c.put_object(*args)
        self.assertTrue(isinstance(value, six.string_types))

    def test_unicode_ok(self):
        conn = c.http_connection(u'http://www.test.com/')
        mock_file = six.StringIO(u'\u5929\u7a7a\u4e2d\u7684\u4e4c\u4e91')
        args = (u'\u5929\u7a7a\u4e2d\u7684\u4e4c\u4e91',
                u'\u5929\u7a7a\u4e2d\u7684\u4e4c\u4e91',
                u'\u5929\u7a7a\u4e2d\u7684\u4e4c\u4e91',
                u'\u5929\u7a7a\u4e2d\u7684\u4e4c\u4e91',
                mock_file)
        text = u'\u5929\u7a7a\u4e2d\u7684\u4e4c\u4e91'
        headers = {'X-Header1': text,
                   'X-2': 1, 'X-3': {'a': 'b'}, 'a-b': '.x:yz mn:fg:lp'}

        resp = MockHttpResponse()
        conn[1].getresponse = resp.fake_response
        conn[1]._request = resp._fake_request
        value = c.put_object(*args, headers=headers, http_conn=conn)
        self.assertTrue(isinstance(value, six.string_types))
        # Test for RFC-2616 encoded symbols
        self.assertIn(("a-b", b".x:yz mn:fg:lp"),
                      resp.buffer)
        # Test unicode header
        self.assertIn(('x-header1', text.encode('utf8')),
                      resp.buffer)

    def test_chunk_warning(self):
        conn = c.http_connection('http://www.test.com/')
        mock_file = six.StringIO('asdf')
        args = ('asdf', 'asdf', 'asdf', 'asdf', mock_file)
        resp = MockHttpResponse()
        conn[1].getresponse = resp.fake_response
        conn[1]._request = resp._fake_request
        with warnings.catch_warnings(record=True) as w:
            c.put_object(*args, chunk_size=20, headers={}, http_conn=conn)
            self.assertEqual(len(w), 0)

        body = 'c' * 60
        c.http_connection = self.fake_http_connection(200, body=body)
        args = ('http://www.test.com', 'asdf', 'asdf', 'asdf', 'asdf')
        with warnings.catch_warnings(record=True) as w:
            c.put_object(*args, chunk_size=20)
            self.assertEqual(len(w), 1)
            self.assertTrue(issubclass(w[-1].category, UserWarning))

    def test_server_error(self):
        body = 'c' * 60
        c.http_connection = self.fake_http_connection(500, body=body)
        args = ('http://www.test.com', 'asdf', 'asdf', 'asdf', 'asdf')
        e = self.assertRaises(c.ClientException, c.put_object, *args)
        self.assertEqual(e.http_response_content, body)
        self.assertEqual(e.http_status, 500)
        self.assertRequests([
            ('PUT', '/asdf/asdf', 'asdf', {'x-auth-token': 'asdf'}),
        ])

    def test_query_string(self):
        c.http_connection = self.fake_http_connection(200,
                                                      query_string="hello=20")
        c.put_object('http://www.test.com', 'asdf', 'asdf', 'asdf',
                     query_string="hello=20")
        for req in self.iter_request_log():
            self.assertEqual(req['method'], 'PUT')
            self.assertEqual(req['parsed_path'].path, '/asdf/asdf')
            self.assertEqual(req['parsed_path'].query, 'hello=20')
            self.assertEqual(req['headers']['x-auth-token'], 'asdf')

    def test_raw_upload(self):
        # Raw upload happens when content_length is passed to put_object
        conn = c.http_connection(u'http://www.test.com/')
        resp = MockHttpResponse(status=200)
        conn[1].getresponse = resp.fake_response
        conn[1]._request = resp._fake_request
        raw_data = b'asdf' * 256
        raw_data_len = len(raw_data)

        for kwarg in ({'headers': {'Content-Length': str(raw_data_len)}},
                      {'content_length': raw_data_len}):
            with tempfile.TemporaryFile() as mock_file:
                mock_file.write(raw_data)
                mock_file.seek(0)

                c.put_object(url='http://www.test.com', http_conn=conn,
                             contents=mock_file, **kwarg)

                req_data = resp.requests_params['data']
                self.assertTrue(isinstance(req_data,
                                           swiftclient.utils.LengthWrapper))
                self.assertEqual(raw_data_len, len(req_data.read()))

    def test_chunk_upload(self):
        # Chunked upload happens when no content_length is passed to put_object
        conn = c.http_connection(u'http://www.test.com/')
        resp = MockHttpResponse(status=200)
        conn[1].getresponse = resp.fake_response
        conn[1]._request = resp._fake_request
        raw_data = b'asdf' * 256
        chunk_size = 16

        with tempfile.TemporaryFile() as mock_file:
            mock_file.write(raw_data)
            mock_file.seek(0)

            c.put_object(url='http://www.test.com', http_conn=conn,
                         contents=mock_file, chunk_size=chunk_size)
            req_data = resp.requests_params['data']
            self.assertTrue(hasattr(req_data, '__iter__'))
            data = b''
            for chunk in req_data:
                self.assertEqual(chunk_size, len(chunk))
                data += chunk
            self.assertEqual(data, raw_data)

    def test_md5_mismatch(self):
        conn = c.http_connection('http://www.test.com')
        resp = MockHttpResponse(status=200, verify=True,
                                headers={'etag': '"badresponseetag"'})
        conn[1].getresponse = resp.fake_response
        conn[1]._request = resp._fake_request
        raw_data = b'asdf' * 256
        raw_data_md5 = md5(raw_data).hexdigest()
        chunk_size = 16

        with tempfile.TemporaryFile() as mock_file:
            mock_file.write(raw_data)
            mock_file.seek(0)

            contents = swiftclient.utils.ReadableToIterable(mock_file,
                                                            md5=True)

            etag = c.put_object(url='http://www.test.com',
                                http_conn=conn,
                                contents=contents,
                                chunk_size=chunk_size)

            self.assertNotEqual(etag, contents.get_md5sum())
            self.assertEqual(etag, 'badresponseetag')
            self.assertEqual(raw_data_md5, contents.get_md5sum())

    def test_md5_match(self):
        conn = c.http_connection('http://www.test.com')
        raw_data = b'asdf' * 256
        raw_data_md5 = md5(raw_data).hexdigest()
        resp = MockHttpResponse(status=200, verify=True,
                                headers={'etag': '"' + raw_data_md5 + '"'})
        conn[1].getresponse = resp.fake_response
        conn[1]._request = resp._fake_request
        chunk_size = 16

        with tempfile.TemporaryFile() as mock_file:
            mock_file.write(raw_data)
            mock_file.seek(0)
            contents = swiftclient.utils.ReadableToIterable(mock_file,
                                                            md5=True)

            etag = c.put_object(url='http://www.test.com',
                                http_conn=conn,
                                contents=contents,
                                chunk_size=chunk_size)

            self.assertEqual(raw_data_md5, contents.get_md5sum())
            self.assertEqual(etag, contents.get_md5sum())

    def test_params(self):
        conn = c.http_connection(u'http://www.test.com/')
        resp = MockHttpResponse(status=200)
        conn[1].getresponse = resp.fake_response
        conn[1]._request = resp._fake_request

        c.put_object(url='http://www.test.com', http_conn=conn,
                     etag='1234-5678', content_type='text/plain')
        request_header = resp.requests_params['headers']
        self.assertEqual(request_header['etag'], b'1234-5678')
        self.assertEqual(request_header['content-type'], b'text/plain')

    def test_no_content_type(self):
        conn = c.http_connection(u'http://www.test.com/')
        resp = MockHttpResponse(status=200)
        conn[1].getresponse = resp.fake_response
        conn[1]._request = resp._fake_request

        c.put_object(url='http://www.test.com', http_conn=conn)
        request_header = resp.requests_params['headers']
        self.assertEqual(request_header['content-type'], b'')


class TestPostObject(MockHttpTest):

    def test_ok(self):
        c.http_connection = self.fake_http_connection(200)
        args = ('http://www.test.com', 'asdf', 'asdf', 'asdf', {})
        c.post_object(*args)

    def test_unicode_ok(self):
        conn = c.http_connection(u'http://www.test.com/')
        args = (u'\u5929\u7a7a\u4e2d\u7684\u4e4c\u4e91',
                u'\u5929\u7a7a\u4e2d\u7684\u4e4c\u4e91',
                u'\u5929\u7a7a\u4e2d\u7684\u4e4c\u4e91',
                u'\u5929\u7a7a\u4e2d\u7684\u4e4c\u4e91')
        text = u'\u5929\u7a7a\u4e2d\u7684\u4e4c\u4e91'
        headers = {'X-Header1': text,
                   b'X-Header2': 'value',
                   'X-2': '1', 'X-3': {'a': 'b'}, 'a-b': '.x:yz mn:kl:qr',
                   'X-Object-Meta-Header-not-encoded': text,
                   b'X-Object-Meta-Header-encoded': 'value'}

        resp = MockHttpResponse()
        conn[1].getresponse = resp.fake_response
        conn[1]._request = resp._fake_request
        c.post_object(*args, headers=headers, http_conn=conn)
        # Test for RFC-2616 encoded symbols
        self.assertIn(('a-b', b".x:yz mn:kl:qr"), resp.buffer)
        # Test unicode header
        self.assertIn(('x-header1', text.encode('utf8')),
                      resp.buffer)
        self.assertIn((b'x-object-meta-header-not-encoded',
                      text.encode('utf8')), resp.buffer)
        self.assertIn((b'x-object-meta-header-encoded', b'value'),
                      resp.buffer)
        self.assertIn((b'x-header2', b'value'), resp.buffer)

    def test_server_error(self):
        body = 'c' * 60
        c.http_connection = self.fake_http_connection(500, body=body)
        args = ('http://www.test.com', 'token', 'container', 'obj', {})
        e = self.assertRaises(c.ClientException, c.post_object, *args)
        self.assertEqual(e.http_response_content, body)
        self.assertRequests([
            ('POST', 'http://www.test.com/container/obj', '', {
                'x-auth-token': 'token',
            }),
        ])


class TestDeleteObject(MockHttpTest):

    def test_ok(self):
        c.http_connection = self.fake_http_connection(200)
        c.delete_object('http://www.test.com', 'asdf', 'asdf', 'asdf')

    def test_server_error(self):
        c.http_connection = self.fake_http_connection(500)
        self.assertRaises(c.ClientException, c.delete_object,
                          'http://www.test.com', 'asdf', 'asdf', 'asdf')

    def test_query_string(self):
        c.http_connection = self.fake_http_connection(200,
                                                      query_string="hello=20")
        c.delete_object('http://www.test.com', 'asdf', 'asdf', 'asdf',
                        query_string="hello=20")


class TestGetCapabilities(MockHttpTest):

    def test_ok(self):
        conn = self.fake_http_connection(200, body=b'{}')
        http_conn = conn('http://www.test.com/info')
        info = c.get_capabilities(http_conn)
        self.assertRequests([
            ('GET', '/info'),
        ])
        self.assertEqual(info, {})
        self.assertTrue(http_conn[1].resp.has_been_read)

    def test_server_error(self):
        conn = self.fake_http_connection(500)
        http_conn = conn('http://www.test.com/info')
        self.assertRaises(c.ClientException, c.get_capabilities, http_conn)

    def test_conn_get_capabilities_with_auth(self):
        auth_headers = {
            'x-auth-token': 'token',
            'x-storage-url': 'http://storage.example.com/v1/AUTH_test'
        }
        auth_v1_response = StubResponse(headers=auth_headers)
        stub_info = {'swift': {'fake': True}}
        info_response = StubResponse(body=b'{"swift":{"fake":true}}')
        fake_conn = self.fake_http_connection(auth_v1_response, info_response)

        conn = c.Connection('http://auth.example.com/auth/v1.0',
                            'user', 'key')
        with mock.patch('swiftclient.client.http_connection',
                        new=fake_conn):
            info = conn.get_capabilities()
        self.assertEqual(info, stub_info)
        self.assertRequests([
            ('GET', '/auth/v1.0'),
            ('GET', 'http://storage.example.com/info'),
        ])

    def test_conn_get_capabilities_with_os_auth(self):
        fake_keystone = fake_get_auth_keystone(
            storage_url='http://storage.example.com/v1/AUTH_test')
        stub_info = {'swift': {'fake': True}}
        info_response = StubResponse(body=b'{"swift":{"fake":true}}')
        fake_conn = self.fake_http_connection(info_response)

        os_options = {'project_id': 'test'}
        conn = c.Connection('http://keystone.example.com/v3.0',
                            'user', 'key', os_options=os_options,
                            auth_version=3)
        with mock.patch.multiple('swiftclient.client',
                                 get_auth_keystone=fake_keystone,
                                 http_connection=fake_conn):
            info = conn.get_capabilities()
        self.assertEqual(info, stub_info)
        self.assertRequests([
            ('GET', 'http://storage.example.com/info'),
        ])

    def test_conn_get_capabilities_with_url_param(self):
        stub_info = {'swift': {'fake': True}}
        info_response = StubResponse(body=b'{"swift":{"fake":true}}')
        fake_conn = self.fake_http_connection(info_response)

        conn = c.Connection('http://auth.example.com/auth/v1.0',
                            'user', 'key')
        with mock.patch('swiftclient.client.http_connection',
                        new=fake_conn):
            info = conn.get_capabilities(
                'http://other-storage.example.com/info')
        self.assertEqual(info, stub_info)
        self.assertRequests([
            ('GET', 'http://other-storage.example.com/info'),
        ])

    def test_conn_get_capabilities_with_preauthurl_param(self):
        stub_info = {'swift': {'fake': True}}
        info_response = StubResponse(body=b'{"swift":{"fake":true}}')
        fake_conn = self.fake_http_connection(info_response)

        storage_url = 'http://storage.example.com/v1/AUTH_test'
        conn = c.Connection('http://auth.example.com/auth/v1.0',
                            'user', 'key', preauthurl=storage_url)
        with mock.patch('swiftclient.client.http_connection',
                        new=fake_conn):
            info = conn.get_capabilities()
        self.assertEqual(info, stub_info)
        self.assertRequests([
            ('GET', 'http://storage.example.com/info'),
        ])

    def test_conn_get_capabilities_with_os_options(self):
        stub_info = {'swift': {'fake': True}}
        info_response = StubResponse(body=b'{"swift":{"fake":true}}')
        fake_conn = self.fake_http_connection(info_response)

        storage_url = 'http://storage.example.com/v1/AUTH_test'
        os_options = {
            'project_id': 'test',
            'object_storage_url': storage_url,
        }
        conn = c.Connection('http://keystone.example.com/v3.0',
                            'user', 'key', os_options=os_options,
                            auth_version=3)
        with mock.patch('swiftclient.client.http_connection',
                        new=fake_conn):
            info = conn.get_capabilities()
        self.assertEqual(info, stub_info)
        self.assertRequests([
            ('GET', 'http://storage.example.com/info'),
        ])


class TestHTTPConnection(MockHttpTest):

    def test_bad_url_scheme(self):
        url = u'www.test.com'
        exc = self.assertRaises(c.ClientException, c.http_connection, url)
        expected = u'Unsupported scheme "" in url "www.test.com"'
        self.assertEqual(expected, str(exc))

        url = u'://www.test.com'
        exc = self.assertRaises(c.ClientException, c.http_connection, url)
        expected = u'Unsupported scheme "" in url "://www.test.com"'
        self.assertEqual(expected, str(exc))

        url = u'blah://www.test.com'
        exc = self.assertRaises(c.ClientException, c.http_connection, url)
        expected = u'Unsupported scheme "blah" in url "blah://www.test.com"'
        self.assertEqual(expected, str(exc))

    def test_ok_url_scheme(self):
        for scheme in ('http', 'https', 'HTTP', 'HTTPS'):
            url = u'%s://www.test.com' % scheme
            parsed_url, conn = c.http_connection(url)
            self.assertEqual(scheme.lower(), parsed_url.scheme)
            self.assertEqual(u'%s://www.test.com' % scheme, conn.url)

    def test_ok_proxy(self):
        conn = c.http_connection(u'http://www.test.com/',
                                 proxy='http://localhost:8080')
        self.assertEqual(conn[1].requests_args['proxies']['http'],
                         'http://localhost:8080')

    def test_bad_proxy(self):
        try:
            c.http_connection(u'http://www.test.com/', proxy='localhost:8080')
        except c.ClientException as e:
            self.assertEqual(e.msg, "Proxy's missing scheme")

    def test_cacert(self):
        conn = c.http_connection(u'http://www.test.com/',
                                 cacert='/dev/urandom')
        self.assertEqual(conn[1].requests_args['verify'], '/dev/urandom')

    def test_insecure(self):
        conn = c.http_connection(u'http://www.test.com/', insecure=True)
        self.assertEqual(conn[1].requests_args['verify'], False)

    def test_response_connection_released(self):
        _parsed_url, conn = c.http_connection(u'http://www.test.com/')
        conn.resp = MockHttpResponse()
        conn.resp.raw = mock.Mock()
        conn.resp.raw.read.side_effect = ["Chunk", ""]
        resp = conn.getresponse()
        self.assertFalse(resp.closed)
        self.assertEqual("Chunk", resp.read())
        self.assertFalse(resp.read())
        self.assertTrue(resp.closed)


class TestConnection(MockHttpTest):

    def test_instance(self):
        conn = c.Connection('http://www.test.com', 'asdf', 'asdf')
        self.assertEqual(conn.retries, 5)

    def test_instance_kwargs(self):
        args = {'user': 'ausername',
                'key': 'secretpass',
                'authurl': 'http://www.test.com',
                'tenant_name': 'atenant'}
        conn = c.Connection(**args)
        self.assertEqual(type(conn), c.Connection)

    def test_instance_kwargs_token(self):
        args = {'preauthtoken': 'atoken123',
                'preauthurl': 'http://www.test.com:8080/v1/AUTH_123456'}
        conn = c.Connection(**args)
        self.assertEqual(conn.url, args['preauthurl'])
        self.assertEqual(conn.token, args['preauthtoken'])

    def test_instance_kwargs_os_token(self):
        storage_url = 'http://storage.example.com/v1/AUTH_test'
        token = 'token'
        args = {
            'os_options': {
                'object_storage_url': storage_url,
                'auth_token': token,
            }
        }
        conn = c.Connection(**args)
        self.assertEqual(conn.url, storage_url)
        self.assertEqual(conn.token, token)

    def test_instance_kwargs_token_precedence(self):
        storage_url = 'http://storage.example.com/v1/AUTH_test'
        token = 'token'
        args = {
            'preauthurl': storage_url,
            'preauthtoken': token,
            'os_options': {
                'auth_token': 'less-specific-token',
                'object_storage_url': 'less-specific-storage-url',
            }
        }
        conn = c.Connection(**args)
        self.assertEqual(conn.url, storage_url)
        self.assertEqual(conn.token, token)

    def test_storage_url_override(self):
        static_url = 'http://overridden.storage.url'
        conn = c.Connection('http://auth.url/', 'some_user', 'some_key',
                            os_options={
                                'object_storage_url': static_url})
        method_signatures = (
            (conn.head_account, []),
            (conn.get_account, []),
            (conn.head_container, ('asdf',)),
            (conn.get_container, ('asdf',)),
            (conn.put_container, ('asdf',)),
            (conn.delete_container, ('asdf',)),
            (conn.head_object, ('asdf', 'asdf')),
            (conn.get_object, ('asdf', 'asdf')),
            (conn.put_object, ('asdf', 'asdf', 'asdf')),
            (conn.post_object, ('asdf', 'asdf', {})),
            (conn.delete_object, ('asdf', 'asdf')),
        )

        with mock.patch('swiftclient.client.get_auth_1_0') as mock_get_auth:
            mock_get_auth.return_value = ('http://auth.storage.url', 'tToken')

            for method, args in method_signatures:
                c.http_connection = self.fake_http_connection(
                    200, body=b'[]', storage_url=static_url)
                method(*args)
                self.assertEqual(len(self.request_log), 1)
                for request in self.iter_request_log():
                    self.assertEqual(request['parsed_path'].netloc,
                                     'overridden.storage.url')
                    self.assertEqual(request['headers']['x-auth-token'],
                                     'tToken')

    def test_get_capabilities(self):
        conn = c.Connection()
        with mock.patch('swiftclient.client.get_capabilities') as get_cap:
            conn.get_capabilities('http://storage2.test.com')
            parsed = get_cap.call_args[0][0][0]
            self.assertEqual(parsed.path, '/info')
            self.assertEqual(parsed.netloc, 'storage2.test.com')
            conn.get_auth = lambda: ('http://storage.test.com/v1/AUTH_test',
                                     'token')
            conn.get_capabilities()
            parsed = get_cap.call_args[0][0][0]
            self.assertEqual(parsed.path, '/info')
            self.assertEqual(parsed.netloc, 'storage.test.com')

    def test_retry(self):
        def quick_sleep(*args):
            pass
        c.sleep = quick_sleep
        conn = c.Connection('http://www.test.com', 'asdf', 'asdf')
        code_iter = [500] * (conn.retries + 1)
        c.http_connection = self.fake_http_connection(*code_iter)

        self.assertRaises(c.ClientException, conn.head_account)
        self.assertEqual(conn.attempts, conn.retries + 1)

    def test_retry_on_ratelimit(self):

        def quick_sleep(*args):
            pass
        c.sleep = quick_sleep

        # test retries
        conn = c.Connection('http://www.test.com/auth/v1.0', 'asdf', 'asdf',
                            retry_on_ratelimit=True)
        code_iter = [200] + [498] * (conn.retries + 1)
        auth_resp_headers = {
            'x-auth-token': 'asdf',
            'x-storage-url': 'http://storage/v1/test',
        }
        c.http_connection = self.fake_http_connection(
            *code_iter, headers=auth_resp_headers)
        e = self.assertRaises(c.ClientException, conn.head_account)
        self.assertIn('Account HEAD failed', str(e))
        self.assertEqual(conn.attempts, conn.retries + 1)

        # test default no-retry
        c.http_connection = self.fake_http_connection(
            200, 498,
            headers=auth_resp_headers)
        conn = c.Connection('http://www.test.com/auth/v1.0', 'asdf', 'asdf')
        e = self.assertRaises(c.ClientException, conn.head_account)
        self.assertIn('Account HEAD failed', str(e))
        self.assertEqual(conn.attempts, 1)

    def test_resp_read_on_server_error(self):
        conn = c.Connection('http://www.test.com', 'asdf', 'asdf', retries=0)

        def get_auth(*args, **kwargs):
            return 'http://www.new.com', 'new'
        conn.get_auth = get_auth
        self.url, self.token = conn.get_auth()

        method_signatures = (
            (conn.head_account, []),
            (conn.get_account, []),
            (conn.head_container, ('asdf',)),
            (conn.get_container, ('asdf',)),
            (conn.put_container, ('asdf',)),
            (conn.delete_container, ('asdf',)),
            (conn.head_object, ('asdf', 'asdf')),
            (conn.get_object, ('asdf', 'asdf')),
            (conn.put_object, ('asdf', 'asdf', 'asdf')),
            (conn.post_object, ('asdf', 'asdf', {})),
            (conn.delete_object, ('asdf', 'asdf')),
        )

        for method, args in method_signatures:
            c.http_connection = self.fake_http_connection(500)
            self.assertRaises(c.ClientException, method, *args)
            requests = list(self.iter_request_log())
            self.assertEqual(len(requests), 1)
            for req in requests:
                msg = '%s did not read resp on server error' % method.__name__
                self.assertTrue(req['resp'].has_been_read, msg)

    def test_reauth(self):
        c.http_connection = self.fake_http_connection(401, 200)

        def get_auth(*args, **kwargs):
            # this mock, and by extension this test are not
            # represenative of the unit under test.  The real get_auth
            # method will always return the os_option dict's
            # object_storage_url which will be overridden by the
            # preauthurl parameter to Connection if it is provided.
            return 'http://www.new.com', 'new'

        def swap_sleep(*args):
            self.swap_sleep_called = True
            c.get_auth = get_auth
        c.sleep = swap_sleep
        self.swap_sleep_called = False

        conn = c.Connection('http://www.test.com', 'asdf', 'asdf',
                            preauthurl='http://www.old.com',
                            preauthtoken='old',
                            )

        self.assertEqual(conn.attempts, 0)
        self.assertEqual(conn.url, 'http://www.old.com')
        self.assertEqual(conn.token, 'old')

        conn.head_account()

        self.assertTrue(self.swap_sleep_called)
        self.assertEqual(conn.attempts, 2)
        self.assertEqual(conn.url, 'http://www.new.com')
        self.assertEqual(conn.token, 'new')

    def test_reauth_preauth(self):
        conn = c.Connection(
            'http://auth.example.com', 'user', 'password',
            preauthurl='http://storage.example.com/v1/AUTH_test',
            preauthtoken='expired')
        auth_v1_response = StubResponse(200, headers={
            'x-auth-token': 'token',
            'x-storage-url': 'http://storage.example.com/v1/AUTH_user',
        })
        fake_conn = self.fake_http_connection(401, auth_v1_response, 200)
        with mock.patch.multiple('swiftclient.client',
                                 http_connection=fake_conn,
                                 sleep=mock.DEFAULT):
            conn.head_account()
        self.assertRequests([
            ('HEAD', '/v1/AUTH_test', '', {'x-auth-token': 'expired'}),
            ('GET', 'http://auth.example.com', '', {
                'x-auth-user': 'user',
                'x-auth-key': 'password'}),
            ('HEAD', '/v1/AUTH_test', '', {'x-auth-token': 'token'}),
        ])

    def test_reauth_os_preauth(self):
        os_preauth_options = {
            'tenant_name': 'demo',
            'object_storage_url': 'http://storage.example.com/v1/AUTH_test',
            'auth_token': 'expired',
        }
        conn = c.Connection('http://auth.example.com', 'user', 'password',
                            os_options=os_preauth_options, auth_version=2)
        fake_keystone = fake_get_auth_keystone(os_preauth_options)
        fake_conn = self.fake_http_connection(401, 200)
        with mock.patch.multiple('swiftclient.client',
                                 get_auth_keystone=fake_keystone,
                                 http_connection=fake_conn,
                                 sleep=mock.DEFAULT):
            conn.head_account()
        self.assertRequests([
            ('HEAD', '/v1/AUTH_test', '', {'x-auth-token': 'expired'}),
            ('HEAD', '/v1/AUTH_test', '', {'x-auth-token': 'token'}),
        ])

    def test_preauth_token_with_no_storage_url_requires_auth(self):
        conn = c.Connection(
            'http://auth.example.com', 'user', 'password',
            preauthtoken='expired')
        auth_v1_response = StubResponse(200, headers={
            'x-auth-token': 'token',
            'x-storage-url': 'http://storage.example.com/v1/AUTH_user',
        })
        fake_conn = self.fake_http_connection(auth_v1_response, 200)
        with mock.patch.multiple('swiftclient.client',
                                 http_connection=fake_conn,
                                 sleep=mock.DEFAULT):
            conn.head_account()
        self.assertRequests([
            ('GET', 'http://auth.example.com', '', {
                'x-auth-user': 'user',
                'x-auth-key': 'password'}),
            ('HEAD', '/v1/AUTH_user', '', {'x-auth-token': 'token'}),
        ])

    def test_os_preauth_token_with_no_storage_url_requires_auth(self):
        os_preauth_options = {
            'tenant_name': 'demo',
            'auth_token': 'expired',
        }
        conn = c.Connection('http://auth.example.com', 'user', 'password',
                            os_options=os_preauth_options, auth_version=2)
        storage_url = 'http://storage.example.com/v1/AUTH_user'
        fake_keystone = fake_get_auth_keystone(storage_url=storage_url)
        fake_conn = self.fake_http_connection(200)
        with mock.patch.multiple('swiftclient.client',
                                 get_auth_keystone=fake_keystone,
                                 http_connection=fake_conn,
                                 sleep=mock.DEFAULT):
            conn.head_account()
        self.assertRequests([
            ('HEAD', '/v1/AUTH_user', '', {'x-auth-token': 'token'}),
        ])

    def test_preauth_url_trumps_auth_url(self):
        storage_url = 'http://storage.example.com/v1/AUTH_pre_url'
        conn = c.Connection(
            'http://auth.example.com', 'user', 'password',
            preauthurl=storage_url)
        auth_v1_response = StubResponse(200, headers={
            'x-auth-token': 'post_token',
            'x-storage-url': 'http://storage.example.com/v1/AUTH_post_url',
        })
        fake_conn = self.fake_http_connection(auth_v1_response, 200)
        with mock.patch.multiple('swiftclient.client',
                                 http_connection=fake_conn,
                                 sleep=mock.DEFAULT):
            conn.head_account()
        self.assertRequests([
            ('GET', 'http://auth.example.com', '', {
                'x-auth-user': 'user',
                'x-auth-key': 'password'}),
            ('HEAD', '/v1/AUTH_pre_url', '', {'x-auth-token': 'post_token'}),
        ])

    def test_os_preauth_url_trumps_auth_url(self):
        storage_url = 'http://storage.example.com/v1/AUTH_pre_url'
        os_preauth_options = {
            'tenant_name': 'demo',
            'object_storage_url': storage_url,
        }
        conn = c.Connection('http://auth.example.com', 'user', 'password',
                            os_options=os_preauth_options, auth_version=2)
        fake_keystone = fake_get_auth_keystone(
            storage_url='http://storage.example.com/v1/AUTH_post_url',
            token='post_token')
        fake_conn = self.fake_http_connection(200)
        with mock.patch.multiple('swiftclient.client',
                                 get_auth_keystone=fake_keystone,
                                 http_connection=fake_conn,
                                 sleep=mock.DEFAULT):
            conn.head_account()
        self.assertRequests([
            ('HEAD', '/v1/AUTH_pre_url', '', {'x-auth-token': 'post_token'}),
        ])

    def test_preauth_url_trumps_os_preauth_url(self):
        storage_url = 'http://storage.example.com/v1/AUTH_pre_url'
        os_storage_url = 'http://storage.example.com/v1/AUTH_os_pre_url'
        os_preauth_options = {
            'tenant_name': 'demo',
            'object_storage_url': os_storage_url,
        }
        orig_os_preauth_options = dict(os_preauth_options)
        conn = c.Connection('http://auth.example.com', 'user', 'password',
                            os_options=os_preauth_options, auth_version=2,
                            preauthurl=storage_url, tenant_name='not_demo')
        fake_keystone = fake_get_auth_keystone(
            storage_url='http://storage.example.com/v1/AUTH_post_url',
            token='post_token')
        fake_conn = self.fake_http_connection(200)
        with mock.patch.multiple('swiftclient.client',
                                 get_auth_keystone=fake_keystone,
                                 http_connection=fake_conn,
                                 sleep=mock.DEFAULT):
            conn.head_account()
        self.assertRequests([
            ('HEAD', '/v1/AUTH_pre_url', '', {'x-auth-token': 'post_token'}),
        ])

        # check that Connection has not modified our os_options
        self.assertEqual(orig_os_preauth_options, os_preauth_options)

    def test_get_auth_sets_url_and_token(self):
        with mock.patch('swiftclient.client.get_auth') as mock_get_auth:
            mock_get_auth.return_value = (
                "https://storage.url/v1/AUTH_storage_acct", "AUTH_token"
            )
            conn = c.Connection("https://auth.url/auth/v2.0",
                                "user", "passkey", tenant_name="tenant")
            conn.get_auth()
        self.assertEqual("https://storage.url/v1/AUTH_storage_acct", conn.url)
        self.assertEqual("AUTH_token", conn.token)

    def test_timeout_passed_down(self):
        # We want to avoid mocking http_connection(), and most especially
        # avoid passing it down in argument. However, we cannot simply
        # instantiate C=Connection(), then shim C.http_conn. Doing so would
        # avoid some of the code under test (where _retry() invokes
        # http_connection()), and would miss get_auth() completely.
        # So, with regret, we do mock http_connection(), but with a very
        # light shim that swaps out _request() as originally intended.

        orig_http_connection = c.http_connection

        timeouts = []

        def my_request_handler(*a, **kw):
            if 'timeout' in kw:
                timeouts.append(kw['timeout'])
            else:
                timeouts.append(None)
            return MockHttpResponse(
                status=200,
                headers={
                    'x-auth-token': 'a_token',
                    'x-storage-url': 'http://files.example.com/v1/AUTH_user'})

        def shim_connection(*a, **kw):
            url, conn = orig_http_connection(*a, **kw)
            conn._request = my_request_handler
            return url, conn

        # v1 auth
        conn = c.Connection(
            'http://auth.example.com', 'user', 'password', timeout=33.0)
        with mock.patch.multiple('swiftclient.client',
                                 http_connection=shim_connection,
                                 sleep=mock.DEFAULT):
            conn.head_account()

        # 1 call is through get_auth, 1 call is HEAD for account
        self.assertEqual(timeouts, [33.0, 33.0])

        # v2 auth
        timeouts = []
        conn = c.Connection(
            'http://auth.example.com', 'user', 'password', timeout=33.0,
            os_options=dict(tenant_name='tenant'), auth_version=2.0)
        fake_ks = FakeKeystone(endpoint='http://some_url', token='secret')
        with mock.patch('swiftclient.client._import_keystone_client',
                        _make_fake_import_keystone_client(fake_ks)):
            with mock.patch.multiple('swiftclient.client',
                                     http_connection=shim_connection,
                                     sleep=mock.DEFAULT):
                conn.head_account()

        # check timeout is passed to keystone client
        self.assertEqual(1, len(fake_ks.calls))
        self.assertTrue('timeout' in fake_ks.calls[0])
        self.assertEqual(33.0, fake_ks.calls[0].get('timeout'))
        # check timeout passed to HEAD for account
        self.assertEqual(timeouts, [33.0])

    def test_reset_stream(self):

        class LocalContents(object):

            def __init__(self, tell_value=0):
                self.already_read = False
                self.seeks = []
                self.tell_value = tell_value

            def tell(self):
                return self.tell_value

            def seek(self, position):
                self.seeks.append(position)
                self.already_read = False

            def read(self, size=-1):
                if self.already_read:
                    return ''
                else:
                    self.already_read = True
                    return 'abcdef'

        class LocalConnection(object):

            def __init__(self, parsed_url=None):
                self.reason = ""
                if parsed_url:
                    self.host = parsed_url.netloc
                    self.port = parsed_url.netloc

            def putrequest(self, *args, **kwargs):
                self.send()

            def putheader(self, *args, **kwargs):
                return

            def endheaders(self, *args, **kwargs):
                return

            def send(self, *args, **kwargs):
                raise socket.error('oops')

            def request(self, *args, **kwargs):
                return

            def getresponse(self, *args, **kwargs):
                self.status = 200
                return self

            def getheader(self, *args, **kwargs):
                return 'header'

            def getheaders(self):
                return {"key1": "value1", "key2": "value2"}

            def read(self, *args, **kwargs):
                return ''

        def local_http_connection(url, proxy=None, cacert=None,
                                  insecure=False, ssl_compression=True,
                                  timeout=None):
            parsed = urlparse(url)
            return parsed, LocalConnection()

        orig_conn = c.http_connection
        try:
            c.http_connection = local_http_connection
            conn = c.Connection('http://www.example.com', 'asdf', 'asdf',
                                retries=1, starting_backoff=.0001)

            contents = LocalContents()
            exc = None
            try:
                conn.put_object('c', 'o', contents)
            except socket.error as err:
                exc = err
            self.assertEqual(contents.seeks, [0])
            self.assertEqual(str(exc), 'oops')

            contents = LocalContents(tell_value=123)
            exc = None
            try:
                conn.put_object('c', 'o', contents)
            except socket.error as err:
                exc = err
            self.assertEqual(contents.seeks, [123])
            self.assertEqual(str(exc), 'oops')

            contents = LocalContents()
            contents.tell = None
            exc = None
            try:
                conn.put_object('c', 'o', contents)
            except c.ClientException as err:
                exc = err
            self.assertEqual(contents.seeks, [])
            self.assertEqual(str(exc), "put_object('c', 'o', ...) failure "
                             "and no ability to reset contents for reupload.")
        finally:
            c.http_connection = orig_conn


class TestResponseDict(MockHttpTest):
    """
    Verify handling of optional response_dict argument.
    """
    calls = [('post_container', 'c', {}),
             ('put_container', 'c'),
             ('delete_container', 'c'),
             ('post_object', 'c', 'o', {}),
             ('put_object', 'c', 'o', 'body'),
             ('delete_object', 'c', 'o')]

    def fake_get_auth(*args, **kwargs):
        return 'http://url', 'token'

    def test_response_dict_with_auth_error(self):
        def bad_get_auth(*args, **kwargs):
            raise c.ClientException('test')

        for call in self.calls:
            resp_dict = {'test': 'should be untouched'}
            with mock.patch('swiftclient.client.get_auth',
                            bad_get_auth):
                conn = c.Connection('http://127.0.0.1:8080', 'user', 'key')
                self.assertRaises(c.ClientException, getattr(conn, call[0]),
                                  *call[1:], response_dict=resp_dict)

            self.assertEqual({'test': 'should be untouched'}, resp_dict)

    def test_response_dict_with_request_error(self):
        for call in self.calls:
            resp_dict = {'test': 'should be untouched'}
            with mock.patch('swiftclient.client.get_auth',
                            self.fake_get_auth):
                exc = c.ClientException('test')
                with mock.patch('swiftclient.client.http_connection',
                                self.fake_http_connection(200, exc=exc)):
                    conn = c.Connection('http://127.0.0.1:8080', 'user', 'key')
                    self.assertRaises(c.ClientException,
                                      getattr(conn, call[0]),
                                      *call[1:],
                                      response_dict=resp_dict)

            self.assertTrue('test' in resp_dict)
            self.assertEqual('should be untouched', resp_dict['test'])
            self.assertTrue('response_dicts' in resp_dict)
            self.assertEqual([{}], resp_dict['response_dicts'])

    def test_response_dict(self):
        # test response_dict is populated and
        # new list of response_dicts is created
        for call in self.calls:
            resp_dict = {'test': 'should be untouched'}
            with mock.patch('swiftclient.client.get_auth',
                            self.fake_get_auth):
                with mock.patch('swiftclient.client.http_connection',
                                self.fake_http_connection(200)):
                    conn = c.Connection('http://127.0.0.1:8080', 'user', 'key')
                    getattr(conn, call[0])(*call[1:], response_dict=resp_dict)

            for key in ('test', 'status', 'headers', 'reason',
                        'response_dicts'):
                self.assertTrue(key in resp_dict)
            self.assertEqual('should be untouched', resp_dict.pop('test'))
            self.assertEqual('Fake', resp_dict['reason'])
            self.assertEqual(200, resp_dict['status'])
            self.assertTrue('x-works' in resp_dict['headers'])
            self.assertEqual('yes', resp_dict['headers']['x-works'])
            children = resp_dict.pop('response_dicts')
            self.assertEqual(1, len(children))
            self.assertEqual(resp_dict, children[0])

    def test_response_dict_with_existing(self):
        # check response_dict is populated and new dict is appended
        # to existing response_dicts list
        for call in self.calls:
            resp_dict = {'test': 'should be untouched',
                         'response_dicts': [{'existing': 'response dict'}]}
            with mock.patch('swiftclient.client.get_auth',
                            self.fake_get_auth):
                with mock.patch('swiftclient.client.http_connection',
                                self.fake_http_connection(200)):
                    conn = c.Connection('http://127.0.0.1:8080', 'user', 'key')
                    getattr(conn, call[0])(*call[1:], response_dict=resp_dict)

            for key in ('test', 'status', 'headers', 'reason',
                        'response_dicts'):
                self.assertTrue(key in resp_dict)
            self.assertEqual('should be untouched', resp_dict.pop('test'))
            self.assertEqual('Fake', resp_dict['reason'])
            self.assertEqual(200, resp_dict['status'])
            self.assertTrue('x-works' in resp_dict['headers'])
            self.assertEqual('yes', resp_dict['headers']['x-works'])
            children = resp_dict.pop('response_dicts')
            self.assertEqual(2, len(children))
            self.assertEqual({'existing': 'response dict'}, children[0])
            self.assertEqual(resp_dict, children[1])


class TestLogging(MockHttpTest):
    """
    Make sure all the lines in http_log are covered.
    """

    def setUp(self):
        super(TestLogging, self).setUp()
        self.swiftclient_logger = logging.getLogger("swiftclient")
        self.log_level = self.swiftclient_logger.getEffectiveLevel()
        self.swiftclient_logger.setLevel(logging.INFO)

    def tearDown(self):
        self.swiftclient_logger.setLevel(self.log_level)
        super(TestLogging, self).tearDown()

    def test_put_ok(self):
        c.http_connection = self.fake_http_connection(200)
        args = ('http://www.test.com', 'asdf', 'asdf', 'asdf', 'asdf')
        value = c.put_object(*args)
        self.assertTrue(isinstance(value, six.string_types))

    def test_head_error(self):
        c.http_connection = self.fake_http_connection(500)
        self.assertRaises(c.ClientException, c.head_object,
                          'http://www.test.com', 'asdf', 'asdf', 'asdf')

    def test_get_error(self):
        c.http_connection = self.fake_http_connection(404)
        e = self.assertRaises(c.ClientException, c.get_object,
                              'http://www.test.com', 'asdf', 'asdf', 'asdf')
        self.assertEqual(e.http_status, 404)


class TestCloseConnection(MockHttpTest):

    def test_close_none(self):
        c.http_connection = self.fake_http_connection()
        conn = c.Connection('http://www.test.com', 'asdf', 'asdf')
        self.assertEqual(conn.http_conn, None)
        conn.close()
        self.assertEqual(conn.http_conn, None)

    def test_close_ok(self):
        url = 'http://www.test.com'
        conn = c.Connection(url, 'asdf', 'asdf')
        self.assertEqual(conn.http_conn, None)
        conn.http_conn = c.http_connection(url)
        self.assertEqual(type(conn.http_conn), tuple)
        self.assertEqual(len(conn.http_conn), 2)
        http_conn_obj = conn.http_conn[1]
        self.assertIsInstance(http_conn_obj, c.HTTPConnection)
        self.assertFalse(hasattr(http_conn_obj, 'close'))
        conn.close()


class TestServiceToken(MockHttpTest):

    def setUp(self):
        super(TestServiceToken, self).setUp()
        self.os_options = {
            'object_storage_url': 'http://storage_url.com',
            'service_username': 'service_username',
            'service_project_name': 'service_project_name',
            'service_key': 'service_key'}

    def get_connection(self):
        conn = c.Connection('http://www.test.com', 'asdf', 'asdf',
                            os_options=self.os_options)

        self.assertTrue(isinstance(conn, c.Connection))
        conn.get_auth = self.get_auth
        conn.get_service_auth = self.get_service_auth

        self.assertEqual(conn.attempts, 0)
        self.assertEqual(conn.service_token, None)

        self.assertTrue(isinstance(conn, c.Connection))
        return conn

    def get_auth(self):
        # The real get_auth function will always return the os_option
        # dict's object_storage_url which will be overridden by the
        # preauthurl paramater to Connection if it is provided.
        return self.os_options.get('object_storage_url'), 'token'

    def get_service_auth(self):
        # The real get_auth function will always return the os_option
        # dict's object_storage_url which will be overridden by the
        # preauthurl parameter to Connection if it is provided.
        return self.os_options.get('object_storage_url'), 'stoken'

    def test_service_token_reauth(self):
        get_auth_call_list = []

        def get_auth(url, user, key, **kwargs):
            # The real get_auth function will always return the os_option
            # dict's object_storage_url which will be overridden by the
            # preauthurl parameter to Connection if it is provided.
            args = {'url': url, 'user': user, 'key': key, 'kwargs': kwargs}
            get_auth_call_list.append(args)
            return_dict = {'asdf': 'new', 'service_username': 'newserv'}
            storage_url = kwargs['os_options'].get('object_storage_url')
            return storage_url, return_dict[user]

        def swap_sleep(*args):
            self.swap_sleep_called = True
            c.get_auth = get_auth

        with mock.patch('swiftclient.client.http_connection',
                        self.fake_http_connection(401, 200)):
            with mock.patch('swiftclient.client.sleep', swap_sleep):
                self.swap_sleep_called = False

                conn = c.Connection('http://www.test.com', 'asdf', 'asdf',
                                    preauthurl='http://www.old.com',
                                    preauthtoken='old',
                                    os_options=self.os_options)

                self.assertEqual(conn.attempts, 0)
                self.assertEqual(conn.url, 'http://www.old.com')
                self.assertEqual(conn.token, 'old')

                conn.head_account()

        self.assertTrue(self.swap_sleep_called)
        self.assertEqual(conn.attempts, 2)
        # The original 'preauth' storage URL *must* be preserved
        self.assertEqual(conn.url, 'http://www.old.com')
        self.assertEqual(conn.token, 'new')
        self.assertEqual(conn.service_token, 'newserv')

        # Check get_auth was called with expected args
        auth_args = get_auth_call_list[0]
        auth_kwargs = get_auth_call_list[0]['kwargs']
        self.assertEqual('asdf', auth_args['user'])
        self.assertEqual('asdf', auth_args['key'])
        self.assertEqual('service_key',
                         auth_kwargs['os_options']['service_key'])
        self.assertEqual('service_username',
                         auth_kwargs['os_options']['service_username'])
        self.assertEqual('service_project_name',
                         auth_kwargs['os_options']['service_project_name'])

        auth_args = get_auth_call_list[1]
        auth_kwargs = get_auth_call_list[1]['kwargs']
        self.assertEqual('service_username', auth_args['user'])
        self.assertEqual('service_key', auth_args['key'])
        self.assertEqual('service_project_name',
                         auth_kwargs['os_options']['tenant_name'])

    def test_service_token_get_account(self):
        with mock.patch('swiftclient.client.http_connection',
                        self.fake_http_connection(200)):
            with mock.patch('swiftclient.client.parse_api_response'):
                conn = self.get_connection()
                conn.get_account()
        self.assertEqual(1, len(self.request_log), self.request_log)
        for actual in self.iter_request_log():
            self.assertEqual('GET', actual['method'])
            actual_hdrs = actual['headers']
            self.assertTrue('X-Service-Token' in actual_hdrs)
            self.assertEqual('stoken', actual_hdrs['X-Service-Token'])
            self.assertEqual('token', actual_hdrs['X-Auth-Token'])
            self.assertEqual('http://storage_url.com/?format=json',
                             actual['full_path'])
        self.assertEqual(conn.attempts, 1)

    def test_service_token_head_account(self):
        with mock.patch('swiftclient.client.http_connection',
                        self.fake_http_connection(200)):
            conn = self.get_connection()
            conn.head_account()
        self.assertEqual(1, len(self.request_log), self.request_log)
        for actual in self.iter_request_log():
            self.assertEqual('HEAD', actual['method'])
            actual_hdrs = actual['headers']
            self.assertTrue('X-Service-Token' in actual_hdrs)
            self.assertEqual('stoken', actual_hdrs['X-Service-Token'])
            self.assertEqual('token', actual_hdrs['X-Auth-Token'])
            self.assertEqual('http://storage_url.com', actual['full_path'])

        self.assertEqual(conn.attempts, 1)

    def test_service_token_post_account(self):
        with mock.patch('swiftclient.client.http_connection',
                        self.fake_http_connection(201)):
            conn = self.get_connection()
            conn.post_account(headers={})
        self.assertEqual(1, len(self.request_log), self.request_log)
        for actual in self.iter_request_log():
            self.assertEqual('POST', actual['method'])
            actual_hdrs = actual['headers']
            self.assertTrue('X-Service-Token' in actual_hdrs)
            self.assertEqual('stoken', actual_hdrs['X-Service-Token'])
            self.assertEqual('token', actual_hdrs['X-Auth-Token'])
            self.assertEqual('http://storage_url.com', actual['full_path'])
        self.assertEqual(conn.attempts, 1)

    def test_service_token_delete_container(self):
        with mock.patch('swiftclient.client.http_connection',
                        self.fake_http_connection(204)):
            conn = self.get_connection()
            conn.delete_container('container1')
        self.assertEqual(1, len(self.request_log), self.request_log)
        for actual in self.iter_request_log():
            self.assertEqual('DELETE', actual['method'])
            actual_hdrs = actual['headers']
            self.assertTrue('X-Service-Token' in actual_hdrs)
            self.assertEqual('stoken', actual_hdrs['X-Service-Token'])
            self.assertEqual('token', actual_hdrs['X-Auth-Token'])
            self.assertEqual('http://storage_url.com/container1',
                             actual['full_path'])
        self.assertEqual(conn.attempts, 1)

    def test_service_token_get_container(self):
        with mock.patch('swiftclient.client.http_connection',
                        self.fake_http_connection(200)):
            with mock.patch('swiftclient.client.parse_api_response'):
                conn = self.get_connection()
                conn.get_container('container1')
        self.assertEqual(1, len(self.request_log), self.request_log)
        for actual in self.iter_request_log():
            self.assertEqual('GET', actual['method'])
            actual_hdrs = actual['headers']
            self.assertTrue('X-Service-Token' in actual_hdrs)
            self.assertEqual('stoken', actual_hdrs['X-Service-Token'])
            self.assertEqual('token', actual_hdrs['X-Auth-Token'])
            self.assertEqual('http://storage_url.com/container1?format=json',
                             actual['full_path'])
        self.assertEqual(conn.attempts, 1)

    def test_service_token_head_container(self):
        with mock.patch('swiftclient.client.http_connection',
                        self.fake_http_connection(200)):
            conn = self.get_connection()
            conn.head_container('container1')
        self.assertEqual(1, len(self.request_log), self.request_log)
        for actual in self.iter_request_log():
            self.assertEqual('HEAD', actual['method'])
            actual_hdrs = actual['headers']
            self.assertTrue('X-Service-Token' in actual_hdrs)
            self.assertEqual('stoken', actual_hdrs['X-Service-Token'])
            self.assertEqual('token', actual_hdrs['X-Auth-Token'])
            self.assertEqual('http://storage_url.com/container1',
                             actual['full_path'])
        self.assertEqual(conn.attempts, 1)

    def test_service_token_post_container(self):
        with mock.patch('swiftclient.client.http_connection',
                        self.fake_http_connection(201)):
            conn = self.get_connection()
            conn.post_container('container1', {})
        self.assertEqual(1, len(self.request_log), self.request_log)
        for actual in self.iter_request_log():
            self.assertEqual('POST', actual['method'])
            actual_hdrs = actual['headers']
            self.assertTrue('X-Service-Token' in actual_hdrs)
            self.assertEqual('stoken', actual_hdrs['X-Service-Token'])
            self.assertEqual('token', actual_hdrs['X-Auth-Token'])
            self.assertEqual('http://storage_url.com/container1',
                             actual['full_path'])
        self.assertEqual(conn.attempts, 1)

    def test_service_token_put_container(self):
        with mock.patch('swiftclient.client.http_connection',
                        self.fake_http_connection(200)):
            conn = self.get_connection()
            conn.put_container('container1')
        self.assertEqual(1, len(self.request_log), self.request_log)
        for actual in self.iter_request_log():
            self.assertEqual('PUT', actual['method'])
            actual_hdrs = actual['headers']
            self.assertTrue('X-Service-Token' in actual_hdrs)
            self.assertEqual('stoken', actual_hdrs['X-Service-Token'])
            self.assertEqual('token', actual_hdrs['X-Auth-Token'])
            self.assertEqual('http://storage_url.com/container1',
                             actual['full_path'])
        self.assertEqual(conn.attempts, 1)

    def test_service_token_get_object(self):
        with mock.patch('swiftclient.client.http_connection',
                        self.fake_http_connection(200)):
            conn = self.get_connection()
            conn.get_object('container1', 'obj1')
        self.assertEqual(1, len(self.request_log), self.request_log)
        for actual in self.iter_request_log():
            self.assertEqual('GET', actual['method'])
            actual_hdrs = actual['headers']
            self.assertTrue('X-Service-Token' in actual_hdrs)
            self.assertEqual('stoken', actual_hdrs['X-Service-Token'])
            self.assertEqual('token', actual_hdrs['X-Auth-Token'])
            self.assertEqual('http://storage_url.com/container1/obj1',
                             actual['full_path'])
        self.assertEqual(conn.attempts, 1)

    def test_service_token_head_object(self):
        with mock.patch('swiftclient.client.http_connection',
                        self.fake_http_connection(200)):
            conn = self.get_connection()
            conn.head_object('container1', 'obj1')
        self.assertEqual(1, len(self.request_log), self.request_log)
        for actual in self.iter_request_log():
            self.assertEqual('HEAD', actual['method'])
            actual_hdrs = actual['headers']
            self.assertTrue('X-Service-Token' in actual_hdrs)
            self.assertEqual('stoken', actual_hdrs['X-Service-Token'])
            self.assertEqual('token', actual_hdrs['X-Auth-Token'])
            self.assertEqual('http://storage_url.com/container1/obj1',
                             actual['full_path'])
        self.assertEqual(conn.attempts, 1)

    def test_service_token_put_object(self):
        with mock.patch('swiftclient.client.http_connection',
                        self.fake_http_connection(200)):
            conn = self.get_connection()
            conn.put_object('container1', 'obj1', 'a_string')
        self.assertEqual(1, len(self.request_log), self.request_log)
        for actual in self.iter_request_log():
            self.assertEqual('PUT', actual['method'])
            actual_hdrs = actual['headers']
            self.assertTrue('X-Service-Token' in actual_hdrs)
            self.assertEqual('stoken', actual_hdrs['X-Service-Token'])
            self.assertEqual('token', actual_hdrs['X-Auth-Token'])
            self.assertEqual('http://storage_url.com/container1/obj1',
                             actual['full_path'])
        self.assertEqual(conn.attempts, 1)

    def test_service_token_post_object(self):
        with mock.patch('swiftclient.client.http_connection',
                        self.fake_http_connection(202)):
            conn = self.get_connection()
            conn.post_object('container1', 'obj1', {})
        self.assertEqual(1, len(self.request_log), self.request_log)
        for actual in self.iter_request_log():
            self.assertEqual('POST', actual['method'])
            actual_hdrs = actual['headers']
            self.assertTrue('X-Service-Token' in actual_hdrs)
            self.assertEqual('stoken', actual_hdrs['X-Service-Token'])
            self.assertEqual('token', actual_hdrs['X-Auth-Token'])
            self.assertEqual('http://storage_url.com/container1/obj1',
                             actual['full_path'])
        self.assertEqual(conn.attempts, 1)

    def test_service_token_delete_object(self):
        with mock.patch('swiftclient.client.http_connection',
                        self.fake_http_connection(202)):
            conn = self.get_connection()
            conn.delete_object('container1', 'obj1', 'a_string')
        self.assertEqual(1, len(self.request_log), self.request_log)
        for actual in self.iter_request_log():
            self.assertEqual('DELETE', actual['method'])
            actual_hdrs = actual['headers']
            self.assertTrue('X-Service-Token' in actual_hdrs)
            self.assertEqual('stoken', actual_hdrs['X-Service-Token'])
            self.assertEqual('token', actual_hdrs['X-Auth-Token'])
            self.assertEqual('http://storage_url.com/container1/obj1?a_string',
                             actual['full_path'])
        self.assertEqual(conn.attempts, 1)
