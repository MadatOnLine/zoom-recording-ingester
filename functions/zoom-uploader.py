import json
import boto3
import requests
from requests.auth import HTTPDigestAuth
from urllib.parse import urljoin
from hashlib import md5
from os import getenv as env
import xml.etree.ElementTree as ET
from datetime import datetime
from pytz import timezone
from xml.sax.saxutils import escape
from uuid import UUID

import logging
from common import setup_logging
logger = logging.getLogger()

UPLOAD_QUEUE_NAME = env("UPLOAD_QUEUE_NAME")
OPENCAST_BASE_URL = env("OPENCAST_BASE_URL")
OPENCAST_API_USER = env("OPENCAST_API_USER")
OPENCAST_API_PASSWORD = env("OPENCAST_API_PASSWORD")
ZOOM_VIDEOS_BUCKET = env('ZOOM_VIDEOS_BUCKET')
ZOOM_RECORDING_TYPE_NUM = 'L01'
ZOOM_OPENCAST_WORKFLOW = "DCE-production-zoom"
DEFAULT_SERIES_ID = env("DEFAULT_SERIES_ID")
DEFAULT_PRODUCER_EMAIL = env("DEFAULT_PRODUCER_EMAIL")
OVERRIDE_PRODUCER = env("OVERRIDE_PRODUCER")
OVERRIDE_PRODUCER_EMAIL = env("OVERRIDE_PRODUCER_EMAIL")
CLASS_SCHEDULE_TABLE = env("CLASS_SCHEDULE_TABLE")
LOCAL_TIME_ZONE = env("LOCAL_TIME_ZONE")

sqs = boto3.resource('sqs')
s3 = boto3.resource('s3')

session = requests.Session()
session.auth = HTTPDigestAuth(OPENCAST_API_USER, OPENCAST_API_PASSWORD)
session.headers.update({
    'X-REQUESTED-AUTH': 'Digest',
    'X-Opencast-Matterhorn-Authentication': 'true',

})


def oc_api_request(method, endpoint, **kwargs):
    url = urljoin(OPENCAST_BASE_URL, endpoint)
    logger.info({'url': url, 'kwargs': kwargs})
    try:
        resp = session.request(method, url, **kwargs)
    except requests.RequestException:
        raise
    resp.raise_for_status()
    return resp


@setup_logging
def handler(event, context):

    # allow upload count to be overridden
    num_uploads = event['num_uploads']
    ignore_schedule = event.get('ignore_schedule', False)
    override_series_id = event.get('override_series_id')

    upload_queue = sqs.get_queue_by_name(QueueName=UPLOAD_QUEUE_NAME)

    for i in range(num_uploads):
        try:
            messages = upload_queue.receive_messages(
                MaxNumberOfMessages=1,
                VisibilityTimeout=2500
            )
            upload_message = messages[0]
            logger.debug({'queue_message': upload_message})

        except IndexError:
            logger.warning("No upload queue messages available")
            return
        try:
            upload_data = json.loads(upload_message.body)
            upload_data['ignore_schedule'] = ignore_schedule
            upload_data['override_series_id'] = override_series_id
            logger.info(upload_data)
            wf_id = process_upload(upload_data)
            if wf_id:
                logger.info("Workflow id {} initiated".format(wf_id))
            else:
                logger.info("No workflow initiated.")
            upload_message.delete()
        except Exception as e:
            logger.exception(e)
            raise


def process_upload(upload_data):
    upload = Upload(upload_data)
    upload.upload()
    return upload.workflow_id


