#!/usr/bin/env python
# Licensed to Cloudera, Inc. under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  Cloudera, Inc. licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
from collections import OrderedDict
from unittest.mock import Mock, patch

import pytest
from django.test.client import Client
from django.urls import reverse

import notebook.conf
import notebook.connectors.hiveserver2
from azure.conf import is_adls_enabled
from desktop import appmanager
from desktop.conf import APP_BLACKLIST, ENABLE_CONNECTORS, ENABLE_PROMETHEUS
from desktop.lib.django_test_util import make_logged_in_client
from desktop.lib.test_utils import add_permission, grant_access
from desktop.metrics import num_of_queries
from desktop.models import Directory, Document, Document2
from hadoop import cluster as originalCluster
from notebook.api import _historify
from notebook.conf import ENABLE_ALL_INTERPRETERS, INTERPRETERS, INTERPRETERS_SHOWN_ON_WHEEL, get_ordered_interpreters
from notebook.connectors.base import Api, Notebook, QueryError, QueryExpired
from notebook.decorators import api_error_handler
from useradmin.models import User


@pytest.mark.django_db
class TestApi(object):
  def setup_method(self):
    self.client = make_logged_in_client(username="test", groupname="default", recreate=True, is_superuser=False)
    self.client_not_me = make_logged_in_client(username="not_perm_user", groupname="default", recreate=True, is_superuser=False)

    self.user = User.objects.get(username="test")
    self.user_not_me = User.objects.get(username="not_perm_user")

    self.notebook_json = (
      """
      {
        "selectedSnippet": "hive",
        "showHistory": false,
        "description": "Test Hive Query",
        "name": "Test Hive Query",
        "sessions": [
            {
                "type": "hive",
                "properties": [],
                "id": null
            }
        ],
        "type": "query-hive",
        "id": 50010,
        "snippets": [{"id":"2b7d1f46-17a0-30af-efeb-33d4c29b1055","type":"hive","status":"running","statement_raw":"""
      """"select * from default.web_logs where app = '${app_name}';","variables":[{"name":"app_name","value":"metastore"}],"""
      """"statement":"select * from default.web_logs where app = 'metastore';","properties":{"settings":[],"files":[],"""
      """"functions":[]},"result":{"id":"b424befa-f4f5-8799-a0b4-79753f2552b1","type":"table","handle":{"log_context":null,"""
      """"statements_count":1,"end":{"column":21,"row":0},"statement_id":0,"has_more_statements":false,"""
      """"start":{"column":0,"row":0},"secret":"rVRWw7YPRGqPT7LZ/TeFaA==an","has_result_set":true,"statement":"""
      """"select * from default.web_logs where app = 'metastore';","operation_type":0,"modified_row_count":null,"""
      """"guid":"7xm6+epkRx6dyvYvGNYePA==an"}},"lastExecuted": 1462554843817,"database":"default"}],
        "uuid": "5982a274-de78-083c-2efc-74f53dce744c",
        "isSaved": false,
        "parentUuid": null
    }
    """
    )

    self.notebook = json.loads(self.notebook_json)
    self.doc2 = Document2.objects.create(id=50010, name=self.notebook['name'], type=self.notebook['type'], owner=self.user)
    self.doc1 = Document.objects.link(
      self.doc2, owner=self.user, name=self.doc2.name, description=self.doc2.description, extra=self.doc2.type
    )

  def test_save_notebook(self):
    # Test that saving a new document with a new parent will set the parent_directory
    home_dir = Document2.objects.get_home_directory(self.user)
    assert home_dir.uuid == self.doc2.parent_directory.uuid

    new_dir = Directory.objects.create(name='new_dir', owner=self.user, parent_directory=home_dir)
    notebook_cp = self.notebook.copy()
    notebook_cp.pop('id')
    notebook_cp['directoryUuid'] = new_dir.uuid
    notebook_json = json.dumps(notebook_cp)

    response = self.client.post(reverse('notebook:save_notebook'), {'notebook': notebook_json})
    data = json.loads(response.content)

    assert 0 == data['status'], data
    doc = Document2.objects.get(pk=data['id'])
    assert new_dir.uuid == doc.parent_directory.uuid

    # Test that saving a new document with a no parent will map it to its home dir
    notebook_json = (
      """
      {
        "selectedSnippet": "hive",
        "showHistory": false,
        "description": "Test Hive Query",
        "name": "Test Hive Query",
        "sessions": [
            {
                "type": "hive",
                "properties": [],
                "id": null
            }
        ],
        "type": "query-hive",
        "id": null,
        "snippets": [{"id":"2b7d1f46-17a0-30af-efeb-33d4c29b1055","type":"hive","status":"running","statement_raw":"""
      """"select * from default.web_logs where app = '${app_name}';","variables":"""
      """[{"name":"app_name","value":"metastore"}],"statement":"""
      """"select * from default.web_logs where app = 'metastore';","properties":{"settings":[],"files":[],"functions":[]},"""
      """"result":{"id":"b424befa-f4f5-8799-a0b4-79753f2552b1","type":"table","handle":{"log_context":null,"""
      """"statements_count":1,"end":{"column":21,"row":0},"statement_id":0,"has_more_statements":false,"""
      """"start":{"column":0,"row":0},"secret":"rVRWw7YPRGqPT7LZ/TeFaA==an","has_result_set":true,"""
      """"statement":"select * from default.web_logs where app = 'metastore';","operation_type":0,"""
      """"modified_row_count":null,"guid":"7xm6+epkRx6dyvYvGNYePA==an"}},"lastExecuted": 1462554843817,"database":"default"}],
        "uuid": "d9efdee1-ef25-4d43-b8f9-1a170f69a05a"
    }
    """
    )

    response = self.client.post(reverse('notebook:save_notebook'), {'notebook': notebook_json})
    data = json.loads(response.content)

    assert 0 == data['status'], data
    doc = Document2.objects.get(pk=data['id'])
    assert Document2.objects.get_home_directory(self.user).uuid == doc.parent_directory.uuid

    # Test that saving a notebook will save the search field to the first statement text
    assert doc.search == "select * from default.web_logs where app = 'metastore';"
    assert doc.type == "query-hive"

  def test_type_when_saving_an_actual_notebook(self):
    notebook_json = (
      """
      {
        "selectedSnippet": "hive",
        "showHistory": false,
        "description": "Test Notebook",
        "name": "Test Notebook",
        "sessions": [
            {
                "type": "hive",
                "properties": [],
                "id": null
            }
        ],
        "type": "notebook",
        "id": null,
        "snippets": [{"id":"2b7d1f46-17a0-30af-efeb-33d4c29b1055","type":"hive","status":"running","statement_raw":"""
      """"select * from default.web_logs where app = '${app_name}';","variables":"""
      """[{"name":"app_name","value":"metastore"}],"statement":"""
      """"select 1;","properties":{"settings":[],"files":[],"functions":[]},"""
      """"result":{"id":"b424befa-f4f5-8799-a0b4-79753f2552b1","type":"table","handle":{"log_context":null,"""
      """"statements_count":1,"end":{"column":21,"row":0},"statement_id":0,"has_more_statements":false,"""
      """"start":{"column":0,"row":0},"secret":"rVRWw7YPRGqPT7LZ/TeFaA==an","has_result_set":true,"""
      """"statement":"select * from default.web_logs where app = 'metastore';","operation_type":0,"""
      """"modified_row_count":null,"guid":"7xm6"}},"lastExecuted": 1462554843817,"database":"default"}],
                      "uuid": "d9efdee1-ef25-4d43-b8f9-1a170f69a05a"
                  }
                  """
    )

    response = self.client.post(reverse('notebook:save_notebook'), {'notebook': notebook_json})
    data = json.loads(response.content)

    assert 0 == data['status'], data
    assert 'notebook' == data['type'], data
    doc = Document2.objects.get(pk=data['id'])

    assert doc.type == "notebook"

  def test_save_notebook_with_connector_off(self):
    reset = ENABLE_CONNECTORS.set_for_testing(False)

    notebook_cp = self.notebook.copy()
    notebook_cp.pop('id')
    notebook_cp['snippets'][0]['connector'] = {
      'name': 'MySql',  # At some point even v1 should set those two
      'dialect': 'mysql',
      'optimizer': 'api',
    }
    notebook_json = json.dumps(notebook_cp)

    try:
      response = self.client.post(reverse('notebook:save_notebook'), {'notebook': notebook_json})
      data = json.loads(response.content)
    finally:
      reset()

    assert 0 == data['status'], data
    doc = Document2.objects.get(pk=data['id'])
    assert 'query-mysql' == doc.type

  def test_save_notebook_with_connector_on(self):
    if not ENABLE_CONNECTORS.get():
      pytest.skip("Skipping Test")

    notebook_cp = self.notebook.copy()
    notebook_cp.pop('id')

    connector = Connector.objects.create(name='MySql', dialect='mysql')

    notebook_cp['snippets'][0]['connector'] = {
      'name': 'MySql',
      'dialect': 'mysql',
      'type': str(connector.id),
      'optimizer': 'api',
    }

    try:
      response = self.client.post(reverse('notebook:save_notebook'), {'notebook': notebook_json})
      data = json.loads(response.content)
    finally:
      connector.delete()

    assert 0 == data['status'], data
    doc = Document2.objects.get(pk=data['id'])
    assert 'query-mysql' == doc.type

  def test_historify(self):
    # Starts with no history
    assert 0 == Document2.objects.filter(name__contains=self.notebook['name'], is_history=True).count()
    assert 1 == Document.objects.filter(name__contains=self.notebook['name']).count()

    history_doc = _historify(self.notebook, self.user)

    assert history_doc.id > 0

    # Test that historify creates new Doc2 and linked Doc1
    assert 1 == Document2.objects.filter(name__contains=self.notebook['name'], is_history=True).count()
    assert 2 == Document.objects.filter(name__contains=self.notebook['name']).count()

    # Historify again
    history_doc = _historify(self.notebook, self.user)

    assert 2 == Document2.objects.filter(name__contains=self.notebook['name'], is_history=True).count()
    assert 3 == Document.objects.filter(name__contains=self.notebook['name']).count()

  def test_get_history(self):
    assert 0 == Document2.objects.filter(name__contains=self.notebook['name'], is_history=True).count()
    _historify(self.notebook, self.user)
    _historify(self.notebook, self.user)
    _historify(self.notebook, self.user)
    assert 3 == Document2.objects.filter(name__contains=self.notebook['name'], is_history=True).count()

    # History should not return history objects that don't have the given doc type
    Document2.objects.create(name='Impala History', type='query-impala', data=self.notebook_json, owner=self.user, is_history=True)

    # Verify that get_history API returns history objects for given type and current user
    response = self.client.get(reverse('notebook:get_history'), {'doc_type': 'hive'})
    data = json.loads(response.content)
    assert 0 == data['status'], data
    assert 3 == len(data['history']), data
    assert all(doc['type'] == 'query-hive' for doc in data['history']), data

    # TODO: test that query history for shared query only returns docs accessible by current user

  def test_clear_history(self):
    assert 0 == Document2.objects.filter(name__contains=self.notebook['name'], is_history=True).count()
    _historify(self.notebook, self.user)
    _historify(self.notebook, self.user)
    _historify(self.notebook, self.user)
    assert 3 == Document2.objects.filter(name__contains=self.notebook['name'], is_history=True).count()

    # Clear history should not clear history objects that don't have the given doc type
    Document2.objects.create(name='Impala History', type='query-impala', owner=self.user, is_history=True)

    # clear history should retain original document but wipe history
    response = self.client.post(reverse('notebook:clear_history'), {'notebook': self.notebook_json, 'doc_type': 'hive'})
    data = json.loads(response.content)
    assert 0 == data['status'], data
    assert not Document2.objects.filter(type='query-hive', is_history=True).exists()
    assert Document2.objects.filter(type='query-hive', is_history=False).exists()
    assert Document2.objects.filter(type='query-impala', is_history=True).exists()

  def test_delete_notebook(self):
    trash_notebook_json = (
      """
        {
          "selectedSnippet": "hive",
          "showHistory": false,
          "description": "Test Hive Query",
          "name": "Test Hive Query",
          "sessions": [
              {
                  "type": "hive",
                  "properties": [],
                  "id": null
              }
          ],
          "type": "query-hive",
          "id": null,
          "snippets": [{"id": "e069ef32-5c95-4507-b961-e79c090b5abf","type":"hive","status":"ready","database":"default","""
      """"statement":"select * from web_logs","statement_raw":"select * from web_logs","variables":[],"properties":"""
      """{"settings":[],"files":[],"functions":[]},"result":{}}],
          "uuid": "8a20da5f-b69c-4843-b17d-dea5c74c41d1"
      }
      """
    )

    # Assert that the notebook is first saved
    response = self.client.post(reverse('notebook:save_notebook'), {'notebook': trash_notebook_json})
    data = json.loads(response.content)
    assert 0 == data['status'], data

    # Test that deleting it moves it to the user's Trash folder
    notebook_doc = Document2.objects.get(id=data['id'])
    trash_notebooks = [Notebook(notebook_doc).get_data()]
    response = self.client.post(reverse('notebook:delete'), {'notebooks': json.dumps(trash_notebooks)})
    data = json.loads(response.content)
    assert 0 == data['status'], data
    assert 'Trashed 1 notebook(s)' == data['message'], data

    response = self.client.get('/desktop/api2/doc', {'path': '/.Trash'})
    data = json.loads(response.content)
    trash_uuids = [doc['uuid'] for doc in data['children']]
    assert notebook_doc.uuid in trash_uuids, data

    # Test that any errors are reported in the response
    nonexistant_doc = {
      "id": 12345,
      "uuid": "ea22da5f-b69c-4843-b17d-dea5c74c41d1",
      "selectedSnippet": "hive",
      "showHistory": False,
      "description": "Test Hive Query",
      "name": "Test Hive Query",
      "sessions": [
        {
          "type": "hive",
          "properties": [],
          "id": None,
        }
      ],
      "type": "query-hive",
      "snippets": [
        {
          "id": "e069ef32-5c95-4507-b961-e79c090b5abf",
          "type": "hive",
          "status": "ready",
          "database": "default",
          "statement": "select * from web_logs",
          "statement_raw": "select * from web_logs",
          "variables": [],
          "properties": {"settings": [], "files": [], "functions": []},
          "result": {},
        }
      ],
    }
    trash_notebooks = [nonexistant_doc]
    response = self.client.post(reverse('notebook:delete'), {'notebooks': json.dumps(trash_notebooks)})
    data = json.loads(response.content)
    assert 0 == data['status'], data
    assert 'Trashed 0 notebook(s) and failed to delete 1 notebook(s).' == data['message'], data
    assert ['ea22da5f-b69c-4843-b17d-dea5c74c41d1'] == data['errors']

  def test_query_error_encoding(self):
    @api_error_handler
    def send_exception(message):
      raise QueryError(message=message)

    message = """SELECT a.key, a.* FROM customers c, c.addresses a"""
    response = send_exception(message)
    data = json.loads(response.content)
    assert 1 == data['status']

    message = """SELECT \u2002\u2002a.key, \u2002\u2002a.* FROM customers c, c.addresses a"""
    response = send_exception(message)
    data = json.loads(response.content)
    assert 1 == data['status']

    message = """SELECT a.key, a.* FROM déclenché c, c.addresses a"""
    response = send_exception(message)
    data = json.loads(response.content)
    assert 1 == data['status']

  def test_notebook_autocomplete(self):
    with patch('notebook.api.get_api') as get_api:
      get_api.return_value = Mock(
        autocomplete=Mock(
          side_effect=QueryExpired("HTTPSConnectionPool(host='gethue.com', port=10001): Read timed out. (read timeout=120)")
        )
      )

      response = self.client.post(
        reverse('notebook:api_autocomplete_tables', kwargs={'database': 'database'}), {'snippet': json.dumps({'type': 'hive'})}
      )

      data = json.loads(response.content)
      assert data == {'status': 0}  # We get back empty instead of failure with QueryExpired to silence end user messages

  def test_autocomplete_functions(self):
    # Note: better test would be to mock autocomplete() and not get_api() with hive and mysql dialects

    with patch('notebook.api.get_api') as get_api:
      get_api.return_value = Mock(autocomplete=Mock(return_value={'functions': [{'name': 'f1'}, {'name': 'f2'}, {'name': 'f3'}]}))

      response = self.client.post(
        reverse('notebook:api_autocomplete_databases'),
        {'snippet': json.dumps({'type': 'hive', 'properties': {}}), 'operation': 'functions'},
      )

      assert response.status_code == 200
      data = json.loads(response.content)
      assert data['status'] == 0

      assert data['functions'] == [{'name': 'f1'}, {'name': 'f2'}, {'name': 'f3'}]


