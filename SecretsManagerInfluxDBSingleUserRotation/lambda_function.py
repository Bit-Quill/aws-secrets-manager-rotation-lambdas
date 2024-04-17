# Copyright 2023 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import boto3
import json
import logging
import os
import influxdb_client
from contextlib import contextmanager

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TIMESTREAM_INFLUXDB_SERVICE = "timestream-influxdb"

# Mandatory user and token secret fields
INFLUXDB_ENGINE = "engine"
INFLUXDB_INSTANCE_IDENTIFIER = "dbIdentifier"

# Mandatory user secret fields
INFLUXDB_USERNAME = "username"

# Optional user and token secret fields
INFLUXDB_ORG = "org"

# Optional user secret fields
INFLUXDB_PASSWORD = "password"

# Optional token secret fields
INFLUXDB_TOKEN = "token"

# Stages
CURRENT_STAGE = "AWSCURRENT"
PENDING_STAGE = "AWSPENDING"

# Steps
CREATE_STEP = "createSecret"
SET_STEP = "setSecret"
TEST_STEP = "testSecret"
FINISH_STEP = "finishSecret"

# get_db_info response fields
INFLUXDB_ENDPOINT = "endpoint"

# Environment variable keys
SECRETS_MANAGER_ENDPOINT = "SECRETS_MANAGER_ENDPOINT"


def lambda_handler(event, context):
    """Secrets Manager InfluxDB Token Rotation Handler

    This handler uses a single users token to rotate their InfluxDB authentication token. This rotation scheme
    authenticates the current user in the InfluxDB instance and creates a new token for the user with the same
    permissions. The new token is then authenticated and verified to have the same properties as the previous token.
    The old token is then deleted, and the rotation is complete. InfluxDB client does not support setting a custom
    value for a token, so the createSecret and setSecret events both take place during the createSecret step.

    Args:
        event (dict): Lambda dictionary of event parameters. These keys must include the following:
            - SecretId: The secret ARN or identifier
            - ClientRequestToken: The ClientRequestToken of the secret version
            - Step: The rotation step (one of createSecret, setSecret, testSecret, or finishSecret)

        context (LambdaContext): The Lambda runtime information

    Raises:
        ResourceNotFoundException: If the secret with the specified arn and stage does not exist

        ValueError: If the secret is not properly configured for rotation

    """
    arn = event["SecretId"]
    version_id = event["ClientRequestToken"]
    step = event["Step"]

    if arn == "" or arn is None:
        raise ValueError("arn is null or empty.")
    if version_id == "" or version_id is None:
        raise ValueError("version_id is null or empty.")
    if step == "" or step is None:
        raise ValueError("step is null or empty.")

    boto_session = boto3.Session()

    if SECRETS_MANAGER_ENDPOINT not in os.environ:
        raise KeyError(
            "SECRETS_MANAGER_ENDPOINT environment variable not set in the environment variables."
        )

    secret_manager_endpoint = os.environ[SECRETS_MANAGER_ENDPOINT]
    if secret_manager_endpoint == "" or secret_manager_endpoint is None:
        raise ValueError(
            "Secret manager endpoint is null or empty, set the environment variable in the lambda configuration."
        )
    secrets_client = boto_session.client(
        "secretsmanager", endpoint_url=secret_manager_endpoint
    )

    # Make sure the version is staged correctly
    metadata = secrets_client.describe_secret(SecretId=arn)
    if not metadata["RotationEnabled"]:
        logger.error("Secret %s is not enabled for rotation" % arn)
        raise ValueError("Secret %s is not enabled for rotation" % arn)
    versions = metadata["VersionIdsToStages"]
    if version_id not in versions:
        logger.error(
            "Secret version %s has no stage for rotation of secret %s."
            % (version_id, arn)
        )
        raise ValueError(
            "Secret version %s has no stage for rotation of secret %s."
            % (version_id, arn)
        )
    if CURRENT_STAGE in versions[version_id]:
        logger.info(
            "Secret version %s already set as AWSCURRENT for secret %s."
            % (version_id, arn)
        )
        return
    elif PENDING_STAGE not in versions[version_id]:
        logger.error(
            "Secret version %s not set as AWSPENDING for rotation of secret %s."
            % (version_id, arn)
        )
        raise ValueError(
            "Secret version %s not set as AWSPENDING for rotation of secret %s."
            % (version_id, arn)
        )

    if step == "createSecret":
        create_secret(secrets_client, boto_session, arn, version_id)

    elif step == "setSecret":
        logger.info(
            "set_secret has no function as we use the value from the already created token in the create_secret stage"
        )

    elif step == "testSecret":
        test_secret(secrets_client, boto_session, arn, version_id)

    elif step == "finishSecret":
        finish_secret(secrets_client, boto_session, arn, version_id)

    else:
        raise ValueError("Invalid step parameter %s" % step)