class Upload:

    def __init__(self, data):
        self.data = data

    @property
    def creator(self):
        return self.data['host_name']

    @property
    def created(self):
        utc = datetime.strptime(
                self.data['recording_start_time'], '%Y-%m-%dT%H:%M:%SZ')\
                .replace(tzinfo=timezone('UTC'))
        return utc

    @property
    def meeting_uuid(self):
        return self.data['uuid']

    @property
    def s3_prefix(self):
        return md5(self.meeting_uuid.encode()).hexdigest()

    @property
    def zoom_series_id(self):
        return self.data['meeting_number']

    @property
    def ignore_schedule(self):
        return self.data['ignore_schedule']

    @property
    def override_series_id(self):
        return self.data.get('override_series_id')

    def series_id_from_schedule(self):
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(CLASS_SCHEDULE_TABLE)

        r = table.get_item(
            Key={"zoom_series_id": str(self.zoom_series_id)}
        )

        if 'Item' not in r:
            return None
        else:
            schedule = r['Item']
            logger.info(schedule)

        zoom_time = self.created.astimezone(timezone(LOCAL_TIME_ZONE))
        weekdays = ['M', 'T', 'W', 'R', 'F']
        if zoom_time.weekday() > 4:
            logger.debug("Meeting occurred on a weekend.")
            return None
        elif weekdays[zoom_time.weekday()] not in schedule['Days']:
            logger.debug("No opencast recording scheduled for this day of the week.")
            return None

        for time in schedule['Time']:
            scheduled_time = datetime.strptime(time, '%H:%M')
            timedelta = abs(zoom_time -
                            zoom_time.replace(hour=scheduled_time.hour, minute=scheduled_time.minute)
                            ).total_seconds()
            threshold_minutes = 30
            if timedelta < (threshold_minutes * 60):
                return schedule['opencast_series_id']

        logger.debug("Meeting started more than {} minutes before or after opencast scheduled start time."
                     .format(threshold_minutes))

        if self.ignore_schedule:
            logger.debug("'ignore_schedule' enabled; using {} as series id."
                        .format(schedule['opencast_series_id']))
            return schedule['opencast_series_id']

        return None

    @property
    def opencast_series_id(self):

        if not hasattr(self, '_oc_series_id'):

            if self.override_series_id:
                series_id = self.override_series_id
                logger.info("Using override series id '{}'".format(series_id))
            else:
                series_id = self.series_id_from_schedule()

            if series_id is not None:
                logger.info("Matched with opencast series '{}'!".format(series_id))
                self._oc_series_id = series_id
            elif DEFAULT_SERIES_ID is not None and DEFAULT_SERIES_ID != "None":
                logger.info("Using default series id {}".format(DEFAULT_SERIES_ID))
                self._oc_series_id = DEFAULT_SERIES_ID
            else:
                self._oc_series_id = None

        return self._oc_series_id

    @property
    def type_num(self):
        return ZOOM_RECORDING_TYPE_NUM

    @property
    def producer_email(self):
        if OVERRIDE_PRODUCER_EMAIL and OVERRIDE_PRODUCER_EMAIL != "None":
            return OVERRIDE_PRODUCER_EMAIL
        elif 'publisher' in self.episode_defaults:
            return self.episode_defaults['publisher']
        elif DEFAULT_PRODUCER_EMAIL:
            return DEFAULT_PRODUCER_EMAIL

    @property
    def producer(self):
        if OVERRIDE_PRODUCER and OVERRIDE_PRODUCER != "None":
            return OVERRIDE_PRODUCER
        elif 'contributor' in self.episode_defaults:
            return self.episode_defaults['contributor']
        else:
            return "Zoom Ingester"

    @property
    def workflow_definition_id(self):
        return ZOOM_OPENCAST_WORKFLOW

    @property
    def s3_files(self):
        if not hasattr(self, '_s3_files'):
            bucket = s3.Bucket(ZOOM_VIDEOS_BUCKET)
            logger.info("Looking for files in {} with prefix {}"
                        .format(ZOOM_VIDEOS_BUCKET, self.s3_prefix))
            objs = [
                x.Object() for x in bucket.objects.filter(Prefix=self.s3_prefix)
            ]
            self._s3_files = [
                x for x in objs if 'directory' not in x.content_type
            ]
            logger.debug({'s3_files': self._s3_files})
        return self._s3_files

    @property
    def speaker_videos(self):
        return self._get_video_files('speaker')

    @property
    def gallery_videos(self):
        return self._get_video_files('gallery')

    @property
    def workflow_id(self):
        if not hasattr(self, 'workflow_xml'):
            logger.warning("No workflow xml yet!")
            return None
        if not hasattr(self, '_workflow_id'):
            root = ET.fromstring(self.workflow_xml)
            self._workflow_id = root.attrib['id']
        return self._workflow_id

    def upload(self):
        if not self.verify_series_mapping():
            logger.info("No series mapping found for zoom series {}"
                        .format(self.zoom_series_id))
            return
        self.load_episode_defaults()
        self.create_mediapackage_id()
        if self.already_ingested():
            logger.warning("Episode with mediapackage id {} already ingested"
                           .format(self.mediapackage_id))
            return None
        self.get_series_catalog()
        self.ingest()
        return self.workflow_id

    def verify_series_mapping(self):
        return self.opencast_series_id is not None

    def already_ingested(self):
        endpoint = '/workflow/instances.json?mp={}'.format(self.mediapackage_id)
        try:
            resp = oc_api_request('GET', endpoint)
            logger.debug("Lookup for mpid: {}, {}"
                         .format(self.mediapackage_id, resp.json()))
            return int(resp.json()["workflows"]["totalCount"]) > 0
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == '404':
                return False

    def load_episode_defaults(self):

        # data includes 'contributor', 'publisher' (ie, producer email), and 'creator'
        endpoint = '/otherpubs/episodedefaults/{}.json'.format(self.opencast_series_id)
        try:
            resp = oc_api_request('GET', endpoint)
            data = resp.json()['http://purl.org/dc/terms/']
            self.episode_defaults = { k: v[0]['value'] for k, v in data.items() }
        except requests.RequestException:
            self.episode_defaults = {}

        logger.debug({'episode_defaults': self.episode_defaults})

    def create_mediapackage_id(self):
        mpid = str(UUID(md5(self.meeting_uuid.encode()).hexdigest()))
        logger.debug("Created mediapackage id {} from uuid {}"
                     .format(mpid, self.meeting_uuid))
        self.mediapackage_id = mpid

    def get_series_catalog(self):

        logger.info("Getting series catalog for series: {}"
                    .format(self.opencast_series_id))

        endpoint = "/series/{}.json".format(self.opencast_series_id)
        resp = oc_api_request('GET', endpoint)

        logger.debug({'series_catalog': resp.text})

        self.series_catalog = resp.text

    def ingest(self):

        logger.info("Adding mediapackage and ingesting")

        if self.speaker_videos is not None:
            videos = self.speaker_videos
        elif self.gallery_videos is not None:
            videos = self.gallery_videos
        else:
            raise Exception("No mp4 files available for upload.")

        endpoint = "/ingest/addMediaPackage/{}".format(self.workflow_definition_id)

        params = [
            ('creator', (None, escape(self.creator))),
            ('identifier', (None, self.mediapackage_id)),
            ('title', (None, "Lecture")),
            ('type', (None, self.type_num)),
            ('isPartOf', (None, self.opencast_series_id)),
            ('license', (None, 'Creative Commons 3.0: Attribution-NonCommercial-NoDerivs')),
            ('publisher', (None, escape(self.producer_email))),
            ('contributor', (None, escape(self.producer))),
            ('created', (None, datetime.strftime(self.created, '%Y-%m-%dT%H:%M:%SZ'))),
            ('language', (None, 'en')),
            ('seriesDCCatalog', (None, self.series_catalog))
        ]

        for video in videos:
            url = self._generate_presigned_url(video)
            params.extend([
                ('flavor', (None, escape('multipart/partsource'))),
                ('mediaUri', (None, url))
            ])

        resp = oc_api_request('POST', endpoint, files=params)

        logger.debug({'addMediaPackage': resp.text})

        self.workflow_xml = resp.text

    def _generate_presigned_url(self, video):
        url = s3.meta.client.generate_presigned_url(
            'get_object',
            Params={'Bucket': video.bucket_name, 'Key': video.key}
        )
        return url

    def _get_video_files(self, view):
        files = [
            x for x in self.s3_files
            if x.metadata['file_type'].lower() == 'mp4'
               and x.metadata.get('view') == view
        ]
        if len(files) == 0:
            return None
        return files