class MockedApi(Api):
  def execute(self, notebook, snippet):
    return {
      'sync': True,
      'has_result_set': True,
      'result': {'has_more': False, 'data': [['test']], 'meta': [{'name': 'test', 'type': '', 'comment': ''}], 'type': 'table'},
    }

  def close_statement(self, notebook, snippet):
    pass

  def export_data_as_hdfs_file(self, snippet, target_file, overwrite):
    return {'destination': target_file}


class MockFs(object):
  def __init__(self, logical_name=None):
    self.fs_defaultfs = 'hdfs://curacao:8020'
    self.logical_name = logical_name if logical_name else ''
    self.DEFAULT_USER = 'test'
    self.user = 'test'
    self._filebrowser_action = ''

  def setuser(self, user):
    self._user = user

  @property
  def user(self):
    return self._user

  def do_as_user(self, username, fn, *args, **kwargs):
    return ''

  def exists(self, path):
    if path == '/user/hue/non_exists_directory':
      return False
    return True

  def listdir_stats(self, path):
    if path == '/user/hue/non_empty_directory':
      return ['mock_dir', 'mock_file']
    return []

  def isdir(self, path):
    return path == '/user/hue'

  def filebrowser_action(self):
    return self._filebrowser_action

  @user.setter
  def user(self, value):
    self._user = value


