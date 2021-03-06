#!/usr/bin/env python
# -*- coding: utf-8 -*-

###############################################################################
#  Copyright Kitware Inc.
#
#  Licensed under the Apache License, Version 2.0 ( the "License" );
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
###############################################################################

from girder.api import access
from girder.api.describe import Description, describeRoute
from girder.api.rest import Resource, filtermodel, loadmodel
from girder.constants import AccessType, TokenScope
from girder.utility.model_importer import ModelImporter
from girder.api.rest import getBodyJson, getCurrentUser

from girder.plugins.imagespace import solr_documents_from_field
from girder import logger

from .settings import SmqtkSetting
from .utils import getCreateSessionsFolder

import json
import requests


class SmqtkIqr(Resource):
    def __init__(self):
        setting = SmqtkSetting()
        self.search_url = setting.get('IMAGE_SPACE_SMQTK_IQR_URL')
        self.resourceName = 'smqtk_iqr'
        self.route('POST', ('session',), self.createSession)
        self.route('GET', ('session',), self.getSessions)
        self.route('PUT', ('session', ':id'), self.updateSession)
        self.route('GET', ('session_folder',), self.getSessionFolder)
        self.route('PUT', ('refine',), self.refine)
        self.route('GET', ('results',), self.results)

    @access.user
    @describeRoute(
        Description('Get all session items')
    )
    def getSessions(self, params):
        sessionsFolder = getCreateSessionsFolder()
        return list(ModelImporter.model('folder').childItems(folder=sessionsFolder))

    @access.user
    @describeRoute(Description('Get session folder'))
    def getSessionFolder(self, params):
        return getCreateSessionsFolder()

    @access.user
    @describeRoute(
        Description('Create an IQR session, return the Girder Item representing that session')
    )
    def createSession(self, params):
        sessionsFolder = getCreateSessionsFolder()
        sessionId = requests.post(self.search_url + '/session').json()['sid']
        item = ModelImporter.model('item').createItem(name=sessionId,
                                                      creator=getCurrentUser(),
                                                      folder=sessionsFolder)
        ModelImporter.model('item').setMetadata(item, {
            'sid': sessionId
        })

        return item
        # create sessions folder in private directory if not existing
        # post to init_session, get sid back
        # create item named sid in sessions folder

    @access.user(scope=TokenScope.DATA_WRITE)
    @loadmodel(model='item', level=AccessType.WRITE)
    @filtermodel(model='item')
    @describeRoute(
        Description('Update a session item')
        .responseClass('Item')
        .param('id', 'The ID of the item.', paramType='path')
        .param('name', 'Name for the item.', required=False)
        .param('description', 'Description for the item.', required=False)
        .errorResponse('ID was invalid.')
        .errorResponse('Write access was denied for the item or folder.', 403))
    def updateSession(self, item, params):
        item['name'] = params.get('name', item['name']).strip()
        item['description'] = params.get(
            'description', item['description']).strip()

        self.model('item').updateItem(item)
        return item

    @access.user
    @describeRoute(
        Description('Refine results based on positive and negative uuids')
        .param('body', 'A JSON object containing the sid and pos_uuids and neg_uuids.',
               paramType='body')
    )
    def refine(self, params):
        return self._refine(getBodyJson())

    def _refine(self, params):
        # Sessions in SMQTK expire, but stay around in Girder
        # Force creation of a session with this id each time
        requests.post(self.search_url + '/session', data={'sid': params['sid']})

        r = requests.put(self.search_url + '/refine', data={
            'sid': params['sid'],
            'pos_uuids': json.dumps(params['pos_uuids']),
            'neg_uuids': json.dumps(params['neg_uuids'])
        })

        return r.json()

    @access.user
    @describeRoute(
        Description('Get the results of an IQR session')
        .param('sid', 'ID of the IQR session')
        .param('offset', 'Where to start from')
        .param('limit', 'How many records to pull')
    )
    def results(self, params):
        def sid_exists(sid):
            """
            Determine if a session ID already exists in SMQTK.
            This currently creates the session if it doesn't already exist.
            """
            return not requests.post(self.search_url + '/session', data={'sid': params['sid']}).ok

        offset = int(params['offset'] if 'offset' in params else 0)
        limit = int(params['limit'] if 'limit' in params else 20)

        if not sid_exists(params['sid']):
            # Get pos/neg uuids from current session
            session = self.model('item').findOne({'meta.sid': params['sid']})

            if session:
                self._refine({
                    'sid': params['sid'],
                    'pos_uuids': session['meta']['pos_uuids'],
                    'neg_uuids': session['meta']['neg_uuids']})

        resp = requests.get(self.search_url + '/get_results', params={
            'sid': params['sid'],
            'i': offset,
            'j': offset + limit
        }).json() # @todo handle errors

        try:
            documents = solr_documents_from_field('sha1sum_s_md', [sha for (sha, _) in resp['results']])
        except KeyError:
            return { 'numFound': 0, 'docs': [] }


        # The documents from Solr (since shas map to >= 1 document) may not be in the order of confidence
        # returned by IQR, sort the documents to match the confidence values.
        # Sort by confidence values first, then sha checksums second so duplicate images are grouped together
        confidenceValues = dict(resp['results'])  # Mapping of sha -> confidence values

        if len(documents) < len(resp['results']):
            logger.error('SID %s: There are SMQTK descriptors that have no corresponding Solr document(s).' % params['sid'])

        for document in documents:
            if isinstance(document['sha1sum_s_md'], list):
                document['smqtk_iqr_confidence'] = confidenceValues[document['sha1sum_s_md'][0]]
            else:
                document['smqtk_iqr_confidence'] = confidenceValues[document['sha1sum_s_md']]

        return {
            'numFound': resp['total_results'],
            'docs': sorted(documents,
                           key=lambda x: (x['smqtk_iqr_confidence'],
                                          x['sha1sum_s_md'][0] if isinstance(x['sha1sum_s_md'],list) else x['sha1sum_s_md']),
                           reverse=True)
        }
