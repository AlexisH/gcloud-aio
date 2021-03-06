import io
import json
import uuid
from enum import Enum
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Union

from gcloud.aio.auth import AioSession  # pylint: disable=no-name-in-module
from gcloud.aio.auth import BUILD_GCLOUD_REST  # pylint: disable=no-name-in-module
from gcloud.aio.auth import Token  # pylint: disable=no-name-in-module

# Selectively load libraries based on the package
if BUILD_GCLOUD_REST:
    from requests import Session
else:
    from aiohttp import ClientSession as Session


API_ROOT = 'https://www.googleapis.com/bigquery/v2'
SCOPES = [
    'https://www.googleapis.com/auth/bigquery.insertdata',
    'https://www.googleapis.com/auth/bigquery',
]


class SourceFormat(Enum):
    AVRO = 'AVRO'
    CSV = 'CSV'
    DATASTORE_BACKUP = 'DATASTORE_BACKUP'
    NEWLINE_DELIMITED_JSON = 'NEWLINE_DELIMITED_JSON'
    ORC = 'ORC'
    PARQUET = 'PARQUET'


class Disposition(Enum):
    WRITE_APPEND = 'WRITE_APPEND'
    WRITE_EMPTY = 'WRITE_EMPTY'
    WRITE_TRUNCATE = 'WRITE_TRUNCATE'