@pytest.mark.django_db
class TestNotebookApiMocked(object):
  def setup_method(self):
    self.client = make_logged_in_client(username="test", groupname="default", recreate=True, is_superuser=False)
    self.client_not_me = make_logged_in_client(username="not_perm_user", groupname="default", recreate=True, is_superuser=False)

    self.user = User.objects.get(username="test")
    self.user_not_me = User.objects.get(username="not_perm_user")

    # Beware: Monkey patch HS2API Mock API
    if not hasattr(notebook.connectors.hiveserver2, 'original_HS2Api'):  # Could not monkey patch base.get_api
      notebook.connectors.hiveserver2.original_HS2Api = notebook.connectors.hiveserver2.HS2Api
    notebook.connectors.hiveserver2.HS2Api = MockedApi

    originalCluster.get_hdfs()
    self.original_fs = originalCluster.FS_CACHE["default"]
    originalCluster.FS_CACHE["default"] = MockFs()

    grant_access("test", "default", "notebook")
    grant_access("test", "default", "beeswax")
    grant_access("test", "default", "hive")
    grant_access("not_perm_user", "default", "notebook")
    grant_access("not_perm_user", "default", "beeswax")
    grant_access("not_perm_user", "default", "hive")
    add_permission('test', 'has_adls', permname='adls_access', appname='filebrowser')

  def teardown_method(self):
    notebook.connectors.hiveserver2.HS2Api = notebook.connectors.hiveserver2.original_HS2Api

    if originalCluster.FS_CACHE is None:
      originalCluster.FS_CACHE = {}
    originalCluster.FS_CACHE["default"] = self.original_fs

  @pytest.mark.integration
  def test_export_result(self):
    notebook_json = (
      """
      {
        "selectedSnippet": "hive",
        "showHistory": false,
        "description": "Test Hive Query",
        "name": "Test Hive Query",
        "sessions": [
            {
                "type": "hive",
                "properties": [],
                "id": null
            }
        ],
        "type": "query-hive",
        "id": null,
        "snippets": [{"id":"2b7d1f46-17a0-30af-efeb-33d4c29b1055","type":"hive","status":"running","statement":"""
      """"select * from web_logs","properties":{"settings":[],"variables":[],"files":[],"functions":[]},"""
      """"result":{"id":"b424befa-f4f5-8799-a0b4-79753f2552b1","type":"table","handle":"""
      """{"log_context":null,"statements_count":1,"end":{"column":21,"row":0},"statement_id":0,"""
      """"has_more_statements":false,"start":{"column":0,"row":0},"secret":"rVRWw7YPRGqPT7LZ/TeFaA==an","""
      """"has_result_set":true,"statement":"select * from web_logs","operation_type":0,"modified_row_count":"""
      """null,"guid":"7xm6+epkRx6dyvYvGNYePA==an"}},"lastExecuted": 1462554843817,"database":"default"}],
        "uuid": "d9efdee1-ef25-4d43-b8f9-1a170f69a05a"
    }
    """
    )

    response = self.client.post(
      reverse('notebook:export_result'),
      {
        'notebook': notebook_json,
        'snippet': json.dumps(json.loads(notebook_json)['snippets'][0]),
        'format': json.dumps('hdfs-file'),
        'destination': json.dumps('/user/hue'),
        'overwrite': json.dumps(False),
      },
    )

    data = json.loads(response.content)
    assert 0 == data['status'], data
    assert '/user/hue/Test Hive Query.csv' == data['watch_url']['destination'], data

    response = self.client.post(
      reverse('notebook:export_result'),
      {
        'notebook': notebook_json,
        'snippet': json.dumps(json.loads(notebook_json)['snippets'][0]),
        'format': json.dumps('hdfs-file'),
        'destination': json.dumps('/user/hue/path.csv'),
        'overwrite': json.dumps(False),
      },
    )

    data = json.loads(response.content)
    assert 0 == data['status'], data
    assert '/user/hue/path.csv' == data['watch_url']['destination'], data

    if is_adls_enabled():
      response = self.client.post(
        reverse('notebook:export_result'),
        {
          'notebook': notebook_json,
          'snippet': json.dumps(json.loads(notebook_json)['snippets'][0]),
          'format': json.dumps('hdfs-file'),
          'destination': json.dumps('adl:/user/hue/path.csv'),
          'overwrite': json.dumps(False),
        },
      )

      data = json.loads(response.content)
      assert 0 == data['status'], data
      assert 'adl:/user/hue/path.csv' == data['watch_url']['destination'], data

    response = self.client.post(
      reverse('notebook:export_result'),
      {
        'notebook': notebook_json,
        'snippet': json.dumps(json.loads(notebook_json)['snippets'][0]),
        'format': json.dumps('hdfs-directory'),
        'destination': json.dumps('/user/hue/non_empty_directory'),
        'overwrite': json.dumps(False),
      },
    )

    data = json.loads(response.content)
    assert -1 == data['status'], data
    assert 'The destination is not an empty directory!' == data['message'], data

  def test_download_result(self):
    notebook_json = (
      """
      {
        "selectedSnippet": "hive",
        "showHistory": false,
        "description": "Test Hive Query",
        "name": "Test Hive Query",
        "sessions": [
            {
                "type": "hive",
                "properties": [],
                "id": null
            }
        ],
        "type": "query-hive",
        "id": null,
        "snippets": [{"id":"2b7d1f46-17a0-30af-efeb-33d4c29b1055","type":"hive","status":"running","statement":"""
      """"select * from web_logs","properties":{"settings":[],"variables":[],"files":[],"functions":[]},"""
      """"result":{"id":"b424befa-f4f5-8799-a0b4-79753f2552b1","type":"table","handle":{"log_context":null,"""
      """"statements_count":1,"end":{"column":21,"row":0},"statement_id":0,"has_more_statements":false,"""
      """"start":{"column":0,"row":0},"secret":"rVRWw7YPRGqPT7LZ/TeFaA==an","has_result_set":true,"statement":"""
      """"select * from web_logs","operation_type":0,"modified_row_count":null,"guid":"7xm6+epkRx6dyvYvGNYePA==an"}},"""
      """"lastExecuted": 1462554843817,"database":"default"}],
        "uuid": "d9efdee1-ef25-4d43-b8f9-1a170f69a05a"
    }
    """
    )
    response = self.client.post(
      reverse('notebook:download'),
      {'notebook': notebook_json, 'snippet': json.dumps(json.loads(notebook_json)['snippets'][0]), 'format': 'csv'},
    )
    content = b"".join(response)
    assert len(content) > 0