@contextmanager
def get_connection(endpoint_url, secret_dict, arn, step):
    """Get connection to InfluxDB

    This helper function returns a connection to the provided InfluxDB instance.

    Args:
        endpoint_url (string): Url for the InfluxDB instance
        secret_dict (dictionary): Dictionary with either username/password or token to authenticate connection
        arn (string): Arn for secret to log in event of failure to make connection
        step (string): Step in which the lambda function is making the connection

    Raises:
        ValueError: If the connection or health check fails

    """
    conn = None
    try:
        conn = (
            influxdb_client.InfluxDBClient(
                url="https://" + endpoint_url + ":8086",
                token=secret_dict[INFLUXDB_TOKEN],
                debug=False,
                verify_ssl=True,
            )
            if INFLUXDB_TOKEN in secret_dict
            else influxdb_client.InfluxDBClient(
                url="https://" + endpoint_url + ":8086",
                username=secret_dict[INFLUXDB_USERNAME],
                password=secret_dict[INFLUXDB_PASSWORD],
                debug=False,
                verify_ssl=True,
            )
        )

        # Verify InfluxDB connection
        health = conn.ping()
        if not health:
            logger.error("%s: Connection failure" % step)

        yield conn
    except Exception as err:
        raise ValueError(
            "%s: Failed to set new authorization with secret ARN %s %s"
            % (step, arn, err)
        ) from err
    finally:
        if conn is not None:
            conn.close()


def create_secret(secrets_client, boto_session, arn, version_id):
    """Create the secret

    This method first checks for the existence of a secret for the passed in token. If one does not exist,
    it will generate a new token in the InfluxDB instance, and put the token value in the AWSPENDING secret value.
    This function completes both the createSecret and setSecret steps.

    Args:
        secrets_client (client): The secrets manager service client
        boto_session (session): Session to retrieve timestream-influxdb client
        arn (string): The secret ARN or other identifier
        version_id (string): The ClientRequestToken associated with the secret version

    Raises:
        ResourceNotFoundException: If the secret with the specified arn and stage does not exist
        ValueError: If the secrets manager token fails to create a new token

    """

    # Make sure the current secret exists
    current_secret_dict = get_secret_dict(secrets_client, arn, CURRENT_STAGE)

    # Now try to get the secret token, if that fails, put a new secret
    try:
        get_secret_dict(secrets_client, arn, PENDING_STAGE, version_id)
        logger.info("create_secret: Successfully retrieved secret for %s." % arn)
    except secrets_client.exceptions.ResourceNotFoundException:
        with get_connection(
            get_db_info(
                current_secret_dict[INFLUXDB_INSTANCE_IDENTIFIER], boto_session
            ),
            current_secret_dict,
            arn,
            CREATE_STEP,
        ) as conn:
            if INFLUXDB_TOKEN in current_secret_dict:
                token_perms = validate_current_token(conn, current_secret_dict)
                create_token(conn, current_secret_dict, token_perms)
            else:
                update_user_creds(
                    secrets_client, boto_session, arn, conn, current_secret_dict
                )

            secrets_client.put_secret_value(
                SecretId=arn,
                ClientRequestToken=version_id,
                SecretString=json.dumps(current_secret_dict),
                VersionStages=[PENDING_STAGE],
            )

    logger.info(
        "create_secret: Successfully created new authorization for ARN %s and version %s."
        % (arn, version_id)
    )


