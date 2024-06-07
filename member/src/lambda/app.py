import json
import os
import boto3
import logging
import uuid
import jmespath
import time
import datetime
from botocore.exceptions import WaiterError
from botocore.waiter import WaiterModel
from botocore.waiter import create_waiter_with_client

'''
This function reads the updated approved products in the master Organization private Marketplace DynamoDB tables and compares it
with the current Private Marketplace products approved and denied products in all experiances.
If the tables are not aligned it adds or removes the relevant products
'''

ssm_parameter_prefix = '/pmp/'
approved_table_name = ''
rejected_table_name = ''
my_session = boto3.session.Session()
my_region = my_session.region_name
logger = logging.getLogger()
logger.setLevel(os.getenv('LOG_LEVEL', 'INFO').upper())

# read the current list from the master DDB account


def get_dynamo_table(tableName):
    logger.info('Connecting to remote DynamoDB Table ' + tableName)
    role_arn = getParameters('CrossAccountAccessRoleARN')
    logger.debug('CrossAccountAccessRole in parameter store is ' + role_arn)
    client = boto3.client('sts')
    newRole = client.assume_role(
        RoleArn=role_arn, RoleSessionName='RoleSessionName', DurationSeconds=900)
    logger.debug('RoleArn assumed')
    dynamodb = boto3.resource('dynamodb', region_name=my_region, aws_access_key_id=newRole['Credentials']['AccessKeyId'],
                              aws_secret_access_key=newRole['Credentials']['SecretAccessKey'], aws_session_token=newRole['Credentials']['SessionToken'])
    table = dynamodb.Table(tableName)
    return table


def getDynamoDBCurrentList(tableName):
    table = get_dynamo_table(tableName)
    response = table.scan()
    IDs = []
    for i in response['Items']:
        IDs.append(i.get('ID'))
        logger.debug('ID fetched: ' + i.get('ID'))
        IDs.sort()
    logger.info(f"Fetched [{len(IDs)}] Ids")
    return IDs


def get_management_account_info():
    client = boto3.client('organizations')
    response = client.describe_organization()
    management_account_id = response["Organization"]["MasterAccountId"]
    management_account_email = response["Organization"]["MasterAccountEmail"]
    return management_account_id, management_account_email


def update_sync_timestamp(tableName, context, number_of_experiences):
    table = get_dynamo_table(tableName)
    aws_account_id, management_account_email = get_management_account_info()
    ts = time.time()
    dt = datetime.datetime.fromtimestamp(ts).isoformat()
    table.update_item(
        Key={'ID': aws_account_id},
        UpdateExpression='SET member_org_email =:moe, stamp =:stamp, update_time_utc =:time, experiences_updated =:exps',
        ExpressionAttributeValues={
            ':moe': str(management_account_email),
            ':stamp': str(ts),
            ':time': dt,
            ':exps': number_of_experiences},
    )
    logger.info(f"{tableName} updated in management org")


def getParameters(param):
    SSMclient = boto3.client('ssm')
    response = SSMclient.get_parameter(Name=ssm_parameter_prefix + param)
    logger.debug('Parameter name ')
    logger.debug(param)
    logger.debug('Parameter value ')
    logger.debug(response['Parameter']['Value'])
    return response['Parameter']['Value']