def test_get_interpreters_to_show():
  default_interpreters = OrderedDict(
    (
      (
        'hive',
        {
          'name': 'Hive',
          'displayName': 'Hive',
          'interface': 'hiveserver2',
          'type': 'hive',
          'is_sql': True,
          'options': {},
          'dialect_properties': {},
          'is_catalog': False,
          'category': 'editor',
          'dialect': 'hive',
        },
      ),
      (
        'impala',
        {
          'name': 'Impala',
          'displayName': 'Impala',
          'interface': 'hiveserver2',
          'type': 'impala',
          'is_sql': True,
          'options': {},
          'dialect_properties': {},
          'is_catalog': False,
          'category': 'editor',
          'dialect': 'impala',
        },
      ),
      (
        'spark',
        {
          'name': 'Scala',
          'displayName': 'Scala',
          'interface': 'livy',
          'type': 'spark',
          'is_sql': False,
          'options': {},
          'dialect_properties': {},
          'is_catalog': False,
          'category': 'editor',
          'dialect': 'spark',
        },
      ),
      (
        'pig',
        {
          'name': 'Pig',
          'displayName': 'Pig',
          'interface': 'pig',
          'type': 'pig',
          'is_sql': False,
          'options': {},
          'dialect_properties': {},
          'is_catalog': False,
          'category': 'editor',
          'dialect': 'pig',
        },
      ),
      (
        'java',
        {
          'name': 'Java',
          'displayName': 'Java',
          'interface': 'oozie',
          'type': 'java',
          'is_sql': False,
          'options': {},
          'dialect_properties': {},
          'is_catalog': False,
          'category': 'editor',
          'dialect': 'java',
        },
      ),
    )
  )

  expected_interpreters = OrderedDict(
    (
      (
        'java',
        {
          'name': 'Java',
          'displayName': 'Java',
          'interface': 'oozie',
          'type': 'java',
          'is_sql': False,
          'options': {},
          'dialect_properties': {},
          'is_catalog': False,
          'category': 'editor',
          'dialect': 'java',
        },
      ),
      (
        'pig',
        {
          'name': 'Pig',
          'displayName': 'Pig',
          'interface': 'pig',
          'is_sql': False,
          'type': 'pig',
          'options': {},
          'dialect_properties': {},
          'is_catalog': False,
          'category': 'editor',
          'dialect': 'pig',
        },
      ),
      (
        'hive',
        {
          'name': 'Hive',
          'displayName': 'Hive',
          'interface': 'hiveserver2',
          'is_sql': True,
          'type': 'hive',
          'options': {},
          'dialect_properties': {},
          'is_catalog': False,
          'category': 'editor',
          'dialect': 'hive',
        },
      ),
      (
        'impala',
        {
          'name': 'Impala',
          'displayName': 'Impala',
          'interface': 'hiveserver2',
          'type': 'impala',
          'is_sql': True,
          'options': {},
          'dialect_properties': {},
          'is_catalog': False,
          'category': 'editor',
          'dialect': 'impala',
        },
      ),
      (
        'spark',
        {
          'name': 'Scala',
          'displayName': 'Scala',
          'interface': 'livy',
          'type': 'spark',
          'is_sql': False,
          'options': {},
          'dialect_properties': {},
          'is_catalog': False,
          'category': 'editor',
          'dialect': 'spark',
        },
      ),
    )
  )

  try:
    resets = [
      INTERPRETERS.set_for_testing(default_interpreters),
      APP_BLACKLIST.set_for_testing(''),
      ENABLE_CONNECTORS.set_for_testing(False),
      ENABLE_ALL_INTERPRETERS.set_for_testing(False),
    ]
    appmanager.DESKTOP_MODULES = []
    appmanager.DESKTOP_APPS = None
    appmanager.load_apps(APP_BLACKLIST.get())
    notebook.conf.INTERPRETERS_CACHE = None

    # 'get_interpreters_to_show should return the same as get_interpreters when interpreters_shown_on_wheel is unset'
    assert list(default_interpreters.values()) == get_ordered_interpreters()

    resets.append(INTERPRETERS_SHOWN_ON_WHEEL.set_for_testing('java,pig'))

    # 'get_interpreters_to_show did not return interpreters in the correct order expected'
    assert (
      list(expected_interpreters.values()) == get_ordered_interpreters()
    ), 'get_interpreters_to_show did not return interpreters in the correct order expected'
  finally:
    for reset in resets:
      reset()
    appmanager.DESKTOP_MODULES = []
    appmanager.DESKTOP_APPS = None
    appmanager.load_apps(APP_BLACKLIST.get())
    notebook.conf.INTERPRETERS_CACHE = None