def update_user_creds(secrets_client, boto_session, arn, conn, current_secret_dict):
    """Update credentials for user

    Set the new user credentials in the InfluxDB instance. A valid password must be supplied in the secret, and then a new password will be
    generated for that user.

    Args:
        secrets_client (client): The secrets manager service client
        boto_session (session): Session to retrieve timestream-influxdb client
        arn (string): The secret ARN or other identifier
        conn (InfluxDBClient): Connection to InfluxDB instance
        current_secret_dict (dictionary): Dictionary with the user and password being set

    """

    users = conn.users_api().find_users()
    # Search through users for one that matches the current user
    user = next(
        (
            user
            for user in users.users
            if user.name == current_secret_dict[INFLUXDB_USERNAME]
        ),
        None,
    )

    if INFLUXDB_PASSWORD not in current_secret_dict:
        raise ValueError(
            "User %s password has not been set yet, set the password before performing a rotation."
            % current_secret_dict[INFLUXDB_USERNAME]
        )

    if user is None:
        raise ValueError(
            "User %s has not been created, create the user before performing a rotation"
            % current_secret_dict[INFLUXDB_USERNAME]
        )

    with get_connection(
        get_db_info(current_secret_dict[INFLUXDB_INSTANCE_IDENTIFIER], boto_session),
        current_secret_dict,
        arn,
        CREATE_STEP,
    ) as test_user_client:
        # Ensure user defined in secret can authenticate
        test_user_client.users_api().me()

    new_password = secrets_client.get_random_password()["RandomPassword"]
    conn.users_api().update_password(user=user.id, password=new_password)
    current_secret_dict[INFLUXDB_PASSWORD] = new_password


def validate_current_token(conn, current_secret_dict):
    """

    Validates that the permission set for the token type line up with those defined by the current token.

    Args:
        conn (InfluxDBClient): The connection to the InfluxDB instance
        current_secret_dict (dictionary): Secret dictionary with token type and permissions

    Raises:
        ValueError: If token type and permissions are not set appropriately

    Returns
        final_token_perms (list): All permissions that should be set
    """

    # Ensure no operator tokens can be created without an already existing token
    if INFLUXDB_TOKEN not in current_secret_dict:
        raise ValueError(
            "Operator tokens cannot be created without an already existing operator token"
        )

    # Ensure the already created token has the same permissions as defined in the secret
    authorizations = conn.authorizations_api().find_authorizations()
    current_auth = next(
        (
            auth
            for auth in authorizations
            if auth.token == current_secret_dict[INFLUXDB_TOKEN]
        ),
        None,
    )
    if current_auth is None:
        raise ValueError("Current authorization token not found in DB instance")

    logger.info(
        "Existing token found in DB instance, token will be rotated with existing permissions"
    )
    return current_auth.permissions


def create_token(conn, current_secret_dict, token_perms):
    """

    Create new authorization token in InfluxDB instance. The type and permissions are set from the values
    defined in the secret dictionary. If these values have been altered then the new auth token will not be created.

    Args:
        conn (InfluxDBClient): The connection to the InfluxDB instance
        current_secret_dict (dictionary): Secret dictionary with token type and permissions
        token_perms (list): Set of permissions to apply to the token being created

    Raises:
        ValueError: If no permissions are set for token

    """

    org = next(
        (
            org
            for org in conn.organizations_api().find_organizations()
            if org.name == current_secret_dict[INFLUXDB_ORG]
        ),
        None,
    )
    if org is None:
        raise ValueError("Org does not exist to associate token value with")

    # Influxdb doesn't support setting token values for already created tokens
    # We must therefore create the token in InfluxDB at this stage
    new_authorization = conn.authorizations_api().create_authorization(
        org_id=org.id, permissions=token_perms
    )
    current_secret_dict[INFLUXDB_TOKEN] = new_authorization.token