class PMP:
    def __init__(self):
        self._client = boto3.client(
            'marketplace-catalog', region_name='us-east-1')
        self._batch_size = 50
        self._experience_ids = []
        self._remote_approved_products_ids = []
        self._remote_rejected_products_ids = []
        self._remote_approved_products_ids_cached = False
        self._remote_rejected_products_ids_cached = False

    def get_remote_approved_products_ids(self, remote_approved_table_name):
        if self._remote_approved_products_ids_cached:
            return (self._remote_approved_products_ids)

        self._remote_approved_products_ids = getDynamoDBCurrentList(
            remote_approved_table_name)
        self._remote_approved_products_ids_cached = True
        return (self._remote_approved_products_ids)

    def get_remote_rejected_products_ids(self, remote_rejected_table_name):
        if self._remote_rejected_products_ids_cached:
            return (self._remote_rejected_products_ids)

        self._remote_rejected_products_ids = getDynamoDBCurrentList(
            remote_rejected_table_name)
        self._remote_rejected_products_ids_cached = True
        return (self._remote_rejected_products_ids)

    def get_proc_policy(self, experience_id):
        experience = self.get_experience(experience_id)
        procpolicy = (json.loads(experience['Details']))[
            'ProcurementPolicies'][0]
        return (procpolicy)

    def is_experience_to_sync(self, experience_id):
        experience = self.get_experience(experience_id)
        admin_status = (json.loads(experience['Details'])).get(
            'AdminStatus', "")
        status = (json.loads(experience['Details']))['Status']
        procpolicy = (json.loads(experience['Details']))[
            'ProcurementPolicies'][0]

        if admin_status == "" and status == 'Enabled' and procpolicy != "":
            return True
        else:
            return False

    def get_products_in_experience(self, experience_id):
        approved_product_ids = []
        rejected_product_ids = []

        parameters = {'Catalog': 'AWSMarketplace',
                      'EntityId': self.get_proc_policy(experience_id)}
        experience_description = self._client.describe_entity(**parameters)
        details = json.loads(experience_description['Details'])

        if jmespath.search("Statements[?Effect=='Allow'].Resources[].Ids[]", details) != None:
            approved_product_ids = jmespath.search(
                "Statements[?Effect=='Allow'].Resources[].Ids[]", details)
        if jmespath.search("Statements[?Effect=='Deny'].Resources[].Ids[]", details) != None:
            rejected_product_ids = jmespath.search(
                "Statements[?Effect=='Deny'].Resources[].Ids[]", details)

        logger.info(
            f"{len(approved_product_ids)} approved products and {len(rejected_product_ids)} rejected products found in {experience_id} experience.")
        return (approved_product_ids, rejected_product_ids)

    def sync_experience(self, expereince_id):

        (approved_product_ids, rejected_product_ids) = self.get_products_in_experience(
            expereince_id)

        local_approved_products_ids = set(approved_product_ids)
        local_rejected_product_ids = set(rejected_product_ids)
        remote_approved_product_ids = set(
            self.get_remote_approved_products_ids(approved_table_name))
        remote_rejected_product_ids = set(
            self.get_remote_rejected_products_ids(rejected_table_name))

        # Products only in the remote experience, that need to be approve
        delta_approved_product_ids = remote_approved_product_ids - local_approved_products_ids
        # Approved products in the local experience that need to be rejected
        delta_rejected_product_ids = local_approved_products_ids - remote_approved_product_ids
        # Rejected products only in the remote experience, that need to be rejected
        delta_rejected_product_ids |= remote_rejected_product_ids - local_rejected_product_ids

        logger.info(
            f"Adding [{len(delta_approved_product_ids)}] products to approve list")
        self.add_product_to_experience(
            expereince_id, list(delta_approved_product_ids))

        logger.info(
            f"Adding [{len(delta_rejected_product_ids)}] products to reject list")
        self.add_product_to_experience(expereince_id, list(
            delta_rejected_product_ids), to_approve=False)

    def add_product_to_experience(self, experience_id, pproducts, to_approve=True):
        # if the to_approve parameter is false, the products are added to the rejected products

        if not len(pproducts):
            logger.info("No products to add to " +
                        ("approve." if to_approve else "reject."))
            return

        waiter_name = 'AddProductWaiter'
        delay = 5
        max_attempts = 60
        waiter_config = {
            'version': 2,
            'waiters': {
                waiter_name: {
                    "delay": delay,
                    "maxAttempts": max_attempts,
                    "operation": "DescribeChangeSet",
                    "acceptors": [
                        {
                            "matcher": "path",
                            "expected": "SUCCEEDED",
                            "argument": "Status",
                            "state": "success"
                        },
                        {
                            "matcher": "path",
                            "expected": "PREPARING",
                            "argument": "Status",
                            "state": "retry"
                        },
                        {
                            "matcher": "path",
                            "expected": "APPLYING",
                            "argument": "Status",
                            "state": "retry"
                        },
                        {
                            "matcher": "path",
                            "expected": "CANCELLED",
                            "argument": "Status",
                            "state": "failure"
                        },
                        {
                            "matcher": "path",
                            "expected": "FAILED",
                            "argument": "Status",
                            "state": "failure"
                        }
                    ]
                }
            }
        }
        waiter_model = WaiterModel(waiter_config)

        products_slices = self.slice_array(pproducts, self._batch_size)
        i = 1
        logger.info(f"Total products to add: {len(pproducts)}")
        for p in (products_slices):
            logger.info(f"Batch [{i}/{len(products_slices)}]")
            i += 1
            products = {
                'Products': [
                    {
                        "Ids": p
                    }
                ]
            }

            change_type = "AllowProductProcurement" if to_approve else "DenyProductProcurement"

            kargs = {
                'Catalog': 'AWSMarketplace',
                'ChangeSet': [
                    {
                        'ChangeType': change_type,
                        'Entity': {
                            'Type': 'Experience@1.0',
                            'Identifier': experience_id
                        },
                        'Details': json.dumps(products),
                    },
                ],
                'ClientRequestToken': str(uuid.uuid4())
            }
            while True:
                try:
                    response = self._client.start_change_set(**kargs)

                    custome_waiter = create_waiter_with_client(
                        waiter_name, waiter_model, self._client)

                    try:
                        custome_waiter.wait(
                            ChangeSetId=response['ChangeSetId'], Catalog='AWSMarketplace')
                    except WaiterError as e:
                        logger.error(WaiterError)
                        logger.error(e)
                        raise

                    break
                except Exception as e:
                    logger.error(e)
                    wait_time = 30
                    logger.error(f"Waiting {wait_time} secs")
                    time.sleep(wait_time)
                    logger.info(f"Retrying...")
                    continue

    def slice_array(self, array, size):
        return [array[i:i + size] for i in range(0, len(array), size)]

    # If the SSM parameter MemberExperienceIds has value it will get used to sync, if not all experience in the account will be returned.
    def get_experience_ids(self):
        if len(self._experience_ids):
            return self._experience_ids

        client = boto3.client('ssm')
        try:
            self._experience_ids = client.get_parameter(
                Name=ssm_parameter_prefix + 'MemberExperienceIds')['Parameter']['Value'].split(',')

            return self._experience_ids
        except client.exceptions.ParameterNotFound:
            logging.info("MemberExperienceIds wasn't found in Parameter store")

        parameters = {'Catalog': 'AWSMarketplace', 'EntityType': "Experience", 'FilterList' : [{'Name': 'Scope', 'ValueList': [ 'SharedWithMe'] }] }
        response = self._client.list_entities(**parameters)
        experience_ids = [e.get('EntityId')
                          for e in response.get('EntitySummaryList')]

        while "NextToken" in response:
            parameters["NextToken"] = self._experiences = response.get(
                'NextToken')
            response = self._client.list_entities(**parameters)
            experience_ids.extend([e.get('EntityId')
                                  for e in response.get('EntitySummaryList')])

        logging.debug (f"Experiences return by CAPI")
        logging.debug (experience_ids)

        experiences_to_sync = []

        for id in experience_ids:
            try:
                if self.is_experience_to_sync(id):
                    experiences_to_sync.append(id)
            except:
                logging.info(
                    f"Experience {id} doesn't have a procurament policy, is not active, or is archived. Experience is being ignored.")
        self.experience_ids = experiences_to_sync
        return self.experience_ids

    def get_experience(self, experience_id):
        parameters = {'Catalog': 'AWSMarketplace', 'EntityId': experience_id}
        experience = self._client.describe_entity(**parameters)
        return (experience)

    def get_audiences(self):
        parameters = {'Catalog': 'AWSMarketplace', 'EntityType': "Audience"}
        response = self._client.list_entities(**parameters)
        audiences = [e.get('EntityId')
                     for e in response.get('EntitySummaryList')]

        while "NextToken" in response:
            parameters["NextToken"] = self._audiences = response.get(
                'NextToken')
            response = self._client.list_entities(**parameters)
            audiences.extend([e.get('EntityId')
                             for e in response.get('EntitySummaryList')])

        return audiences

    def is_aws_account_id_in_active_experience_audiences(self, account_id):
        for audience in self.get_audiences():
            audience_description = self._client.describe_entity(
                Catalog='AWSMarketplace', EntityId=audience)
            audience_details = json.loads(audience_description['Details'])
            if account_id in audience_details.get('Principals', []) and audience_details.get('ExperienceId') in self.get_experience_ids():
                return True
        return False


def lambda_handler(event, context):
    global approved_table_name, rejected_table_name
    approved_table_name = getParameters('ApprovedTable')
    logger.info('ApprovedTable from parameter store is ' + approved_table_name)
    rejected_table_name = getParameters('RejectedTable')
    logger.info('RejectedTable from parameter store is ' + rejected_table_name)
    sync_timestamps_table_name = getParameters('SyncTimestampsTableName')
    logger.info('SyncTimestampsTableName from parameter store is ' +
                sync_timestamps_table_name)

    pmp = PMP()
    experiences = pmp.get_experience_ids()

    number_of_experiences = len(experiences)
    logger.info(f"Syncing [{number_of_experiences}] experiences")

    i = 0
    for exp_id in experiences:
        logger.info(f"Syncing experience: {exp_id} [{i+1}/{len(experiences)}]")
        pmp.sync_experience(exp_id)
        i += 1

    logger.info(f"Updating timestamp")
    update_sync_timestamp(sync_timestamps_table_name,
                          context, number_of_experiences)