class Table:
    def __init__(self, dataset_name: str, table_name: str,
                 project: Optional[str] = None,
                 service_file: Optional[Union[str, io.IOBase]] = None,
                 session: Optional[Session] = None,
                 token: Optional[Token] = None) -> None:
        self._project = project
        self.dataset_name = dataset_name
        self.table_name = table_name

        self.session = AioSession(session)
        self.token = token or Token(service_file=service_file, scopes=SCOPES,
                                    session=self.session.session)

    async def project(self) -> str:
        if self._project:
            return self._project

        self._project = await self.token.get_project()
        if self._project:
            return self._project

        raise Exception('could not determine project, please set it manually')

    @staticmethod
    def _mk_unique_insert_id(row: Dict[str, Any]) -> str:
        # pylint: disable=unused-argument
        return uuid.uuid4().hex

    def _make_copy_body(
            self, source_project: str, destination_project: str,
            destination_dataset: str,
            destination_table: str) -> Dict[str, Any]:
        return {
            'configuration': {
                'copy': {
                    'writeDisposition': 'WRITE_TRUNCATE',
                    'destinationTable': {
                        'projectId': destination_project,
                        'datasetId': destination_dataset,
                        'tableId': destination_table,
                    },
                    'sourceTable': {
                        'projectId': source_project,
                        'datasetId': self.dataset_name,
                        'tableId': self.table_name,
                    }
                }
            }
        }

    @staticmethod
    def _make_insert_body(
            rows: List[Dict[str, Any]], *, skip_invalid: bool,
            ignore_unknown: bool, template_suffix: Optional[str],
            insert_id_fn: Callable[[Dict[str, Any]], str]) -> Dict[str, Any]:
        body = {
            'kind': 'bigquery#tableDataInsertAllRequest',
            'skipInvalidRows': skip_invalid,
            'ignoreUnknownValues': ignore_unknown,
            'rows': [{
                'insertId': insert_id_fn(row),
                'json': row,
            } for row in rows],
        }

        if template_suffix is not None:
            body['templateSuffix'] = template_suffix

        return body

    def _make_load_body(
            self, source_uris: List[str], project: str, autodetect: bool,
            source_format: SourceFormat,
            write_disposition: Disposition) -> Dict[str, Any]:
        return {
            'configuration': {
                'load': {
                    'autodetect': autodetect,
                    'sourceUris': source_uris,
                    'sourceFormat': source_format.value,
                    'writeDisposition': write_disposition.value,
                    'destinationTable': {
                        'projectId': project,
                        'datasetId': self.dataset_name,
                        'tableId': self.table_name,
                    },
                },
            },
        }

    def _make_query_body(
            self, query: str, project: str,
            write_disposition: Disposition) -> Dict[str, Any]:
        return {
            'configuration': {
                'query': {
                    'query': query,
                    'writeDisposition': write_disposition.value,
                    'destinationTable': {
                        'projectId': project,
                        'datasetId': self.dataset_name,
                        'tableId': self.table_name,
                    },
                },
            },
        }

    async def headers(self) -> Dict[str, str]:
        token = await self.token.get()
        return {
            'Authorization': f'Bearer {token}',
        }

    async def _post_json(
            self, url: str, body: Dict[str, Any], session: Optional[Session],
            timeout: int) -> Dict[str, Any]:
        payload = json.dumps(body).encode('utf-8')

        headers = await self.headers()
        headers.update({
            'Content-Length': str(len(payload)),
            'Content-Type': 'application/json',
        })

        s = AioSession(session) if session else self.session
        resp = await s.post(url, data=payload, headers=headers, params=None,
                            timeout=timeout)
        return await resp.json()

    # https://cloud.google.com/bigquery/docs/reference/rest/v2/tables/delete
    async def delete(self,
                     session: Optional[Session] = None,
                     timeout: int = 60) -> Dict[str, Any]:
        """Deletes the table specified by tableId from the dataset."""
        project = await self.project()
        url = (f'{API_ROOT}/projects/{project}/datasets/'
               f'{self.dataset_name}/tables/{self.table_name}')

        headers = await self.headers()

        s = AioSession(session) if session else self.session
        resp = await s.session.delete(url, headers=headers, params=None,
                                      timeout=timeout)
        try:
            return await resp.json()
        except Exception:  # pylint: disable=broad-except
            # For some reason, `gcloud-rest` seems to have intermittent issues
            # parsing this response. In that case, fall back to returning the
            # raw response body.
            try:
                return {'response': await resp.text()}
            except (AttributeError, TypeError):
                return {'response': resp.text}

    # https://cloud.google.com/bigquery/docs/reference/rest/v2/tables/get
    async def get(
            self, session: Optional[Session] = None,
            timeout: int = 60) -> Dict[str, Any]:
        """Gets the specified table resource by table ID."""
        project = await self.project()
        url = (f'{API_ROOT}/projects/{project}/datasets/'
               f'{self.dataset_name}/tables/{self.table_name}')

        headers = await self.headers()

        s = AioSession(session) if session else self.session
        resp = await s.get(url, headers=headers, timeout=timeout)
        return await resp.json()

    # https://cloud.google.com/bigquery/docs/reference/rest/v2/tabledata/insertAll
    async def insert(
            self, rows: List[Dict[str, Any]], skip_invalid: bool = False,
            ignore_unknown: bool = True, session: Optional[Session] = None,
            template_suffix: Optional[str] = None,
            timeout: int = 60, *,
            insert_id_fn: Optional[Callable[[Dict[str, Any]], str]] = None,
    ) -> Dict[str, Any]:
        """
        Streams data into BigQuery

        By default, each row is assigned a unique insertId. This can be
        customized by supplying an `insert_id_fn` which takes a row and
        returns an insertId.

        In cases where at least one row has successfully been inserted and at
        least one row has failed to be inserted, the Google API will return a
        2xx (successful) response along with an `insertErrors` key in the
        response JSON containing details on the failing rows.
        """
        if not rows:
            return {}

        project = await self.project()
        url = (f'{API_ROOT}/projects/{project}/datasets/{self.dataset_name}/'
               f'tables/{self.table_name}/insertAll')

        body = self._make_insert_body(
            rows, skip_invalid=skip_invalid, ignore_unknown=ignore_unknown,
            template_suffix=template_suffix,
            insert_id_fn=insert_id_fn or self._mk_unique_insert_id)
        return await self._post_json(url, body, session, timeout)

    # https://cloud.google.com/bigquery/docs/reference/rest/v2/jobs/insert
    # https://cloud.google.com/bigquery/docs/reference/rest/v2/Job#jobconfigurationtablecopy
    async def insert_via_copy(
            self, destination_project: str, destination_dataset: str,
            destination_table: str, session: Optional[Session] = None,
            timeout: int = 60) -> Dict[str, Any]:
        """Copy BQ table to another table in BQ"""
        project = await self.project()
        url = f'{API_ROOT}/projects/{project}/jobs'

        body = self._make_copy_body(
            project, destination_project,
            destination_dataset, destination_table)
        return await self._post_json(url, body, session, timeout)

    # https://cloud.google.com/bigquery/docs/reference/rest/v2/jobs/insert
    # https://cloud.google.com/bigquery/docs/reference/rest/v2/Job#JobConfigurationLoad
    async def insert_via_load(
            self, source_uris: List[str], session: Optional[Session] = None,
            autodetect: bool = False,
            source_format: SourceFormat = SourceFormat.CSV,
            write_disposition: Disposition = Disposition.WRITE_TRUNCATE,
            timeout: int = 60) -> Dict[str, Any]:
        """Loads entities from storage to BigQuery."""
        if not source_uris:
            return {}

        project = await self.project()
        url = f'{API_ROOT}/projects/{project}/jobs'

        body = self._make_load_body(
            source_uris, project, autodetect, source_format, write_disposition,
        )
        return await self._post_json(url, body, session, timeout)

    # https://cloud.google.com/bigquery/docs/reference/rest/v2/jobs/insert
    # https://cloud.google.com/bigquery/docs/reference/rest/v2/Job#JobConfigurationQuery
    async def insert_via_query(
            self, query: str, session: Optional[Session] = None,
            write_disposition: Disposition = Disposition.WRITE_EMPTY,
            timeout: int = 60) -> Dict[str, Any]:
        """Create table as a result of the query"""
        if not query:
            return {}

        project = await self.project()
        url = f'{API_ROOT}/projects/{project}/jobs'

        body = self._make_query_body(query, project, write_disposition)
        return await self._post_json(url, body, session, timeout)

    async def close(self):
        await self.session.close()

    async def __aenter__(self) -> 'Table':
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()
