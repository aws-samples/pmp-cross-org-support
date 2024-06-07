import boto3
import os
import logging
from crhelper import CfnResource

logger = logging.getLogger(__name__)
topic_arn = None
aws_account = None
client = boto3.client('sns')

helper = CfnResource(
    json_logging=False,
    log_level=os.getenv('LOG_LEVEL', 'INFO').upper(),
    boto_level='CRITICAL',
)


def handler(event, context):
    global topic_arn, aws_account

    ResourceProperties = event['ResourceProperties']
    topic_arn = ResourceProperties["TopicArn"]
    aws_account = ResourceProperties["AWSAccount"]

    helper(event, context)


@helper.create
def create(event, context):

    logger.info(f"Topic: {topic_arn}")
    logger.info(f"Adding subscribe permission for account {aws_account}")

    response = client.add_permission(
        TopicArn=topic_arn,
        Label=aws_account,
        AWSAccountId=[
            aws_account,
        ],
        ActionName=[
            'Subscribe',
        ]
    )


@helper.update
def update(event, context):
    logger.info("No update")


@helper.delete
def delete(event, context):

    logger.info(f"Topic: {topic_arn}")
    logger.info(f"Removing subscribe permission for account {aws_account}")

    response = client.remove_permission(
        TopicArn=topic_arn,
        Label=aws_account
    )

    logger.info("Subscription permission removed")