def test_secret(secrets_client, boto_session, arn, version_id):
    """Test the token against the InfluxDB instance

    This method authenticates the tokens in the AWSCURRENT and AWSPENDING stages against the InfluxDB instance. Once
    both tokens have been authenticated, the users the tokens belong to is verified to be the same,
    and the authentication tokens are verified to have the same permission values.

    Args:
        secrets_client (client): The secrets manager service client
        boto_session (session): Session to retrieve timestream-influxdb client
        arn (string): The secret ARN or other identifier
        version_id (string): The ClientRequestToken associated with the secret version

    Raises: ValueError: If the secrets manager or pending user tokens fail to authenticate, or the current and
    pending token permissions or users are not identical.

    """

    current_secret_dict = get_secret_dict(secrets_client, arn, CURRENT_STAGE)
    pending_secret_dict = get_secret_dict(
        secrets_client, arn, PENDING_STAGE, version_id
    )

    with get_connection(
        get_db_info(current_secret_dict[INFLUXDB_INSTANCE_IDENTIFIER], boto_session),
        pending_secret_dict,
        arn,
        CREATE_STEP,
    ) as client:
        if (
            INFLUXDB_TOKEN in pending_secret_dict
            and INFLUXDB_TOKEN in current_secret_dict
        ):
            authorizations = client.authorizations_api().find_authorizations()
            current_auth = next(
                (
                    auth
                    for auth in authorizations
                    if auth.token == current_secret_dict[INFLUXDB_TOKEN]
                ),
                None,
            )
            pending_auth = next(
                (
                    auth
                    for auth in authorizations
                    if auth.token == pending_secret_dict[INFLUXDB_TOKEN]
                ),
                None,
            )

            if current_auth is None or pending_auth is None:
                raise ValueError(
                    "Current or pending token does not exist for secret ARN %s" % arn
                )

            # Validate current and pending tokens have the same permissions
            if current_auth.permissions != pending_auth.permissions:
                raise ValueError(
                    "Current and pending tokens do not have the same permissions for secret ARN %s"
                    % arn
                )

            # Validate current and pending tokens have the same user
            if current_auth.user != pending_auth.user:
                raise ValueError(
                    "Current and pending tokens failed user equality test for secret ARN %s"
                    % arn
                )

    logger.info("test_secret: Successfully tested authentication rotation")


def finish_secret(secrets_client, boto_session, arn, version_id):
    """Finish the secret

    This method finalizes the rotation process by marking the secret version passed in as the AWSCURRENT secret and
    deleting the previous user authentication token in the InfluxDB instance.

    Args:
        secrets_client (client): The secrets manager service client
        boto_session (session): Session to retrieve timestream-influxdb client
        arn (string): The secret ARN or other identifier
        version_id (string): The ClientRequestToken associated with the secret version

    Raises:
        ValueError: If the secrets manager fails to delete the previous user token in the InfluxDB instance.

    """
    current_secret_dict = get_secret_dict(secrets_client, arn, CURRENT_STAGE)
    pending_secret_dict = get_secret_dict(
        secrets_client, arn, PENDING_STAGE, version_id
    )

    # First describe the secret to get the current version
    metadata = secrets_client.describe_secret(SecretId=arn)
    current_version = None
    for version in metadata["VersionIdsToStages"]:
        if CURRENT_STAGE in metadata["VersionIdsToStages"][version]:
            if version == version_id:
                # The correct version is already marked as current, return
                logger.info(
                    "finish_secret: Version %s already marked as AWSCURRENT for %s"
                    % (version, arn)
                )
                return
            current_version = version
            break

    # Finalize by staging the secret version current
    secrets_client.update_secret_version_stage(
        SecretId=arn,
        VersionStage=CURRENT_STAGE,
        MoveToVersionId=version_id,
        RemoveFromVersionId=current_version,
    )

    # Delete previous authorization for user if using token authentication
    if INFLUXDB_TOKEN in pending_secret_dict and INFLUXDB_TOKEN in current_secret_dict:
        with get_connection(
            get_db_info(
                current_secret_dict[INFLUXDB_INSTANCE_IDENTIFIER], boto_session
            ),
            pending_secret_dict,
            arn,
            CREATE_STEP,
        ) as operator_client:
            authorizations = operator_client.authorizations_api().find_authorizations()
            current_auth = next(
                (
                    auth
                    for auth in authorizations
                    if auth.token == current_secret_dict[INFLUXDB_TOKEN]
                )
            )
            operator_client.authorizations_api().delete_authorization(current_auth)

    logger.info(
        "finish_secret: Successfully set AWSCURRENT stage to version %s for secret %s."
        % (version_id, arn)
    )