def test_get_ordered_interpreters():
  try:
    resets = [APP_BLACKLIST.set_for_testing(''), ENABLE_ALL_INTERPRETERS.set_for_testing(False)]
    flag_reset = ENABLE_ALL_INTERPRETERS.set_for_testing(False)
    appmanager.DESKTOP_MODULES = []
    appmanager.DESKTOP_APPS = None
    appmanager.load_apps(APP_BLACKLIST.get())

    with patch('notebook.conf.appmanager.get_apps_dict') as get_apps_dict:
      with patch('notebook.conf.has_connectors') as has_connectors:
        get_apps_dict.return_value = {'hive': {}}  # Impala blacklisted indirectly
        has_connectors.return_value = False
        notebook.conf.INTERPRETERS_CACHE = None

        # No interpreters explicitly added
        INTERPRETERS.set_for_testing(OrderedDict(()))

        assert [interpreter['name'] for interpreter in get_ordered_interpreters()] == ['Hive']
        assert [interpreter['name'] for interpreter in get_ordered_interpreters()] == ['Hive']  # Check twice because of cache
        notebook.conf.INTERPRETERS_CACHE = None

        # Interpreter added explicitly
        INTERPRETERS.set_for_testing(OrderedDict((('phoenix', {'name': 'Phoenix', 'interface': 'sqlalchemy', 'dialect': 'phoenix'}),)))
        assert [interpreter['name'] for interpreter in get_ordered_interpreters()] == ['Hive', 'Phoenix']
        assert [interpreter['name'] for interpreter in get_ordered_interpreters()] == ['Hive', 'Phoenix']  # Check twice
        notebook.conf.INTERPRETERS_CACHE = None

        # Add one of the spark editor explicitly when spark is blacklisted
        INTERPRETERS.set_for_testing(OrderedDict((('pyspark', {'name': 'PySpark', 'interface': 'livy', 'dialect': 'pyspark'}),)))
        # Explicitly added spark editor not seen when flag is False
        assert [interpreter['name'] for interpreter in get_ordered_interpreters()] == ['Hive']
        assert [interpreter['name'] for interpreter in get_ordered_interpreters()] == ['Hive']  # Check twice because of cache
        notebook.conf.INTERPRETERS_CACHE = None

        # Whitelist spark app and no explicit interpreter added
        get_apps_dict.return_value = {'hive': {}, 'spark': {}, 'oozie': {}}

        INTERPRETERS.set_for_testing(OrderedDict(()))
        # No spark interpreter because ENABLE_ALL_INTERPRETERS is currently False
        assert [interpreter['name'] for interpreter in get_ordered_interpreters()] == ['Hive']
        assert [interpreter['name'] for interpreter in get_ordered_interpreters()] == ['Hive']  # Check twice because of cache
        notebook.conf.INTERPRETERS_CACHE = None

        # Add one of the spark editor explicitly
        INTERPRETERS.set_for_testing(OrderedDict((('pyspark', {'name': 'PySpark', 'interface': 'livy', 'dialect': 'pyspark'}),)))
        # Explicitly added spark editor seen even when flag is False
        assert [interpreter['name'] for interpreter in get_ordered_interpreters()] == ['Hive', 'PySpark']
        assert [interpreter['name'] for interpreter in get_ordered_interpreters()] == ['Hive', 'PySpark']  # Check twice because of cache
        notebook.conf.INTERPRETERS_CACHE = None

        flag_reset = ENABLE_ALL_INTERPRETERS.set_for_testing(True)  # Check interpreters when flag is True

        INTERPRETERS.set_for_testing(OrderedDict(()))

        assert [interpreter['name'] for interpreter in get_ordered_interpreters()] == [
          'Hive',
          'Scala',
          'PySpark',
          'R',
          'Spark Submit Jar',
          'Spark Submit Python',
          'Text',
          'Markdown',
        ]
        assert [interpreter['name'] for interpreter in get_ordered_interpreters()] == [
          'Hive',
          'Scala',
          'PySpark',
          'R',
          'Spark Submit Jar',
          'Spark Submit Python',
          'Text',
          'Markdown',
        ]  # Check twice because of cache
        notebook.conf.INTERPRETERS_CACHE = None

        # Interpreter added explicitly when flag is True
        INTERPRETERS.set_for_testing(OrderedDict((('phoenix', {'name': 'Phoenix', 'interface': 'sqlalchemy', 'dialect': 'phoenix'}),)))
        assert [interpreter['name'] for interpreter in get_ordered_interpreters()] == [
          'Hive',
          'Scala',
          'PySpark',
          'R',
          'Spark Submit Jar',
          'Spark Submit Python',
          'Text',
          'Markdown',
          'Phoenix',
        ]
        assert [interpreter['name'] for interpreter in get_ordered_interpreters()] == [
          'Hive',
          'Scala',
          'PySpark',
          'R',
          'Spark Submit Jar',
          'Spark Submit Python',
          'Text',
          'Markdown',
          'Phoenix',
        ]  # Check twice because of cache

  finally:
    flag_reset()
    for reset in resets:
      reset()
    appmanager.DESKTOP_MODULES = []
    appmanager.DESKTOP_APPS = None
    appmanager.load_apps(APP_BLACKLIST.get())
    notebook.conf.INTERPRETERS_CACHE = None


