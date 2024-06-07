import json
import os
import boto3
import logging
import jmespath
from botocore.exceptions import ClientError

experience_id = ''
ssm_parameter_prefix = "/" + os.getenv("SSM_PREFIX") + "/"
logger = logging.getLogger()
logger.setLevel(os.getenv('LOG_LEVEL', 'INFO').upper())
dynamodb = boto3.resource('dynamodb')


def add_product_id_from_db(ids, table_name):
    logger.debug(f"Adding product(s) [len({ids})] to {table_name} table")
    table = dynamodb.Table(table_name)
    try:
        with table.batch_writer() as writer:
            for id in ids:
                logger.debug(f"Adding product [{id}] to {table_name} table")
                writer.put_item(Item={'ID': id})
        logger.info("All Ids added into table %s.", table.name)
    except ClientError:
        logger.exception("Error adding Ids into table %s.", table.name)
        raise


def delete_product_id_from_db(ids, table_name):
    logger.debug(f"Deleting product(s) [{len(ids)}] from {table_name} table")
    table = dynamodb.Table(table_name)
    try:
        with table.batch_writer() as writer:
            for id in ids:
                logger.debug(
                    f"Deleting product [{id}] from {table_name} table")
                writer.delete_item(Key={'ID': id})
        logger.info("All Ids deleted from table %s.", table.name)
    except ClientError:
        logger.exception("Error deleting Ids from table %s.", table.name)
        raise


def get_product_ids_from_db(table_name):
    logger.info(f"Getting product_ids from {table_name} table")
    client = boto3.resource('dynamodb')
    table = client.Table(table_name)
    response = table.scan()
    ids = []
    for i in response['Items']:
        ids.append(i.get('ID'))
        ids.sort()
    return ids


def send_update_notification():
    logger.info("Sending SNS notification")
    message = {"Action": "Products-Updated"}
    client = boto3.client('sns')
    sns_arn = get_ssm_parameter('SNSarn')
    response = client.publish(TargetArn=sns_arn, Message=json.dumps(message))
    return response


def get_ssm_parameter(param):
    client = boto3.client('ssm')
    response = client.get_parameter(Name=ssm_parameter_prefix + param)
    logger.debug('Parameter name ')
    logger.debug(param)
    logger.debug('Parameter value ')
    logger.debug(response['Parameter']['Value'])
    return response['Parameter']['Value']


class PMP:
    def __init__(self, experience_id):
        self._client = boto3.client(
            'marketplace-catalog', region_name='us-east-1')
        self._approved_product_ids = []
        self._rejected_product_ids = []
        self._experience_id = experience_id

    def get_proc_policy(self):
        experience = self.get_experience()
        procpolicy = (json.loads(experience['Details']))[
            'ProcurementPolicies'][0]
        return (procpolicy)

    def get_experience(self):
        parameters = {'Catalog': 'AWSMarketplace',
                      'EntityId': self._experience_id}
        experience = self._client.describe_entity(**parameters)
        return (experience)

    def _get_products_in_experience(self):

        parameters = {'Catalog': 'AWSMarketplace',
                      'EntityId': self.get_proc_policy()}
        experience_description = self._client.describe_entity(**parameters)
        details = json.loads(experience_description['Details'])

        if jmespath.search("Statements[?Effect=='Allow'].Resources[].Ids[]", details) != None:
            self._approved_product_ids = jmespath.search(
                "Statements[?Effect=='Allow'].Resources[].Ids[]", details)
        if jmespath.search("Statements[?Effect=='Deny'].Resources[].Ids[]", details) != None:
            self._rejected_product_ids = jmespath.search(
                "Statements[?Effect=='Deny'].Resources[].Ids[]", details)

    def get_approved_products_ids(self):
        if len(self._approved_product_ids):
            return self._approved_product_ids
        self._get_products_in_experience()
        return self._approved_product_ids

    def get_rejected_products_ids(self):
        if len(self._rejected_product_ids):
            return self._rejected_product_ids
        self._get_products_in_experience()
        return self._rejected_product_ids


def lambda_handler(event, context):
    is_updated = False
    logger.info(f"Getting experience_id")
    experience_id = get_ssm_parameter('experience')
    allways_send_notification = (get_ssm_parameter(
        'AllwaysSendNotification') == "Yes")
    logger.info(f"Experience Id : [{experience_id}]")

    pmp = PMP(experience_id)

    for i in ["approved", "rejected"]:
        logger.info(f"Working {i} products")
        table_name = get_ssm_parameter(
            'ApprovedTable') if i == "approved" else get_ssm_parameter('RejectedTable')
        logger.debug(f"Table name: {table_name}")
        table_set = set(get_product_ids_from_db(table_name))
        logger.info(f"Products in db: {len (table_set)}")
        pmp_set = set(pmp.get_approved_products_ids() if i ==
                      "approved" else pmp.get_rejected_products_ids())
        logger.info(f"Products in experience: {len (pmp_set)}")
        if (table_set != pmp_set):
            logger.info(f"Products in db and experience are different")
            is_updated = True
            product_ids_to_be_added = list(pmp_set - table_set)
            logger.info(
                f"Number of products to be added: {len(product_ids_to_be_added)}")
            logger.debug(f"Products to be added: {product_ids_to_be_added}")
            product_ids_to_be_deleted = list(table_set - pmp_set)
            logger.info(
                f"Number of products to be deleted: {len(product_ids_to_be_deleted)}")
            logger.debug(
                f"Products to be deleted: {product_ids_to_be_deleted}")
            logger.info(f"Updating db...")
            if len(product_ids_to_be_added) > 0:
                add_product_id_from_db(product_ids_to_be_added, table_name)
            if len(product_ids_to_be_deleted) > 0:
                delete_product_id_from_db(
                    product_ids_to_be_deleted, table_name)
    if is_updated or allways_send_notification:
        send_update_notification()
