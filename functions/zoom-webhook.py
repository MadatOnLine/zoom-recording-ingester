import requests
import json
from urllib.parse import parse_qsl
from os import getenv as env
from common import setup_logging
from datetime import datetime
from pytz import timezone
import boto3

import logging
logger = logging.getLogger()

DOWNLOAD_QUEUE_NAME = env('DOWNLOAD_QUEUE_NAME')
LOCAL_TIME_ZONE = env("LOCAL_TIME_ZONE")
DEFAULT_MESSAGE_DELAY = 300
PARALLEL_ENDPOINT = env('PARALLEL_ENDPOINT')


class BadWebhookData(Exception):
    pass


def resp_204(msg):
    logger.info("http 204 response: {}".format(msg))
    return {
        'statusCode': 204,
        'headers': {},
        'body': ""  # 204 = no content
    }


def resp_400(msg):
    logger.error("http 400 response: {}".format(msg))
    return {
        'statusCode': 400,
        'headers': {},
        'body': msg
    }


@setup_logging
def handler(event, context):
    """
    This function accepts the incoming POST relay from the API Gateway endpoint that
    serves as the Zoom webhook endpoint. It checks for the appropriate event status
    type, fetches info about the meeting host, and then passes responsibility on
    to the downloader function via a queue.
    """

    if 'body' not in event:
        return resp_400("bad data: no body in event")

    if PARALLEL_ENDPOINT and PARALLEL_ENDPOINT != "None":

        logger.debug("Sending webhook to {}. Data: {}"
                     .format(PARALLEL_ENDPOINT, event['body']))

        r = requests.post(PARALLEL_ENDPOINT,
                          headers={'content-type': 'application/json'},
                          data=event['body'])

        r.raise_for_status()

        logger.info("Copied webhook to endpoint {}, status code {}"
                    .format(PARALLEL_ENDPOINT, r.status_code))

    try:
        payload = parse_payload(event['body'])
        logger.info({'payload': payload})
    except BadWebhookData as e:
        return resp_400("bad webhook payload data: {}".format(str(e)))

    if payload['status'] != 'RECORDING_MEETING_COMPLETED':
        return resp_204(
            "Handling not implemented for status '{}'".format(payload['status'])
        )

    now = datetime.strftime(timezone(LOCAL_TIME_ZONE).localize(datetime.today()), '%Y-%m-%dT%H:%M:%SZ')

    sqs_message = {
        'uuid': payload["uuid"],
        'host_id': payload["host_id"],
        'correlation_id': context.aws_request_id,
        'received_time': now
    }

    if 'delay_seconds' in payload:
        logger.debug("Override default message delay.")
        send_sqs_message(sqs_message, delay=payload['delay_seconds'])
    else:
        send_sqs_message(sqs_message)

    return {
        'statusCode': 200,
        'headers': {},
        'body': "Success"
    }


def parse_payload(event_body):

    try:
        payload = dict(parse_qsl(event_body, strict_parsing=True))
    except ValueError as e:
        raise BadWebhookData(str(e))

    if 'type' in payload:
        logger.info("Got old-style payload {}".format(payload))
        payload['status'] = payload['type']
        del payload['type']
        if 'content' in payload:
            try:
                content = json.loads(payload['content'])
                logger.debug({"payload content": content})
                payload['uuid'] = content['uuid']
                payload['host_id'] = content['host_id']
                del payload['content']
            except Exception as e:
                raise BadWebhookData("Failed to parse payload 'content' value. {}".format(e))
        else:
            raise BadWebhookData("payload missing 'content' value")
    elif 'status' in payload:
        return payload
    else:
        try:
            payload = json.loads(event_body)
            logger.info("Got new-style payload {}".format(payload))

            if 'payload' in payload and 'event' in payload:
                status = payload['event']
                payload = payload['payload']

                if 'meeting' in payload:
                    payload['object'] = payload['meeting']
                    del payload['meeting']

                if 'recording' in status.lower() and 'completed' in status.lower():
                    payload['status'] = 'RECORDING_MEETING_COMPLETED'
                    payload['uuid'] = payload['object']['uuid']
                    payload['host_id'] = payload['object']['host_id']
                else:
                    payload['status'] = status
            else:
                payload['status'] = payload['event']
                return payload
        except Exception as e:
            raise BadWebhookData("Unrecognized payload format. {}".format(e))

    return payload


def send_sqs_message(message, delay=DEFAULT_MESSAGE_DELAY):

    logger.debug("SQS sending start...")
    sqs = boto3.resource('sqs')

    try:
        download_queue = sqs.get_queue_by_name(QueueName=DOWNLOAD_QUEUE_NAME)

        message_sent = download_queue.send_message(
            MessageBody=json.dumps(message),
            DelaySeconds=delay
        )

    except Exception as e:
        logger.error("Error when sending SQS message for meeting uuid {} :{}"
                     .format(message['uuid'], e))
        raise

    logger.debug({"Message sent": message_sent})