def get_secret_dict(secrets_client, arn, stage, version_id=None):
    """Gets the secret dictionary corresponding for the secret arn, stage, and token

    This helper function gets credentials for the arn and stage passed in and returns the dictionary by parsing the
    JSON string

    Args: secrets_client (client): The secrets manager service client arn (string): The secret ARN or other
    identifier stage (string): The stage identifying the secret version version_id (string): The ClientRequestToken
    associated with the secret version, or None if no validation is desired

    Returns:
        SecretDictionary: Secret dictionary

    Raises:
        ResourceNotFoundException: If the secret with the specified arn and stage does not exist
        ValueError: If the secret is not valid JSON
        KeyError: If required keys missing in secret or engine is not 'timestream-influxdb'

    """

    # Only do VersionId validation against the stage if a token is passed in
    if version_id:
        secret = secrets_client.get_secret_value(
            SecretId=arn, VersionId=version_id, VersionStage=stage
        )
    else:
        secret = secrets_client.get_secret_value(SecretId=arn, VersionStage=stage)
    plaintext = secret["SecretString"]
    try:
        secret_dict = json.loads(plaintext)
    except Exception:
        # wrapping json parser exceptions to avoid possible token disclosure
        logger.error("Invalid secret value json for secret %s." % arn)
        raise ValueError("Invalid secret value json for secret %s." % arn)

    if (
        INFLUXDB_TOKEN not in secret_dict
        and INFLUXDB_USERNAME not in secret_dict
        and INFLUXDB_PASSWORD not in secret_dict
    ):
        raise KeyError(
            "No credentials were provided to authenticate with the DB instance"
        )

    # Run semantic validations for secrets
    if INFLUXDB_TOKEN in secret_dict:
        required_fields = [
            INFLUXDB_ENGINE,
            INFLUXDB_ORG,
            INFLUXDB_INSTANCE_IDENTIFIER,
            INFLUXDB_TOKEN,
        ]
    else:
        required_fields = [
            INFLUXDB_ENGINE,
            INFLUXDB_INSTANCE_IDENTIFIER,
            INFLUXDB_USERNAME,
            INFLUXDB_PASSWORD,
        ]

    for field in required_fields:
        if field not in secret_dict:
            raise KeyError("%s key is missing from secret JSON" % field)

    if secret_dict["engine"] != TIMESTREAM_INFLUXDB_SERVICE:
        raise KeyError(
            "Database engine must be set to 'timestream-influxdb' in order to use this rotation lambda"
        )

    return secret_dict


def get_db_info(db_instance_identifier, boto_session):
    """Get InfluxDB information

    This helper function returns the url for the InfluxDB instance that matches the identifier
    that is provided in the user secret.

    Args:
        db_instance_identifier (string): The InfluxDB instance identifier
        boto_session (session): Session to retrieve timestream-influxdb client

    Raises:
        ValueError: Failed to retrieve DB information
        KeyError: DB info returned does not contain expected key

    """

    influx_client = boto_session.client(TIMESTREAM_INFLUXDB_SERVICE)
    describe_response = influx_client.get_db_instance(identifier=db_instance_identifier)

    if describe_response is None or describe_response[INFLUXDB_ENDPOINT] is None:
        raise KeyError("Invalid endpoint info for influxdb instance")

    return describe_response[INFLUXDB_ENDPOINT]