class TestQueriesMetrics(object):
  def test_queries_num(self):
    with patch('desktop.models.Document2.objects') as doc2_value_mock:
      doc2_value_mock.filter.return_value.count.return_value = 12500
      count = num_of_queries()
      assert 12500 == count

      if not ENABLE_PROMETHEUS.get():
        pytest.skip("Skipping Test")

      c = Client()
      response = c.get('/metrics')
      assert b'hue_queries_numbers 12500.0' in response.content, response.content


@pytest.mark.django_db
class TestEditor(object):
  def setup_method(self):
    self.client = make_logged_in_client(username="test", groupname="empty", recreate=True, is_superuser=False)

    self.user = User.objects.get(username="test")

    grant_access("test", "empty", "impala")

  def test_open_saved_impala_query_when_no_hive_interepreter(self):
    try:
      doc, created = Document2.objects.get_or_create(
        name='open_saved_query_with_hive_not_present', type='query-impala', owner=self.user, data={}
      )

      with patch('desktop.middleware.fsmanager') as fsmanager:
        response = self.client.get(reverse('notebook:editor'), {'editor': doc.id, 'is_embeddable': True})
        assert 200 == response.status_code
    finally:
      doc.delete()


@pytest.mark.django_db
class TestPrivateURLPatterns():

  def setup_method(self):
    self.client = make_logged_in_client(username="api_user", recreate=True, is_superuser=False)
    self.client_not_me = make_logged_in_client(username="not_api_user", recreate=True, is_superuser=False)

    self.user = User.objects.get(username="api_user")
    self.user_not_me = User.objects.get(username="not_api_user")

  def test_autocomplete_databases(self):
    """
    Test the autocomplete URL for databases
    """
    response = self.client.post('/notebook/api/autocomplete/')
    assert response.status_code == 200

  def test_autocomplete_tables(self):
    """
    Test the autocomplete URL for tables in a specific database
    """
    # Test with a valid database name
    response = self.client.post('/notebook/api/autocomplete/test_db')
    assert response.status_code == 200

    # Test with a special character in the database name
    response = self.client.post('/notebook/api/autocomplete/test_db:-;test')
    assert response.status_code == 200

  def test_autocomplete_columns(self):
    """
    Test the autocomplete URL for columns in a specific table within a database
    """
    # Test with valid database and table names
    response = self.client.post('/notebook/api/autocomplete/test_db/test_table')
    assert response.status_code == 200

    # Test with special characters in the database and table names
    response = self.client.post('/notebook/api/autocomplete/test_db:-$@test/test_table:-$@test')
    assert response.status_code == 200

  def test_describe_database(self):
    """
    Test the describe URL for a specific database
    """
    # Test with a valid database name
    response = self.client.post('/notebook/api/describe/test_db/')
    assert response.status_code == 200

    # Test with a special character in the database name
    response = self.client.post('/notebook/api/describe/test_db:-$@test/')
    assert response.status_code == 200

  def test_describe_table(self):
    """
    Test the describe URL for a specific table in a database
    """
    # Test with valid database and table names
    response = self.client.post('/notebook/api/describe/test_db/test_table/')
    assert response.status_code == 200

    # Test with special characters in the database and table names
    response = self.client.post('/notebook/api/describe/test_db:-$@test/test_table:-$@test/')
    assert response.status_code == 200

  def test_describe_column(self):
    """
    Test the describe URL for a specific column in a table within a database
    """
    # Test with valid database, table, and column names
    response = self.client.post('/notebook/api/describe/test_db/test_table/stats/test_column/')
    assert response.status_code == 200

    # Test with special characters in the database, table, and column names
    response = self.client.post('/notebook/api/describe/test_db:-$@test/test_table:-$@test/stats/test_column:-$@test/')
    assert response.status_code == 200

  def test_sample_data_for_table(self):
    """
    Test the sample data URL for a specific table in a database
    """
    # Test with valid database and table names
    response = self.client.post('/notebook/api/sample/test_db/test_table/')
    assert response.status_code == 200

    # Test with special characters in the database and table names
    response = self.client.post('/notebook/api/sample/test_db:-$@test/test_table:-$@test/')
    assert response.status_code == 200

  def test_sample_data_for_column(self):
    """
    Test the sample data URL for a specific column in a table within a database
    """
    # Test with valid database, table, and column names
    response = self.client.post('/notebook/api/sample/test_db/test_table/test_column/')
    assert response.status_code == 200

    # Test with special characters in the database, table, and column names
    response = self.client.post('/notebook/api/sample/test_db:-$@test/test_table:-$@test/test_column:-$@test/')
    assert response.status_code == 200

  def test_sample_data_for_nested(self):
    """
    Test the sample data URL for a specific nested field in a column of a table within a database
    """
    # Test with valid database, table, column, and nested field
    response = self.client.post('/notebook/api/sample/test_db/test_table/test_column/test_nested/')
    assert response.status_code == 200

    # Test with special characters in the database, table, column, and nested field names
    response = self.client.post('/notebook/api/sample/test_db:-$@test/test_table:-$@test/test_column:-$@test/test_nested:-$@test/')
    assert response.status_code == 200
